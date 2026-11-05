#include "selftest_host.h"
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include "usb_desc.h"

#define POLL_BYTES 1024u   /* largest high-speed interrupt packet */
#define SHOWN_BYTES 16u

static void (*log_line)(const char *line);
static selftest_host_status_t status;
static yoke_usbh_urb_t control, poll;
static uint8_t control_data[255], poll_data[POLL_BYTES], last[POLL_BYTES];
static uint32_t last_length;
static uint16_t lang;
static unsigned string_step;   /* 0: LANGIDs, 1..3: iManufacturer, iProduct, iSerialNumber */

static void say(const char *format, ...)
{
    char line[160];
    va_list args;
    va_start(args, format);
    vsnprintf(line, sizeof(line), format, args);
    va_end(args);
    log_line(line);
}

void selftest_host_init(void (*log)(const char *line))
{
    log_line = log;
    memset(&status, 0, sizeof(status));
}

const selftest_host_status_t *selftest_host_status(void) { return &status; }

static bool get_string(uint8_t index)
{
    const uint8_t setup[8] = {0x80, 6, index, USB_DT_STRING, (uint8_t)lang, (uint8_t)(lang >> 8),
                              sizeof(control_data), 0};
    control = (yoke_usbh_urb_t){.endpoint = 0x00, .data = control_data,
                                .length = sizeof(control_data)};
    memcpy(control.setup, setup, sizeof(setup));
    int err = yoke_usbh_submit(&control);
    if (err) say("string request failed (%d)", err);
    return err == 0;
}

static void start_poll(void)
{
    if (yoke_usbh_submit(&poll)) {
        status.errors++;
        status.poll_endpoint = 0;
    }
}

/* After the strings: claim alternate 0 and poll the first interrupt IN. */
static void claim_and_poll(void)
{
    int err = yoke_usbh_claim();
    status.claimed = err == 0;
    if (err) {
        say("claim failed (%d); descriptors above are still valid", err);
        return;
    }
    usb_walk_t w;
    usb_walk_init(&w, yoke_usbh_config_descriptor());
    for (const uint8_t *p; (p = usb_walk_next(&w));) {
        if (p[1] != USB_DT_ENDPOINT || p[0] < 7 || w.alternate || (p[3] & 3u) != USB_XFER_INT ||
            !(p[2] & 0x80u))
            continue;
        status.poll_endpoint = p[2];
        poll = (yoke_usbh_urb_t){.endpoint = p[2], .data = poll_data,
                                 .length = usb_le16(p + 4) & 0x7ffu};
        say("polling endpoint 0x%02x (%u bytes); press keys or move the device", p[2],
            (unsigned)poll.length);
        start_poll();
        return;
    }
    say("no interrupt IN endpoint to poll; enumeration self-test complete");
}

static void next_string(void)
{
    const uint8_t *dev = yoke_usbh_device_descriptor();
    while (++string_step <= 3)
        if (dev[13 + string_step] && get_string(dev[13 + string_step])) return;
    claim_and_poll();
}

static void on_attached(void *ctx)
{
    (void)ctx;
    static const char *const speeds[] = {"unknown", "low", "full", "high"};
    const uint8_t *dev = yoke_usbh_device_descriptor(), *cfg = yoke_usbh_config_descriptor();
    char line[160];
    status = (selftest_host_status_t){.attached = true, .vendor = usb_le16(dev + 8),
                                      .product = usb_le16(dev + 10), .speed = yoke_usbh_speed()};
    usb_desc_device(dev, line, sizeof(line));
    say("device %u attached at %s speed: %s", yoke_usbh_address(), speeds[status.speed & 3u], line);
    if (usb_desc_line(cfg, line, sizeof(line))) say("%s", line);
    usb_walk_t w;
    usb_walk_init(&w, cfg);
    for (const uint8_t *p; (p = usb_walk_next(&w));)
        if (usb_desc_line(p, line, sizeof(line))) say("%s", line);
    if (!usb_walk_complete(&w)) say("configuration descriptor is malformed after byte %u", w.offset);
    string_step = 0;
    last_length = 0;
    lang = 0;
    if (!(dev[14] | dev[15] | dev[16]) || !get_string(0)) claim_and_poll();
}

static void on_control(int err, uint32_t actual)
{
    static const char *const names[] = {"", "manufacturer", "product", "serial"};
    if (err || actual < 2 || control_data[1] != USB_DT_STRING) {
        say("string %u: no answer (%d)", string_step, err);
    } else if (string_step == 0) {
        lang = actual >= 4 ? usb_le16(control_data + 2) : 0x0409u;
    } else {
        char text[128];
        if (control_data[0] > actual) control_data[0] = (uint8_t)actual;
        usb_desc_string(control_data, text, sizeof(text));
        say("%s: \"%s\"", names[string_step], text);
    }
    next_string();
}

static void on_poll(int err, uint32_t actual)
{
    if (err) {
        /* -ECONNRESET/-ENODEV: unplugged or released; anything else is a fault. */
        if (err != -YOKE_USBH_ECONNRESET && err != -YOKE_USBH_ENODEV) {
            status.errors++;
            say("endpoint 0x%02x stopped (%d)", poll.endpoint, err);
        }
        status.poll_endpoint = 0;
        return;
    }
    status.reports++;
    if (actual != last_length || memcmp(last, poll_data, actual)) {
        char hex[3 * SHOWN_BYTES + 4] = "";
        for (uint32_t i = 0; i < actual && i < SHOWN_BYTES; ++i)
            snprintf(hex + 3 * i, 4, " %02x", poll_data[i]);
        say("report %lu (%lu bytes):%s%s", (unsigned long)status.reports, (unsigned long)actual,
            hex, actual > SHOWN_BYTES ? " ..." : "");
        memcpy(last, poll_data, actual);
        last_length = actual;
    }
    start_poll();
}

static void on_complete(void *ctx, yoke_usbh_urb_t *urb, int err, uint32_t actual)
{
    (void)ctx;
    if (urb == &control) on_control(err, actual);
    else if (urb == &poll) on_poll(err, actual);
}

static void on_detached(void *ctx)
{
    (void)ctx;
    say("device detached after %lu reports", (unsigned long)status.reports);
    status.attached = status.claimed = false;
    status.poll_endpoint = 0;
}

const yoke_usbh_sink_t selftest_host_sink = {
    .attached = on_attached, .detached = on_detached, .complete = on_complete,
};
