#include "selftest_cdc.h"
#include <stdarg.h>
#include <stdio.h>
#include <string.h>

/* pid.codes test VID:PID 1209:0001: for private testing only, never sold. */
#define VID 0x1209u
#define PID 0x0001u
#define CONFIG_TOTAL 75u
#define NOTIFY_AT 45u     /* endpoint descriptor offsets in `config` */
#define OUT_AT 61u
#define IN_AT 68u
#define SET_LINE_CODING 0x20u
#define GET_LINE_CODING 0x21u
#define SET_CONTROL_LINE_STATE 0x22u
#define SEND_BREAK 0x23u

static const uint8_t device[18] = {
    18, 1, 0x00, 0x02, 0xef, 0x02, 0x01, 64,  /* USB 2.0, IAD composite, ep0 64 */
    VID & 0xff, VID >> 8, PID & 0xff, PID >> 8, 0x00, 0x01, 1, 2, 3, 1,
};

/* One configuration: CDC-ACM with an interface association. The box is bus
 * powered by the computer and declares 500 mA (⚠ unmeasured, as the
 * presentation default in USB-DATAPATH.md). */
static uint8_t config[CONFIG_TOTAL] = {
    9, 2, CONFIG_TOTAL, 0, 2, 1, 0, 0x80, 250,
    8, 11, 0, 2, 0x02, 0x02, 0x00, 0,          /* IAD: interfaces 0-1, CDC ACM */
    9, 4, 0, 0, 1, 0x02, 0x02, 0x00, 0,        /* communications, no AT protocol */
    5, 0x24, 0x00, 0x10, 0x01,                 /* header, CDC 1.10 */
    5, 0x24, 0x01, 0x00, 1,                    /* call management: data interface 1 */
    4, 0x24, 0x02, 0x02,                       /* ACM: line coding and serial state */
    5, 0x24, 0x06, 0, 1,                       /* union: 0 controls 1 */
    7, 5, 0x81, 0x03, 16, 0, 16,               /* notification, interrupt IN */
    9, 4, 1, 0, 2, 0x0a, 0x00, 0x00, 0,        /* CDC data */
    7, 5, 0x02, 0x02, 64, 0, 0,                /* bulk OUT (512 at high speed) */
    7, 5, 0x82, 0x02, 64, 0, 0,                /* bulk IN */
};

static const uint8_t langids[4] = {4, 3, 0x09, 0x04};
static uint8_t manufacturer[2 + 2 * 4], product[2 + 2 * 18], serial_desc[2 + 2 * 16];
static yoke_usbd_string_t strings[4] = {
    {0, 0, langids}, {1, 0x0409, manufacturer}, {2, 0x0409, product}, {3, 0x0409, serial_desc},
};
static yoke_usbd_descriptors_t descriptors = {
    .device = device, .num_configs = 1, .configs = {config},
    .strings = strings, .num_strings = 4,
};

static const selftest_cdc_port_t *port;
static uint8_t *buffer;
static selftest_cdc_status_t status;
static uint8_t line_coding[7], incoming[7];
static int notify = -1, out = -1, in = -1;
static uint16_t packet;
static enum { EP0_NONE, EP0_LINE_CODING } ep0_step;

static void utf16(uint8_t *desc, size_t cap, const char *text)
{
    size_t n = 0;
    for (; text[n] && 2 + 2 * (n + 1) <= cap; ++n) {
        desc[2 + 2 * n] = (uint8_t)text[n];
        desc[3 + 2 * n] = 0;
    }
    desc[0] = (uint8_t)(2 + 2 * n);
    desc[1] = 3;
}

static void say(const char *format, ...)
{
    char line[96];
    va_list args;
    va_start(args, format);
    vsnprintf(line, sizeof(line), format, args);
    va_end(args);
    port->log(line);
}

const yoke_usbd_descriptors_t *selftest_cdc_init(const selftest_cdc_port_t *p,
                                                 yoke_usbd_speed_t speed, const char *serial,
                                                 uint8_t *buf)
{
    port = p;
    buffer = buf;
    memset(&status, 0, sizeof(status));
    status.baud = 115200;
    const uint8_t coding[7] = {0x00, 0xc2, 0x01, 0x00, 0, 0, 8}; /* 115200 8N1 */
    memcpy(line_coding, coding, sizeof(line_coding));
    packet = speed == YOKE_USBD_SPEED_HIGH ? 512u : 64u;
    config[OUT_AT + 4] = config[IN_AT + 4] = (uint8_t)packet;
    config[OUT_AT + 5] = config[IN_AT + 5] = (uint8_t)(packet >> 8);
    utf16(manufacturer, sizeof(manufacturer), "Yoke");
    utf16(product, sizeof(product), "Yoke USB self-test");
    utf16(serial_desc, sizeof(serial_desc), serial);
    descriptors.speed = speed;
    notify = out = in = -1;
    return &descriptors;
}

const selftest_cdc_status_t *selftest_cdc_status(void) { return &status; }

static void read_next(void)
{
    yoke_usbd_ep_read(out, buffer, packet);
}

static void configure(uint8_t value)
{
    notify = out = in = -1;
    status.configured = value == 1;
    if (status.configured) {
        notify = yoke_usbd_ep_enable(config + NOTIFY_AT);
        out = yoke_usbd_ep_enable(config + OUT_AT);
        in = yoke_usbd_ep_enable(config + IN_AT);
        if (notify < 0 || out < 0 || in < 0) {
            status.errors++;
            say("self-test: endpoint enable failed (%d, %d, %d)", notify, out, in);
        } else {
            read_next();
        }
    }
    say("self-test: configuration %u", value);
}

static void reply_line_coding(void *arg)
{
    (void)arg;
    status.deferred_replies++;
    yoke_usbd_ep0_write(line_coding, sizeof(line_coding), false);
}

static void acknowledge(void *arg)
{
    (void)arg;
    status.deferred_replies++;
    yoke_usbd_ep0_read(NULL, 0);
}

static void control(const uint8_t *s)
{
    uint8_t type = s[0], request = s[1];
    uint16_t value = (uint16_t)(s[2] | s[3] << 8), length = (uint16_t)(s[6] | s[7] << 8);
    status.control_requests++;
    ep0_step = EP0_NONE;
    if (type == 0x00 && request == 9) {
        configure((uint8_t)value);
    } else if (type == 0x21 && request == SET_LINE_CODING && length == sizeof(incoming)) {
        ep0_step = EP0_LINE_CODING;
        yoke_usbd_ep0_read(incoming, sizeof(incoming));
    } else if (type == 0xa1 && request == GET_LINE_CODING) {
        port->later(reply_line_coding, NULL, SELFTEST_CDC_DEFER_MS);
    } else if (type == 0x21 && request == SET_CONTROL_LINE_STATE) {
        status.dtr = value & 1u;
        status.rts = value & 2u;
        say("self-test: DTR %d RTS %d (status held off %u ms)", status.dtr, status.rts,
            SELFTEST_CDC_DEFER_MS);
        port->later(acknowledge, NULL, SELFTEST_CDC_DEFER_MS);
    } else if (type == 0x21 && request == SEND_BREAK) {
        yoke_usbd_ep0_read(NULL, 0);
    } else if (type == 0x81 && request == 10) {
        static const uint8_t alternate;
        yoke_usbd_ep0_write(&alternate, 1, false);
    } else if (type == 0x01 && request == 11 && value == 0) {
        yoke_usbd_ep0_read(NULL, 0);
    } else {
        yoke_usbd_ep0_stall();
    }
}

static void on_event(void *ctx, yoke_usbd_event_t type, const uint8_t *setup)
{
    (void)ctx;
    static const char *const names[] = {"", "connect", "control", "suspend", "resume", "bus reset",
                                        "disconnect"};
    if (type == YOKE_USBD_CONTROL || type == YOKE_USBD_CONTROL_ACKED) {
        control(setup);
        return;
    }
    if (type == YOKE_USBD_RESET || type == YOKE_USBD_DISCONNECT) {
        status.configured = false;
        notify = out = in = -1;
    }
    say("self-test: %s", names[type]);
}

static void on_ep0_done(void *ctx, int result)
{
    (void)ctx;
    if (result < 0) {
        status.errors++;
        say("self-test: control request ended with %d", result);
    } else if (ep0_step == EP0_LINE_CODING && result == (int)sizeof(incoming)) {
        memcpy(line_coding, incoming, sizeof(line_coding));
        status.baud = (uint32_t)line_coding[0] | (uint32_t)line_coding[1] << 8 |
                      (uint32_t)line_coding[2] << 16 | (uint32_t)line_coding[3] << 24;
        say("self-test: line coding %lu baud, %u data bits", (unsigned long)status.baud,
            line_coding[6]);
        ep0_step = EP0_NONE; /* TinyUSB sent the status itself */
    }
}

static void on_ep_done(void *ctx, int handle, int result)
{
    (void)ctx;
    if (handle != out && handle != in) return; /* a voided configuration */
    if (result == YOKE_USBD_SHUTDOWN || result == YOKE_USBD_INTERRUPTED) return;
    if (result < 0 && result != YOKE_USBD_HALT_CLEARED) {
        status.errors++;
        say("self-test: endpoint %d failed (%d)", handle, result);
        return;
    }
    if (handle == out && result > 0) {
        yoke_usbd_ep_write(in, buffer, (size_t)result, false); /* echo */
        return;
    }
    if (handle == in && result > 0) status.echoed_bytes += (uint32_t)result;
    read_next();
}

const yoke_usbd_sink_t selftest_cdc_sink = {
    .event = on_event, .ep0_done = on_ep0_done, .ep_done = on_ep_done,
};
