#pragma once
/* Computer-role hardware self-test: a CDC-ACM echo device presented through
 * the mirror backend exactly as a remote device would be. It exercises the
 * paths the presentation core uses: deferred control replies (the host is
 * held off for SELFTEST_CDC_DEFER_MS, a stand-in for the network round
 * trip), OUT data, the synthesized SET_CONFIGURATION, endpoint enable and
 * bulk/interrupt transfers. Pure C over yoke_usbd.h (host-tested). */
#include "yoke_usbd.h"

#define SELFTEST_CDC_DEFER_MS 20u
#define SELFTEST_CDC_BUFFER 512u   /* one high-speed bulk packet */

typedef struct {
    /* Run fn(arg) in the USB task after ms. */
    void (*later)(void (*fn)(void *arg), void *arg, uint32_t ms);
    void (*log)(const char *line);
} selftest_cdc_port_t;

typedef struct {
    bool configured, dtr, rts;
    uint32_t baud;
    uint32_t control_requests, deferred_replies, echoed_bytes, errors;
} selftest_cdc_status_t;

/* buffer: SELFTEST_CDC_BUFFER DMA-capable bytes (yoke_usbd_alloc). serial:
 * up to 16 printable ASCII characters. */
const yoke_usbd_descriptors_t *selftest_cdc_init(const selftest_cdc_port_t *port,
                                                 yoke_usbd_speed_t speed, const char *serial,
                                                 uint8_t *buffer);
extern const yoke_usbd_sink_t selftest_cdc_sink;
const selftest_cdc_status_t *selftest_cdc_status(void);
