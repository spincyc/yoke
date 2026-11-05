#pragma once
/* Device-role hardware self-test: whatever is plugged into the USB-A port
 * is enumerated by the library; this logs its descriptors and strings,
 * claims it, and logs every changed report of its first interrupt IN
 * endpoint (keys, mouse moves). Pure C over yoke_usbh.h (host-tested). */
#include <stdbool.h>
#include <stdint.h>
#include "yoke_usbh.h"

typedef struct {
    bool attached, claimed;
    uint16_t vendor, product;
    unsigned speed;              /* Linux usb_device_speed */
    uint8_t poll_endpoint;       /* 0: none */
    uint32_t reports, errors;
} selftest_host_status_t;

void selftest_host_init(void (*log)(const char *line));
extern const yoke_usbh_sink_t selftest_host_sink;
const selftest_host_status_t *selftest_host_status(void);
