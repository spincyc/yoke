#pragma once
/* Yoke portable USB core: definitions shared by every module (ADR 0027).
 *
 * Every module is sans-I/O: no sockets, clocks, threads, signals or files.
 * The platform (Linux worker, ESP-IDF firmware, host tests) owns those and
 * drives each core object through plain function calls:
 *
 *  - Time is an explicit monotonic millisecond count passed by the caller.
 *  - Bytes in: the core names the buffer for the next received bytes
 *    (*_rx_space) and the platform reports how many arrived (*_rx_done).
 *    Payloads land directly in their final buffer (no copy).
 *  - Bytes out: the core exposes pending transmit segments (*_tx_iov) and
 *    the platform reports how many were written (*_tx_done).
 *  - Hardware: small operation tables (yusb_gadget_ops_t, yusb_hc_ops_t).
 *    An operation either completes synchronously or reports its end later
 *    through the matching *_done or *_complete call. Operations never call
 *    back into the core object that invoked them.
 *
 * A core object is not thread-safe: the platform serializes all calls to
 * one object (one mutex or one task). Memory is allocated with calloc only
 * at create, configuration change and destroy, never per transfer.
 */
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define YUSB_VERSION_MAJOR 0
#define YUSB_VERSION_MINOR 1

/* Writable bytes every data buffer handed to a backend reserves before its
 * payload, so a backend can build its request header in place without a
 * copy (Linux Raw Gadget: struct usb_raw_ep_io is 8 bytes). */
#define YUSB_HEADROOM 8u

typedef enum {
    YUSB_OK = 0,
    YUSB_ERR_PROTOCOL = -1,    /* malformed or unexpected peer bytes: close */
    YUSB_ERR_RANGE = -2,       /* a value outside its documented range */
    YUSB_ERR_DESCRIPTOR = -3,  /* malformed descriptor from the device */
    YUSB_ERR_UNSUPPORTED = -4, /* well-formed, explicitly not presentable */
    YUSB_ERR_NOMEM = -5,       /* a configured bound or allocation failed */
    YUSB_ERR_STATE = -6,       /* call not valid in the current state */
} yusb_err_t;

const char *yusb_strerror(int err);

/* One contiguous transmit segment; maps 1:1 onto struct iovec. */
typedef struct {
    const uint8_t *base;
    size_t len;
} yusb_iov_t;

/* Optional allocator for buffers a core hands to a backend, for DMA
 * engines with alignment rules (ESP32-P4 USB: 64-byte aligned, whole cache
 * lines). With align > 1, every data pointer the core passes to a backend
 * (ep0, endpoint and export URB data) is aligned to `align`, the buffer
 * extends to a multiple of `align` past the largest transfer it can carry,
 * and YUSB_HEADROOM writable bytes still precede it. alloc returns zeroed,
 * `align`-aligned memory of `size` bytes (a multiple of align); NULL
 * alloc/free use aligned_alloc/free. A NULL table or align <= 1 means plain
 * calloc. Used only at create and configuration change. */
typedef struct {
    size_t align;                    /* a power of two */
    void *(*alloc)(void *ctx, size_t size, size_t align);
    void (*free)(void *ctx, void *ptr);
    void *ctx;
} yusb_buffers_t;

/* Optional diagnostic sink; may be NULL. Messages are short and bounded. */
typedef void (*yusb_log_fn)(void *ctx, const char *message);
