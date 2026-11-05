#pragma once
/* Device-role USB host backend: the one device on the root port, through
 * the ESP-IDF USB Host Library. Its operations are the host-controller
 * model of the device-side export core (native/yusb yusb_hc_ops_t):
 * claim, endpoint_type, asynchronous submit and cancel.
 *
 * Threading: every function runs in the USB host task and every sink call
 * is made there; other tasks enter it with yoke_usbh_post(). Statuses are
 * Linux wire errnos (USB/IP carries them): 0, -EPIPE (32, stall),
 * -ECONNRESET (104, cancelled), -ENODEV (19), -EPROTO (71), -EOVERFLOW
 * (75), -ETIME (62), -EINVAL (22), -ENOMEM (12), -EBUSY (16).
 *
 * Library limits (doc/design/USB-TIER1.md):
 *  - cancel halts and flushes the endpoint, so every URB queued on it ends
 *    with -ECONNRESET (no per-transfer cancel); control transfers cannot
 *    be cancelled and complete normally;
 *  - IN transfers are sized up to whole packets (library rule); more data
 *    than the URB asked for ends it with -EOVERFLOW;
 *  - a URB larger than YOKE_USBH_CHUNK moves in whole-packet chunks and ends
 *    at the first short chunk, as one transfer would; a control transfer
 *    carries at most YOKE_USBH_CHUNK bytes of data. */
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"

/* Transfers outstanding at once: more than the 16 reads Linux cdc-acm
 * parks once the export core's held URBs are added. Each owns a DMA buffer
 * of 8 + YOKE_USBH_CHUNK bytes from start (USB-TIER1.md): 2 KiB (32
 * full-speed packets) keeps the S3's at 8 x 4 KiB. */
#define YOKE_USBH_URBS 16u
#if CONFIG_IDF_TARGET_ESP32P4
#define YOKE_USBH_CHUNK 4096u     /* bytes per hardware transfer */
#else
#define YOKE_USBH_CHUNK 2048u
#endif

#define YOKE_USBH_EPIPE 32
#define YOKE_USBH_ECONNRESET 104
#define YOKE_USBH_ENODEV 19
#define YOKE_USBH_EPROTO 71
#define YOKE_USBH_EOVERFLOW 75
#define YOKE_USBH_ETIME 62
#define YOKE_USBH_EINVAL 22
#define YOKE_USBH_ENOMEM 12
#define YOKE_USBH_EBUSY 16

/* Host-library side effects of a successful control request, applied the
 * way the Linux USB/IP stub applies them (yusb_export.h tweaks). */
typedef enum {
    YOKE_USBH_PLAIN,
    YOKE_USBH_SET_INTERFACE,  /* setup is SET_INTERFACE: re-claim at the new alternate */
    YOKE_USBH_CLEAR_HALT,     /* setup is CLEAR_FEATURE(HALT): resume the library pipe */
} yoke_usbh_tweak_t;

typedef struct {
    uint8_t endpoint;      /* bEndpointAddress; 0x00 or 0x80: control */
    yoke_usbh_tweak_t tweak;
    uint8_t setup[8];      /* control transfers */
    uint8_t *data;         /* length bytes (OUT data, or room for IN) */
    uint32_t length;
    bool zero;             /* OUT: end with a zero-length packet */
    void *owner;           /* the caller's */
} yoke_usbh_urb_t;

typedef struct {
    void (*attached)(void *ctx);  /* a device enumerated; descriptors readable */
    void (*detached)(void *ctx);  /* after every outstanding URB completed */
    void (*complete)(void *ctx, yoke_usbh_urb_t *urb, int status, uint32_t actual);
} yoke_usbh_sink_t;

/* Install the library and start the USB host task (root port powered). */
esp_err_t yoke_usbh_start(const yoke_usbh_sink_t *sink, void *ctx);
void yoke_usbh_post(void (*fn)(void *arg), void *arg);

/* The attached device (between attached and detached), else NULL/0. */
const uint8_t *yoke_usbh_device_descriptor(void);
const uint8_t *yoke_usbh_config_descriptor(void);   /* the active configuration */
unsigned yoke_usbh_speed(void);                      /* Linux usb_device_speed */
unsigned yoke_usbh_address(void);

/* Every interface of the active configuration at alternate 0: 0 or -errno. */
int yoke_usbh_claim(void);
void yoke_usbh_release(void);
/* Transfer type of address in the current alternates (0x00/0x80: control),
 * or -1. */
int yoke_usbh_endpoint_type(uint8_t address);
/* 0: complete() follows. < 0: refused now with that status. */
int yoke_usbh_submit(yoke_usbh_urb_t *urb);
void yoke_usbh_cancel(yoke_usbh_urb_t *urb);
