#pragma once
/* Computer-role USB device backend: presents arbitrary (mirrored) descriptors
 * through TinyUSB (esp_tinyusb 3.0.0, DWC2) with an application class driver.
 * Its operations are the Linux Raw Gadget model the presentation core uses
 * (native/yusb yusb_gadget_ops_t): asynchronous ep0 and endpoint operations,
 * each ending in exactly one ep0_done/ep_done; synchronous endpoint setup.
 *
 * Threading: yoke_usbd_start creates the USB task (TinyUSB's) once, with
 * nothing presented. Every other function runs in the USB task, and every
 * sink call is made there, never from inside a backend function
 * (completions are deferred). Other tasks enter it with yoke_usbd_post().
 *
 * What TinyUSB/DWC2 permits (doc/design/USB-TIER1.md, "Control requests"):
 *  - interface, endpoint-class and vendor requests reach the sink; the host
 *    is held off (NAK) until ep0_write/ep0_read(0)/ep0_stall, so a remote
 *    reply or STALL reaches the computer exactly;
 *  - an OUT request with data is acknowledged as soon as ep0_read(data)
 *    completes (TinyUSB queues the status stage): nothing follows it;
 *  - TinyUSB answers standard device requests itself: GET_DESCRIPTOR from
 *    the presented descriptors, SET_ADDRESS, GET/SET_CONFIGURATION (the sink
 *    still receives SET_CONFIGURATION, as CONTROL_ACKED), GET_STATUS and
 *    device features; an uncached descriptor type STALLs;
 *  - a deferred request unanswered for YOKE_USBD_CONTROL_TIMEOUT_MS STALLs
 *    (DWC2 cannot take the host's next SETUP while EP0 waits). */
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"

#define YOKE_USBD_CONTROL_TIMEOUT_MS 4000u /* below the 5 s host timeout */
#define YOKE_USBD_MAX_CONFIGS 8u

/* Values: Linux Raw Gadget usb_raw_event_type. */
typedef enum {
    YOKE_USBD_CONNECT = 1,
    YOKE_USBD_CONTROL = 2,   /* setup holds the 8-byte SETUP */
    YOKE_USBD_SUSPEND = 3,
    YOKE_USBD_RESUME = 4,
    YOKE_USBD_RESET = 5,
    YOKE_USBD_DISCONNECT = 6,
    /* Not Raw Gadget (native/yusb's YUSB_EV_CONTROL_ACKED): a SETUP without
     * a data stage whose status TinyUSB already sent (SET_CONFIGURATION).
     * Apply it; it is never answered on ep0. */
    YOKE_USBD_CONTROL_ACKED = 0x40,
} yoke_usbd_event_t;

/* Negative operation results; a non-negative result is a byte count. */
enum {
    YOKE_USBD_INTERRUPTED = -1001,  /* ep_cancel, or the host abandoned the request */
    YOKE_USBD_HALT_CLEARED = -1002, /* host CLEAR_FEATURE(ENDPOINT_HALT) on a started op */
    YOKE_USBD_SHUTDOWN = -1003,     /* bus reset, configuration change, disconnect */
    YOKE_USBD_ERROR = -1004,
};

/* Linux usb_device_speed values. */
typedef enum {
    YOKE_USBD_SPEED_UNKNOWN = 0,
    YOKE_USBD_SPEED_FULL = 2,
    YOKE_USBD_SPEED_HIGH = 3,
} yoke_usbd_speed_t;

typedef struct {
    uint8_t index;
    uint16_t lang;
    const uint8_t *data;   /* string descriptor; NULL: STALL */
} yoke_usbd_string_t;

/* Descriptors as the computer sees them, already rewritten (bMaxPacketSize0
 * 64, bus power, no remote wakeup). They stay valid while presented. */
typedef struct {
    yoke_usbd_speed_t speed;           /* FULL, or HIGH on a high-speed port */
    const uint8_t *device;             /* 18 bytes */
    uint8_t num_configs;
    const uint8_t *configs[YOKE_USBD_MAX_CONFIGS]; /* wTotalLength bytes each */
    const uint8_t *qualifier;          /* NULL: STALL (also other_speed, bos) */
    const uint8_t *other_speed;
    const uint8_t *bos;
    const yoke_usbd_string_t *strings;
    size_t num_strings;
} yoke_usbd_descriptors_t;

typedef struct {
    void (*event)(void *ctx, yoke_usbd_event_t type, const uint8_t *setup);
    void (*ep0_done)(void *ctx, int result);
    void (*ep_done)(void *ctx, int handle, int result);
} yoke_usbd_sink_t;

/* Install TinyUSB and its task, disconnected. Once, from any task. */
esp_err_t yoke_usbd_start(void);
/* Validate against this controller (endpoint numbers, IN endpoints, FIFO
 * RAM, packet sizes) and connect. Fails with a reason (why) and no bus
 * activity when the controller cannot present them. */
int yoke_usbd_present(const yoke_usbd_descriptors_t *d, const yoke_usbd_sink_t *sink,
                      void *ctx, char *why, size_t cap);
/* Disconnect (the computer sees an unplug); outstanding operations end
 * with YOKE_USBD_SHUTDOWN. */
void yoke_usbd_withdraw(void);

/* ep0, asynchronous; each answers the newest CONTROL event (a newer one
 * supersedes an unanswered one, as with Raw Gadget). ep0_read with len 0
 * acknowledges the status stage. With nothing owed (after a bus reset, or
 * once the host moved on) ep0_stall completes at once without bus effect;
 * ep0_write and ep0_read complete YOKE_USBD_INTERRUPTED. */
void yoke_usbd_ep0_write(const uint8_t *data, size_t len, bool zero);
void yoke_usbd_ep0_read(uint8_t *data, size_t len);
void yoke_usbd_ep0_stall(void);

/* Endpoints of the current configuration, synchronous: a handle >= 0 or a
 * negative errno. ep_enable resets the data toggle (SET_INTERFACE).
 * ep_disable and ep_set_halt require no operation outstanding. */
int yoke_usbd_ep_enable(const uint8_t *endpoint_descriptor);
int yoke_usbd_ep_disable(int handle);
int yoke_usbd_ep_set_halt(int handle);
/* Asynchronous, one per handle. Buffers are DMA-capable (yoke_usbd_alloc)
 * and stay valid until ep_done. zero appends a zero-length packet. */
void yoke_usbd_ep_write(int handle, uint8_t *data, size_t len, bool zero);
void yoke_usbd_ep_read(int handle, uint8_t *data, size_t len);
/* End the outstanding operation early; it still completes, with
 * YOKE_USBD_INTERRUPTED or its real result. */
void yoke_usbd_ep_cancel(int handle);

yoke_usbd_speed_t yoke_usbd_link_speed(void);
/* Non-control endpoint numbers this controller has (1..N); IN endpoints
 * only 1..*in_numbers (DWC2 gives IN endpoint n TX FIFO n). */
unsigned yoke_usbd_endpoints(unsigned *in_numbers);
/* Run fn(arg) in the USB task. */
void yoke_usbd_post(void (*fn)(void *arg), void *arg);
/* DMA-capable buffer for endpoint data (cache-line aligned on ESP32-P4). */
uint8_t *yoke_usbd_alloc(size_t size);
