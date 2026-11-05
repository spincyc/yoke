/* The device-role self-test (selftest_host.c, unchanged) against a scripted
 * USB host backend: enumeration log, string requests, claim and report
 * polling. No USB Host Library or hardware is involved. */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "selftest_host.h"

static uint8_t device[18] = {18, 1, 0x10, 0x01, 0, 0, 0, 8, 0x6d, 0x04, 0x1c, 0xc3,
                             0, 1, 1, 2, 0, 1};
static const uint8_t keyboard[34] = {
    9, 2, 34, 0, 1, 1, 0, 0xa0, 50,
    9, 4, 0, 0, 1, 3, 1, 1, 0,
    9, 0x21, 0x11, 1, 0, 1, 0x22, 65, 0,
    7, 5, 0x81, 3, 8, 0, 10,
};
static const uint8_t storage[32] = {
    9, 2, 32, 0, 1, 1, 0, 0x80, 50,
    9, 4, 0, 0, 2, 8, 6, 0x50, 0,
    7, 5, 0x81, 2, 64, 0, 0,
    7, 5, 0x02, 2, 64, 0, 0,
};
static const uint8_t *config = keyboard;
static yoke_usbh_urb_t *submitted[8];
static int submissions, claims, claim_result;
static char lines[32][160];
static int line_count;

const uint8_t *yoke_usbh_device_descriptor(void) { return device; }
const uint8_t *yoke_usbh_config_descriptor(void) { return config; }
unsigned yoke_usbh_speed(void) { return 1; }
unsigned yoke_usbh_address(void) { return 1; }
int yoke_usbh_claim(void) { claims++; return claim_result; }
int yoke_usbh_submit(yoke_usbh_urb_t *urb) { submitted[submissions++ % 8] = urb; return 0; }

static void capture(const char *line)
{
    snprintf(lines[line_count++ % 32], sizeof(lines[0]), "%s", line);
}

static bool logged(const char *text)
{
    for (int i = 0; i < line_count && i < 32; ++i)
        if (strstr(lines[i], text)) return true;
    return false;
}

static yoke_usbh_urb_t *last(void) { return submitted[(submissions - 1) % 8]; }

static void answer(const uint8_t *data, uint32_t length, int status)
{
    yoke_usbh_urb_t *urb = last();
    memcpy(urb->data, data, length);
    selftest_host_sink.complete(NULL, urb, status, length);
}

static void string(const char *text)
{
    uint8_t desc[64] = {0, 3};
    size_t n = strlen(text);
    for (size_t i = 0; i < n; ++i) desc[2 + 2 * i] = (uint8_t)text[i];
    desc[0] = (uint8_t)(2 + 2 * n);
    yoke_usbh_urb_t *urb = last();
    assert(urb->endpoint == 0 && urb->setup[0] == 0x80 && urb->setup[1] == 6 && urb->setup[3] == 3);
    answer(desc, desc[0], 0);
}

static int mode_keyboard(void)
{
    selftest_host_init(capture);
    selftest_host_sink.attached(NULL);
    assert(logged("device 1 attached at low speed: USB 1.10 046d:c31c"));
    assert(logged(" interface 0 alt 0: class 03/01/01 (HID), 1 endpoint"));
    assert(logged("  endpoint 0x81 interrupt IN, 8 bytes, interval 10"));
    assert(submissions == 1 && last()->setup[2] == 0);         /* LANGIDs first */
    const uint8_t langs[4] = {4, 3, 0x09, 0x04};
    answer(langs, 4, 0);
    assert(last()->setup[2] == 1 && last()->setup[4] == 0x09); /* iManufacturer, en-US */
    string("Logitech");
    assert(last()->setup[2] == 2);                             /* iProduct; no serial */
    string("USB Receiver");
    assert(logged("manufacturer: \"Logitech\"") && logged("product: \"USB Receiver\""));
    assert(claims == 1 && last()->endpoint == 0x81 && last()->length == 8);
    assert(selftest_host_status()->poll_endpoint == 0x81);
    const uint8_t key_a[8] = {0, 0, 4, 0, 0, 0, 0, 0}, none[8] = {0};
    int before = line_count;
    answer(key_a, 8, 0);
    answer(key_a, 8, 0);                                       /* unchanged: not logged */
    answer(none, 8, 0);
    assert(line_count == before + 2 && logged("report 1 (8 bytes): 00 00 04 00 00 00 00 00"));
    assert(selftest_host_status()->reports == 3 && submissions == 3 + 3 + 1);
    answer(none, 0, -32);                                      /* -EPIPE */
    assert(selftest_host_status()->errors == 1 && logged("endpoint 0x81 stopped (-32)"));
    assert(submissions == 7 && !selftest_host_status()->poll_endpoint);
    selftest_host_sink.detached(NULL);
    assert(!selftest_host_status()->attached && logged("detached after 3 reports"));
    return 0;
}

static int mode_quiet_device(void)
{
    /* No strings, no interrupt IN, a failed string answer and a claim error. */
    device[14] = device[15] = 0;
    config = storage;
    selftest_host_init(capture);
    selftest_host_sink.attached(NULL);
    assert(submissions == 0 && claims == 1);
    assert(logged("no interrupt IN endpoint to poll"));
    assert(logged(" interface 0 alt 0: class 08/06/50 (mass storage), 2 endpoints"));
    device[15] = 2;
    claim_result = -16;
    selftest_host_sink.attached(NULL);
    answer(device, 0, -32);                                    /* LANGIDs stalled */
    assert(logged("string 0: no answer (-32)"));
    answer(device, 0, -71);                                    /* product failed */
    assert(logged("claim failed (-16)") && !selftest_host_status()->claimed);
    return 0;
}

int main(int argc, char **argv)
{
    assert(argc == 2);
    if (!strcmp(argv[1], "keyboard")) return mode_keyboard();
    if (!strcmp(argv[1], "quiet-device")) return mode_quiet_device();
    return 2;
}
