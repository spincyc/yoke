#pragma once
/* Device-side export core: a USB/IP 1.1.1 server over an abstract host
 * controller (ADR 0027). Tier 1/1.5 device-side boxes (ESP-IDF USB Host
 * library) and the host test servers use it; the ZERO 3E device side keeps
 * the kernel's usbip-host stub.
 *
 * One object serves one TCP connection: OP_REQ_DEVLIST (reply, then the
 * connection closes) or OP_REQ_IMPORT (claim, reply, then CMD_SUBMIT /
 * CMD_UNLINK until the client goes away). Semantics follow the stock stub
 * (drivers/usb/usbip/stub_rx.c, stub_tx.c) where a client can observe them:
 *  - every command must carry the imported devid, else the connection closes;
 *  - a URB for an endpoint outside the current configuration closes the
 *    connection (the stub desynchronizes instead; closing is the safe form);
 *  - SET_CONFIGURATION, SET_INTERFACE, CLEAR_FEATURE(ENDPOINT_HALT) and the
 *    hub port reset are passed to the backend as tweaks it applies itself;
 *  - CMD_UNLINK of a pending URB that the backend cancels yields
 *    RET_UNLINK(-ECONNRESET) and no RET_SUBMIT; a URB that completed first
 *    yields its RET_SUBMIT, then RET_UNLINK(0) (the order the stub produces
 *    when the completion wins); an unknown seqnum yields RET_UNLINK(0).
 * The ADR 0028 extension is accepted when offered and configured.
 *
 * Flow control. Per endpoint, URBs reach the backend in arrival order.
 *  - A CMD_SUBMIT without OUT data (IN, or OUT of length 0) that finds no
 *    free slot or no buffer is held (up to max_urbs of them) and parsing
 *    continues, so a CMD_UNLINK for a URB parked in a read that never
 *    completes is still read. Unlinking a held URB yields RET_UNLINK
 *    (-ECONNRESET) and no RET_SUBMIT, as the stub does for a URB it has
 *    not started.
 *  - A CMD_SUBMIT with OUT data waits for its slot and buffer: the stream
 *    stops there (rx_space returns 0), and anything behind it, a
 *    CMD_UNLINK included, is read only once an earlier URB completes.
 *  - The stream also stops while max_urbs URBs are held, or while max_urbs
 *    RET_UNLINKs for held or unknown URBs are waiting to be sent.
 *  - One slot is reserved for control transfers (when max_urbs >= 2), and
 *    the pool keeps a control buffer (YUSB_EXPORT_EP0_BYTES), so bulk and
 *    interrupt URBs cannot starve ep0.
 * Memory referenced by yusb_export_tx_iov segments stays valid and
 * unchanged until yusb_export_tx_done reports it sent, so a platform may
 * release its lock around the send.
 *
 * Sizing. A URB larger than max_urb_bytes, or than the pool can ever hold,
 * completes with -ENOMEM. Stock Linux usb-storage sends up to 120 KiB per
 * URB, so an MCU exporter with 4 KiB URBs cannot serve it (its I/O fails
 * with -ENOMEM); clients that split transfers (the Yoke proxy's
 * max_transfer) work.
 * Stack: the deepest chain (tx_done -> resumed parse -> submit -> a
 * synchronous failure -> YX reply -> LZ4) measures about 1.4 KB on x86-64
 * (gcc -O2 -fstack-usage); plan about 2.3 KB of task stack on Xtensa, plus
 * the backend's own frames and the platform vsnprintf (log messages are
 * formatted into a 200-byte buffer).
 */
#include "yusb_usbip.h"
#include "yusb_yx.h"

#ifndef YUSB_EXPORT_MAX_DEVICES
#define YUSB_EXPORT_MAX_DEVICES 4
#endif
#define YUSB_EXPORT_MAX_IFACES 32      /* yusb_export_dev_t.num_ifaces bound */
/* Control buffer kept in the pool for ep0 (control transfers longer than
 * this use the shared part). */
#ifndef YUSB_EXPORT_EP0_BYTES
#define YUSB_EXPORT_EP0_BYTES 1024u
#endif
/* pool_bytes 0: room for this many URBs of max_urb_bytes (fewer when
 * max_urbs is smaller), plus the control buffer. */
#ifndef YUSB_EXPORT_AUTO_POOL_URBS
#define YUSB_EXPORT_AUTO_POOL_URBS 8u
#endif

typedef enum {
    YUSB_TWEAK_NONE,
    YUSB_TWEAK_SET_CONFIGURATION, /* setup.value */
    YUSB_TWEAK_SET_INTERFACE,     /* setup.index (interface), setup.value (alternate) */
    YUSB_TWEAK_CLEAR_HALT,        /* setup.index (endpoint address) */
    YUSB_TWEAK_RESET_DEVICE,      /* hub-class SET_FEATURE(PORT_RESET) */
} yusb_tweak_t;

/* One URB handed to the backend. The core owns it and its buffer until
 * yusb_export_complete() returns it. */
typedef struct {
    uint32_t seqnum;      /* the client's */
    uint8_t ep;           /* endpoint number, 0..15 */
    bool dir_in;
    uint8_t type;         /* YUSB_XFER_* */
    yusb_tweak_t tweak;
    uint32_t flags;       /* USB/IP transfer flags (YUSB_URB_ZERO_PACKET ...) */
    uint32_t length;      /* transfer_buffer_length */
    int32_t interval;
    yusb_setup_t setup;   /* control transfers */
    uint8_t *data;        /* OUT filled, IN to fill; YUSB_HEADROOM before (see below) */
    void *cookie;         /* backend's */
} yusb_xurb_t;

typedef struct {
    void *ctx;
    /* Exclusive import of device index `dev`: 0, or < 0 to refuse. */
    int (*claim)(void *ctx, unsigned dev);
    void (*release)(void *ctx, unsigned dev);
    /* Transfer type of `address` (direction bit included) in the current
     * configuration, or -1. Address 0x00/0x80 is the control endpoint. */
    int (*endpoint_type)(void *ctx, unsigned dev, unsigned address);
    /* Start a transfer. 0: yusb_export_complete() follows (possibly from a
     * later call). < 0: the URB completes now with that wire status. */
    int (*submit)(void *ctx, unsigned dev, yusb_xurb_t *urb);
    /* End an outstanding transfer early; its completion still follows
     * (status -YUSB_ECONNRESET when it was cancelled). */
    void (*cancel)(void *ctx, unsigned dev, yusb_xurb_t *urb);
    yusb_log_fn log;      /* optional */
    const yusb_buffers_t *buffers; /* optional DMA layout (yusb.h) */
    /* cancel ends every URB queued on that endpoint (ESP-IDF USB Host:
     * flush). The core then resubmits, unchanged, the URBs queued behind the
     * one being unlinked (they had not started). A URB queued ahead of it
     * that the flush ends reports its real status (-ECONNRESET with the
     * bytes moved); clients that unlink only a queue's tail (the Yoke proxy,
     * Linux usb-storage and usbhid) never see that. */
    bool cancel_flushes_endpoint;
} yusb_hc_ops_t;

/* Backend notes: an IN URB's data has room for its length rounded up to
 * 1024 bytes (a backend whose controller rounds IN lengths to whole packets
 * may use it; a device sending more than length is -YUSB_EOVERFLOW); an OUT
 * URB's data holds its length. With buffers->align > 1 data is aligned and
 * the buffer extends to a multiple of align. A backend that
 * cannot apply a tweak completes it with -YUSB_EPIPE (STALL): the client
 * presents a refused SET_CONFIGURATION/SET_INTERFACE and treats a refused
 * port reset as done (⚠ the device then keeps its state). */

/* create rejects a device whose busid or path is not NUL terminated inside
 * its field (or not ASCII), or whose num_ifaces exceeds
 * YUSB_EXPORT_MAX_IFACES. */
typedef struct {
    yusb_device_info_t info;       /* path, busid, ids, speed, configuration */
    uint8_t num_ifaces;
    yusb_iface_info_t ifaces[YUSB_EXPORT_MAX_IFACES];
} yusb_export_dev_t;

typedef struct {
    uint16_t max_urbs;             /* URBs at the backend per connection, 1..1024 */
    uint32_t max_urb_bytes;        /* largest transfer accepted, <= YUSB_MAX_TRANSFER */
    yusb_yx_params_t yx;           /* extension levers/cache accepted; levers 0: plain */
    /* URB buffer budget in bytes, allocated once at create. Each URB's
     * buffer is cut from it to fit (data room as in the backend notes, plus
     * YUSB_HEADROOM, rounded to buffers->align); the first
     * YUSB_EXPORT_EP0_BYTES of room is kept for control. A full pool holds
     * URBs back (flow control above), it does not fail them. With YX
     * agreed, a bulk IN buffer also holds yusb_yx_bound(length) bytes for
     * the encoded reply when the pool can hold both (else that reply goes
     * plain). 0: automatic (YUSB_EXPORT_AUTO_POOL_URBS). */
    size_t pool_bytes;
} yusb_export_cfg_t;

void yusb_export_defaults(yusb_export_cfg_t *cfg);
/* Bytes yusb_export_create allocates for cfg (per connection), the buffer
 * pool included, and with cfg->yx's wire levers the codec state an import
 * can add (yusb_yx_enc_footprint/dec_footprint and both structs). */
size_t yusb_export_footprint(const yusb_export_cfg_t *cfg, const yusb_buffers_t *buffers);

#define YUSB_EXPORT_COUNTERS(X) \
    X(submits) X(unlinks) X(unlinked) X(ret_submits) X(in_bytes) X(out_bytes) \
    X(refused) X(violations) X(yx_raw_in) X(yx_wire_in) X(yx_raw_out) X(yx_wire_out)

typedef struct {
#define YUSB_COUNTER_FIELD(name) uint64_t name;
    YUSB_EXPORT_COUNTERS(YUSB_COUNTER_FIELD)
#undef YUSB_COUNTER_FIELD
} yusb_export_counters_t;

typedef struct yusb_export yusb_export_t;

/* devs is copied. NULL on an invalid configuration or device, or when the
 * ops lack endpoint_type, submit or cancel. */
yusb_export_t *yusb_export_create(const yusb_export_cfg_t *cfg, const yusb_hc_ops_t *ops,
                                  const yusb_export_dev_t *devs, unsigned count);
void yusb_export_destroy(yusb_export_t *x);

/* Transport, as in yusb_proxy.h. rx_space returns 0 while the core holds
 * the stream (flow control above; TCP backpressure) or once finished.
 * rx_done < 0: violation; the connection must close. */
size_t yusb_export_rx_space(yusb_export_t *x, uint8_t **buf);
int yusb_export_rx_done(yusb_export_t *x, size_t n, uint64_t now_ms);
size_t yusb_export_tx_iov(const yusb_export_t *x, yusb_iov_t *iov, size_t max);
void yusb_export_tx_done(yusb_export_t *x, size_t n);
/* Close after the pending transmit drains (devlist answered, import
 * refused, or a violation). */
bool yusb_export_finished(const yusb_export_t *x);

/* status: 0 or a negative wire errno; actual: bytes moved. */
void yusb_export_complete(yusb_export_t *x, yusb_xurb_t *urb, int status, uint32_t actual,
                          uint64_t now_ms);

/* The transport is gone: cancel outstanding URBs and release the claim
 * once they have completed. Destroy only when yusb_export_idle(). */
void yusb_export_close(yusb_export_t *x);
bool yusb_export_idle(const yusb_export_t *x);

/* Imported device index, or -1. */
int yusb_export_device(const yusb_export_t *x);
bool yusb_export_yx(const yusb_export_t *x, yusb_yx_params_t *agreed);
const yusb_export_counters_t *yusb_export_counters(const yusb_export_t *x);
