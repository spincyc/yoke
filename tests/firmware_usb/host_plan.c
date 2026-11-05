/* Descriptor rules of the TinyUSB DWC2 backend (usbd_plan.c) and the shared
 * descriptor walk and log lines (usb_desc.c), unchanged, on the host.
 * Controller limits are TinyUSB 0.21's dwc2_esp32.h values (⚠ not
 * measured on silicon). */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "usb_desc.h"
#include "usbd_plan.h"

#define S3 ((usbd_limits_t){7, 5, 242, 16, false})
#define P4 ((usbd_limits_t){16, 8, 992, 16, true})

static uint8_t device[18] = {18, 1, 0x00, 0x02, 0, 0, 0, 64, 0x6d, 0x04, 0x1c, 0xc3,
                             0, 1, 1, 2, 0, 1};
static uint8_t cfg[512];
static size_t used;
static yoke_usbd_descriptors_t d;
static char why[160];

static void begin(void)
{
    const uint8_t header[9] = {9, 2, 0, 0, 0, 1, 0, 0x80, 50};
    memcpy(cfg, header, 9);
    used = 9;
    d = (yoke_usbd_descriptors_t){.speed = YOKE_USBD_SPEED_FULL, .device = device,
                                  .num_configs = 1, .configs = {cfg}};
}

static void add(const uint8_t *desc)
{
    memcpy(cfg + used, desc, desc[0]);
    used += desc[0];
    cfg[2] = (uint8_t)used;
    cfg[3] = (uint8_t)(used >> 8);
}

static void interface(uint8_t number, uint8_t alternate, uint8_t endpoints, uint8_t cls)
{
    const uint8_t desc[9] = {9, 4, number, alternate, endpoints, cls, 0, 0, 0};
    add(desc);
    if (!alternate) cfg[4]++;
}

static void endpoint(uint8_t address, uint8_t type, uint16_t packet)
{
    const uint8_t desc[7] = {7, 5, address, type, (uint8_t)packet, (uint8_t)(packet >> 8), 1};
    add(desc);
}

static int check(usbd_limits_t lim)
{
    why[0] = 0;
    return usbd_plan_check(&d, &lim, why, sizeof(why));
}

static void refused(usbd_limits_t lim, const char *reason)
{
    assert(check(lim) == -1);
    if (!strstr(why, reason)) {
        fprintf(stderr, "reason \"%s\" lacks \"%s\"\n", why, reason);
        assert(0);
    }
}

static int mode_keyboard(void)
{
    begin();
    interface(0, 0, 1, 3);
    endpoint(0x81, 3, 8);
    assert(check(S3) == 0);
    usbd_fifo_t f[4];
    assert(usbd_plan_fifos(cfg, f, 4) == 1 && f[0].address == 0x81 && f[0].max_packet == 8);
    /* RX: 13 + 1 + 2 * (64 / 4 + 1) + 2 * 7 = 62; TX: EP0 16 + 2. */
    const usbd_limits_t s3 = S3;
    assert(usbd_plan_fifo_words(cfg, &s3) == 62 + 16 + 2);
    assert(usbd_plan_endpoint(cfg, 0, 0, 0x81) == cfg + 18);
    assert(!usbd_plan_endpoint(cfg, 0, 1, 0x81) && !usbd_plan_endpoint(cfg, 0, 0, 0x01));
    assert(usbd_plan_config_of(&d, cfg + 9) == 0 && usbd_plan_config_of(&d, device) == -1);
    return 0;
}

static int mode_alternates(void)
{
    /* One FIFO per address at its largest packet across alternates. */
    begin();
    interface(0, 0, 0, 0xff);
    interface(0, 1, 1, 0xff);
    endpoint(0x81, 2, 32);
    interface(0, 2, 1, 0xff);
    endpoint(0x81, 2, 64);
    assert(check(S3) == 0);
    usbd_fifo_t f[4];
    assert(usbd_plan_fifos(cfg, f, 4) == 1 && f[0].max_packet == 64);
    assert(usbd_plan_endpoint(cfg, 0, 2, 0x81)[4] == 64);
    return 0;
}

static int mode_refusals(void)
{
    begin(); interface(0, 0, 1, 0xff); endpoint(0x87, 2, 64);
    refused(S3, "endpoint numbers 1..6");
    assert(check(P4) == -1);                  /* 64-byte bulk is invalid at high speed */
    begin(); interface(0, 0, 1, 0xff); endpoint(0x81, 1, 64);
    refused(S3, "isochronous");
    begin(); interface(0, 0, 1, 0xff); endpoint(0x81, 2, 512);
    refused(S3, "wMaxPacketSize 512 is invalid at full speed");
    begin(); interface(0, 0, 1, 0xff); endpoint(0x81, 3, 0x1000 | 512);
    d.speed = YOKE_USBD_SPEED_HIGH;
    refused(P4, "high-bandwidth");
    begin(); interface(0, 0, 8, 0xff);        /* 7 x 512-byte bulk IN + OUT: FIFO RAM */
    for (uint8_t i = 1; i <= 7; ++i) endpoint((uint8_t)(0x80 | i), 2, 512);
    endpoint(0x01, 2, 512);
    d.speed = YOKE_USBD_SPEED_HIGH;
    refused(P4, "FIFO words");
    begin(); interface(16, 0, 0, 0xff);
    refused(S3, "TinyUSB's 16 interfaces");
    begin(); interface(0, 0, 0, 9);
    refused(S3, "hub");
    begin(); { const uint8_t hid[9] = {9, 0x21, 0x11, 1, 0, 1, 0x22, 50, 0}; add(hid); }
    interface(0, 0, 0, 3);
    refused(S3, "before the first interface");
    begin(); interface(0, 0, 0, 0xff); cfg[5] = 2;
    refused(S3, "bConfigurationValue 2; TinyUSB needs 1");
    begin(); interface(0, 0, 0, 0xff); cfg[used - 9] = 30; /* bLength past wTotalLength */
    refused(S3, "bad bLength");
    begin(); interface(0, 0, 0, 0xff); device[7] = 8;
    refused(S3, "bMaxPacketSize0");
    device[7] = 64;
    begin(); interface(0, 0, 0, 0xff); device[17] = 2;
    refused(S3, "bNumConfigurations 2");
    device[17] = 1;
    begin(); interface(0, 0, 0, 0xff); d.speed = YOKE_USBD_SPEED_HIGH;
    refused(S3, "full-speed devices only");
    begin(); { const uint8_t iad[8] = {8, 11, 15, 2, 2, 2, 0, 0}; add(iad); }
    interface(15, 0, 0, 2);
    refused(S3, "interface association");
    return 0;
}

/* IN endpoint n uses TX FIFO n: the S3 has IN numbers 1..4, the P4 1..7.
 * The P4's high-speed port presents high-speed devices only (TinyUSB 0.21
 * runs a high-speed PHY at high speed). */
static int mode_speeds_and_in_numbers(void)
{
    begin(); interface(0, 0, 2, 0xff); endpoint(0x84, 3, 8); endpoint(0x06, 2, 64);
    assert(check(S3) == 0);
    begin(); interface(0, 0, 1, 0xff); endpoint(0x85, 3, 8);
    refused(S3, "IN endpoint numbers 1..4");
    begin(); interface(0, 0, 1, 0xff); endpoint(0x86, 2, 64);
    refused(S3, "IN endpoint numbers 1..4");
    begin(); interface(0, 0, 1, 0xff); endpoint(0x87, 2, 512);
    d.speed = YOKE_USBD_SPEED_HIGH;
    assert(check(P4) == 0);
    begin(); interface(0, 0, 1, 0xff); endpoint(0x88, 2, 512);
    d.speed = YOKE_USBD_SPEED_HIGH;
    refused(P4, "IN endpoint numbers 1..7");
    begin(); interface(0, 0, 1, 3); endpoint(0x81, 3, 8);
    refused(P4, "high-speed devices only");
    return 0;
}

/* A high-speed port serves the presented qualifier and other-speed
 * configuration when they fit, else the device's own fields and none. */
static int mode_other_speed(void)
{
    begin(); interface(0, 0, 1, 0xff); endpoint(0x81, 2, 512);
    d.speed = YOKE_USBD_SPEED_HIGH;
    const uint8_t qualifier[10] = {10, 6, 0x00, 0x02, 0xff, 0, 0, 64, 1, 0};
    uint8_t other[25] = {9, 7, 25, 0, 1, 1, 0, 0x80, 50};
    memcpy(other + 9, cfg + 9, 16);
    other[22] = 64;                           /* full-speed bulk packet: 64 */
    other[23] = 0;
    d.qualifier = qualifier;
    d.other_speed = other;
    uint8_t q[10], o[64];
    usbd_plan_other_speed(&d, q, o, sizeof(o));
    assert(!memcmp(q, qualifier, 10) && !memcmp(o, other, sizeof(other)));
    usbd_plan_other_speed(&d, q, o, 20);      /* does not fit: none offered */
    assert(o[0] == 9 && o[1] == 7 && o[2] == 9 && o[4] == 0 && q[8] == 0);
    d.qualifier = NULL;
    d.other_speed = NULL;
    usbd_plan_other_speed(&d, q, o, sizeof(o));
    const uint8_t own[10] = {10, 6, 0x00, 0x02, 0, 0, 0, 64, 0, 0};
    assert(!memcmp(q, own, 10) && o[2] == 9);
    return 0;
}

static int mode_strings(void)
{
    static const uint8_t langs[4] = {4, 3, 0x09, 0x04}, german[6] = {6, 3, 'H', 0, 0xe4, 0};
    static const yoke_usbd_string_t table[] = {{0, 0, langs}, {1, 0x0407, german}, {2, 0x0409, NULL}};
    begin();
    d.strings = table;
    d.num_strings = 3;
    assert(usbd_plan_string(&d, 0, 0x1234) == langs); /* the LANGID table ignores wIndex */
    assert(usbd_plan_string(&d, 1, 0x0407) == german && !usbd_plan_string(&d, 1, 0x0409));
    assert(!usbd_plan_string(&d, 2, 0x0409));         /* cached STALL */
    char text[8];
    usb_desc_string(german, text, sizeof(text));
    assert(!strcmp(text, "H?"));
    usb_desc_string(german, text, 2);
    assert(!strcmp(text, "H"));
    return 0;
}

static int mode_lines(void)
{
    char line[160];
    usb_desc_device(device, line, sizeof(line));
    assert(!strcmp(line, "USB 2.00 046d:c31c class 00/00/00 (per-interface) ep0 64, 1 configuration"));
    begin();
    interface(0, 0, 1, 3);
    endpoint(0x81, 3, 8);
    assert(usb_desc_line(cfg, line, sizeof(line)));
    assert(!strcmp(line, "configuration 1: 1 interface, bus powered, 100 mA"));
    assert(usb_desc_line(cfg + 9, line, sizeof(line)));
    assert(!strcmp(line, " interface 0 alt 0: class 03/00/00 (HID), 1 endpoint"));
    assert(usb_desc_line(cfg + 18, line, sizeof(line)));
    assert(!strcmp(line, "  endpoint 0x81 interrupt IN, 8 bytes, interval 1"));
    const uint8_t other[4] = {4, 0x24, 0, 0};
    assert(!usb_desc_line(other, line, sizeof(line)));
    /* The walk stops at a malformed descriptor and reports it. */
    cfg[18] = 1;
    usb_walk_t w;
    usb_walk_init(&w, cfg);
    while (usb_walk_next(&w)) {}
    assert(!usb_walk_complete(&w) && w.offset == 18);
    return 0;
}

int main(int argc, char **argv)
{
    static const struct { const char *name; int (*run)(void); } modes[] = {
        {"keyboard", mode_keyboard}, {"alternates", mode_alternates},
        {"refusals", mode_refusals}, {"speeds-and-in-numbers", mode_speeds_and_in_numbers},
        {"other-speed", mode_other_speed},
        {"strings", mode_strings}, {"lines", mode_lines},
    };
    assert(argc == 2);
    for (size_t i = 0; i < sizeof(modes) / sizeof(modes[0]); ++i)
        if (!strcmp(argv[1], modes[i].name)) return modes[i].run();
    fprintf(stderr, "unknown mode %s\n", argv[1]);
    return 2;
}
