#pragma once
/* Computer-side presentation core: one imported USB/IP device presented
 * through a gadget backend (reference: src/yoke/usb/{client,proxy,session}.py,
 * ADRs 0021, 0022, 0027).
 *
 * One object is one session: OP_REQ_IMPORT, descriptor fetch, then
 * presentation until it ends. The Python reference runs one thread per
 * endpoint with blocking calls; this core is the same behavior as explicit
 * state machines driven by events, so it also runs on an event-driven MCU
 * device stack (TinyUSB). Packet semantics are exact by construction:
 *  - without a class hint a pump moves one (high-bandwidth) packet per URB;
 *  - IN pumps keep several URBs outstanding and coalesce full completions
 *    into one gadget write (the UDC re-packetizes identically);
 *  - Bulk-Only Transport interfaces size URBs from each CBW;
 *  - a remote STALL is reproduced locally (partial data, unlink later
 *    URBs, halt, clear the remote halt once, probe; park with backoff);
 *  - a host CLEAR_FEATURE(HALT) seen as YUSB_GADGET_HALT_CLEARED on a
 *    started request is mirrored to the device;
 *  - unlinked sequence numbers are tombstoned so a late RET_SUBMIT (stub_tx
 *    order) never drops the connection.
 * Differences from the Python reference are listed in native/README.md.
 *
 * Driving it (all calls serialized by the platform):
 *
 *   p = yusb_proxy_create(&policy, &ops, "1-1", now);   // queues OP_REQ_IMPORT
 *   loop: recv into yusb_proxy_rx_space(), report with yusb_proxy_rx_done();
 *         write yusb_proxy_tx_iov() segments, report with yusb_proxy_tx_done();
 *         call yusb_proxy_poll() at its deadline;
 *         on YUSB_PHASE_READY: bind the gadget at presentation->speed, then
 *           yusb_proxy_gadget_started(); feed yusb_proxy_event(),
 *           yusb_proxy_ep0_done(), yusb_proxy_ep_done();
 *         on YUSB_PHASE_ENDED (or to detach): finish any receive or
 *           transmit in progress (shut the socket down), then
 *           yusb_proxy_stop(); when YUSB_PHASE_STOPPED, close the
 *           transport and unbind the gadget.
 * Buffers named by rx_space/tx_iov stay valid until rx_done/tx_done or
 * yusb_proxy_stop, whichever comes first.
 */
#include "yusb_policy.h"
#include "yusb_usbip.h"
#include "yusb_yx.h"

/* Gadget events; values match Raw Gadget usb_raw_event_type. */
typedef enum {
    YUSB_EV_CONNECT = 1,
    YUSB_EV_CONTROL = 2,
    YUSB_EV_SUSPEND = 3,
    YUSB_EV_RESUME = 4,
    YUSB_EV_RESET = 5,
    YUSB_EV_DISCONNECT = 6,
    /* Not Raw Gadget: a SETUP without a data stage whose status stage the
     * backend already completed on the bus (TinyUSB acks SET_CONFIGURATION
     * itself). The core forwards it and applies its effect as usual, but
     * never issues an ep0 operation for it and never abandons it; a refusal
     * by the device is only counted (late_out_stalls). With wLength != 0 it
     * is treated as YUSB_EV_CONTROL. */
    YUSB_EV_CONTROL_ACKED = 0x40,
} yusb_event_t;

/* Negative results of ep0/endpoint operations; backends map their errno.
 * A non-negative result is the byte count. */
typedef enum {
    YUSB_GADGET_INTERRUPTED = -1001,   /* cancelled (ep_cancel, signal: EINTR) */
    YUSB_GADGET_HALT_CLEARED = -1002,  /* host cleared the halt of a started request (dwc3 -EPIPE) */
    YUSB_GADGET_SHUTDOWN = -1003,      /* bus reset, disconnect, disable (ESHUTDOWN, ECONNRESET, ENODEV) */
    YUSB_GADGET_ERROR = -1004,         /* anything else */
} yusb_gadget_result_t;

/* Gadget backend. Asynchronous operations start an operation and return;
 * the platform reports its end with yusb_proxy_ep0_done/yusb_proxy_ep_done.
 * At most one ep0 operation and one operation per endpoint handle are
 * outstanding. Buffers are core-owned and carry YUSB_HEADROOM writable
 * bytes before `data`. Synchronous operations must not block on the bus. */
typedef struct {
    void *ctx;
    /* ep0, asynchronous. ep0_read with len 0 completes the status stage. */
    void (*ep0_write)(void *ctx, uint8_t *data, size_t len, bool zero);
    void (*ep0_read)(void *ctx, uint8_t *data, size_t len);
    void (*ep0_stall)(void *ctx);
    /* Endpoints, synchronous: a handle >= 0 or a negative errno. The core
     * calls ep_disable/ep_set_halt only with no operation outstanding on
     * that handle (Raw Gadget refuses otherwise). */
    int (*ep_enable)(void *ctx, const yusb_endpoint_t *ep);
    int (*ep_disable)(void *ctx, int handle);
    int (*ep_set_halt)(void *ctx, int handle);
    int (*configure)(void *ctx);
    int (*vbus_draw)(void *ctx, unsigned milliamps);
    /* The speed the computer enumerated, or YUSB_SPEED_UNKNOWN. */
    yusb_speed_t (*link_speed)(void *ctx);
    /* Endpoints, asynchronous. ep_write with zero appends a ZLP. */
    void (*ep_write)(void *ctx, int handle, uint8_t *data, size_t len, bool zero);
    void (*ep_read)(void *ctx, int handle, uint8_t *data, size_t len);
    /* Ask the outstanding operation on handle to end early; its completion
     * is still reported (YUSB_GADGET_INTERRUPTED, or its real result). */
    void (*ep_cancel)(void *ctx, int handle);
    yusb_log_fn log;            /* optional */
    const yusb_buffers_t *buffers; /* optional DMA layout (yusb.h) */
} yusb_gadget_ops_t;

/* Backend guarantees and tolerances (TinyUSB, Raw Gadget):
 *  - ep_cancel is issued only while stopping a pump before an event that
 *    resets the data toggle anyway: bus reset, disconnect,
 *    SET_CONFIGURATION, SET_INTERFACE or teardown. A backend may implement
 *    it by re-activating the endpoint (toggle back to DATA0).
 *  - ep0 ordering. Every ep0 operation the core issues (ep0_write,
 *    ep0_read, ep0_stall) answers the most recent SETUP the backend
 *    delivered, never an earlier one: once a newer SETUP is delivered, the
 *    core issues nothing more for older requests. An operation already
 *    outstanding when a SETUP arrives still completes through
 *    yusb_proxy_ep0_done as usual.
 *  - A request whose status stage the UDC completed itself must be
 *    delivered as YUSB_EV_CONTROL_ACKED: the core never issues an ep0
 *    operation for it, and serves later SETUPs once its effect is applied.
 *    Every UDC acks OUT-with-data when the data arrives; after the data
 *    stage's ep0_read the core issues nothing more for that request.
 *  - A newer SETUP abandons a request that is still owed its answer (it is
 *    unlinked), except SET_CONFIGURATION and SET_INTERFACE, which run to
 *    completion without an answer; SETUPs wait while an acked request or an
 *    outstanding ep0 operation finishes. A waiting SETUP followed by a newer
 *    one is dropped unless it was delivered as YUSB_EV_CONTROL_ACKED.
 *    Acknowledged requests are never dropped: more than YUSB_E0_QUEUE (16)
 *    of them waiting at once ends the session (YUSB_END_FAULT).
 *  - After YUSB_EV_RESET or YUSB_EV_DISCONNECT the core may issue one
 *    ep0_stall, for the most recent SETUP if it is still owed (Raw Gadget
 *    keeps it pending across the reset and refuses later SETUPs until ep0
 *    answers); a SETUP delivered before that STALL is issued cancels it. A
 *    backend with nothing pending completes it at once without bus effect.
 *  - A backend that serves device-recipient GET_DESCRIPTOR itself answers
 *    from yusb_proxy_presentation() with yusb_presentation_lookup(): device,
 *    configurations, qualifier, other-speed, BOS (or their STALL), the
 *    language table and every string the descriptors reference in up to
 *    four languages, plus string 0xEE when policy.prefetch_os_string.
 *  - A backend that stalls an unanswered control request itself should
 *    allow more than policy.control_timeout_ms (default 4.5 s), or lower
 *    that timeout below its own (TinyUSB: 3.5 s against its 4 s). */

typedef enum {
    YUSB_PHASE_IMPORT,      /* OP_REQ_IMPORT queued; awaiting OP_REP_IMPORT */
    YUSB_PHASE_FETCH,       /* fetching descriptors through control URBs */
    YUSB_PHASE_READY,       /* bind the gadget, then yusb_proxy_gadget_started() */
    YUSB_PHASE_PRESENTING,  /* serving the computer */
    YUSB_PHASE_ENDED,       /* see status.end; call yusb_proxy_stop() */
    YUSB_PHASE_STOPPING,    /* waiting for outstanding gadget operations */
    YUSB_PHASE_STOPPED,     /* close the transport, then unbind the gadget */
} yusb_phase_t;

typedef enum {
    YUSB_END_NONE,
    YUSB_END_STOPPED,        /* yusb_proxy_stop() by the owner */
    YUSB_END_REMOTE_LOST,    /* transport closed, protocol error or device gone */
    YUSB_END_IMPORT_REFUSED, /* exporter refused OP_REQ_IMPORT (retry) */
    YUSB_END_UNSUPPORTED,    /* device cannot be presented (slow retry) */
    YUSB_END_DESCRIPTOR,     /* malformed descriptors or failed fetch (retry) */
    YUSB_END_TIMEOUT,        /* import or descriptor fetch timed out (retry) */
    YUSB_END_REJECTED,       /* UDC lacks endpoints or link too slow (slow retry) */
    YUSB_END_FAULT,          /* gadget failure or unconfirmed unlink */
    YUSB_END_YX_UNAVAILABLE, /* agreed YX codec could not be allocated: reconnect
                                with policy.yx_levers = 0 (no offer) */
} yusb_end_t;

/* X-macro over every counter; the names are the Python reference's
 * Counters fields (status JSON keys) plus native additions at the end. */
#define YUSB_PROXY_COUNTERS(X) \
    X(control) X(cached) X(forwarded) X(local_stalls) X(remote_stalls) \
    X(remote_clears) X(host_clear_halts) X(parked) X(transaction_errors) \
    X(late_out_stalls) X(dropped_out) X(bus_resets) X(remote_resets) \
    X(disconnects) X(in_urbs) X(out_urbs) X(in_bytes) X(out_bytes) \
    X(gadget_writes) X(gadget_reads) X(late_ret_submits) X(unlinks) \
    X(yx_raw_in) X(yx_wire_in) X(yx_raw_out) X(yx_wire_out)

typedef struct {
#define YUSB_COUNTER_FIELD(name) uint64_t name;
    YUSB_PROXY_COUNTERS(YUSB_COUNTER_FIELD)
#undef YUSB_COUNTER_FIELD
} yusb_proxy_counters_t;

typedef struct {
    /* "presenting", "attached", "default", "addressed", "configured",
     * "suspended" (configured and suspended) or "disconnected" */
    const char *state;
    uint8_t configuration;
    uint16_t vendor, product;
    yusb_speed_t remote_speed, presented_speed, link_speed;
    yusb_end_t end;
    const char *end_reason;  /* "" until ended */
    const char *error;       /* "" or the last fault */
    const char *rejected;    /* "" or why the device cannot be presented */
    bool yx;                 /* ADR 0028 extension negotiated */
    yusb_yx_params_t yx_params;
} yusb_proxy_status_t;

typedef struct yusb_proxy yusb_proxy_t;

/* What yusb_proxy_create and a configuration will allocate for `policy`
 * (and the ops' buffers hook), so firmware can budget before creating. */
yusb_footprint_t yusb_proxy_footprint(const yusb_policy_t *policy, const yusb_buffers_t *buffers);

/* NULL on an invalid policy or busid (yusb_policy_check). */
yusb_proxy_t *yusb_proxy_create(const yusb_policy_t *policy, const yusb_gadget_ops_t *ops,
                                const char *busid, uint64_t now_ms);
void yusb_proxy_destroy(yusb_proxy_t *p);

yusb_phase_t yusb_proxy_phase(const yusb_proxy_t *p);
/* Valid from YUSB_PHASE_READY; presentation->speed is the bind speed. */
const yusb_presentation_t *yusb_proxy_presentation(const yusb_proxy_t *p);
const yusb_device_info_t *yusb_proxy_device_info(const yusb_proxy_t *p);
/* The gadget is bound. eps/count: Raw Gadget EPS_INFO, or NULL/0 when
 * unavailable. A UDC lacking a needed endpoint ends the session
 * (YUSB_END_REJECTED, "UDC lacks endpoints: 0x03"). */
void yusb_proxy_gadget_started(yusb_proxy_t *p, const yusb_udc_ep_t *eps, size_t count,
                               uint64_t now_ms);

/* Transport. rx_space names where the next received bytes go (payloads
 * land in their final buffer) and returns how many fit; 0 once ended.
 * rx_done < 0: protocol error, the session has ended. */
size_t yusb_proxy_rx_space(yusb_proxy_t *p, uint8_t **buf);
/* The last rx_space named a payload's own buffer, all of which is required
 * next: the platform may wait for every byte (MSG_WAITALL, one call). */
bool yusb_proxy_rx_direct(const yusb_proxy_t *p);
int yusb_proxy_rx_done(yusb_proxy_t *p, size_t n, uint64_t now_ms);
/* Pending transmit segments in order; returns how many were filled. The
 * memory stays valid until reported with yusb_proxy_tx_done. */
size_t yusb_proxy_tx_iov(const yusb_proxy_t *p, yusb_iov_t *iov, size_t max);
void yusb_proxy_tx_done(yusb_proxy_t *p, size_t n);
/* EOF or a socket error: ends the session as YUSB_END_REMOTE_LOST. */
void yusb_proxy_transport_lost(yusb_proxy_t *p, const char *why, uint64_t now_ms);

/* Gadget inputs. setup is the 8-byte SETUP for YUSB_EV_CONTROL. */
void yusb_proxy_event(yusb_proxy_t *p, yusb_event_t type, const uint8_t *setup,
                      uint64_t now_ms);
void yusb_proxy_ep0_done(yusb_proxy_t *p, int result, uint64_t now_ms);
void yusb_proxy_ep_done(yusb_proxy_t *p, int handle, int result, uint64_t now_ms);

/* Timers: run due work; returns the next deadline (UINT64_MAX: none). */
uint64_t yusb_proxy_poll(yusb_proxy_t *p, uint64_t now_ms);

/* Begin teardown: cancel outstanding gadget operations; when they have all
 * completed, disable the endpoints and enter YUSB_PHASE_STOPPED. Without a
 * gadget (before READY) it stops at once. Idempotent. If an operation does
 * not complete within stop_timeout_ms the phase stays STOPPING and
 * yusb_proxy_teardown_overdue() turns true: the platform must keep the
 * gadget (the fence holds) and report the error. */
void yusb_proxy_stop(yusb_proxy_t *p, uint64_t now_ms);
bool yusb_proxy_teardown_overdue(const yusb_proxy_t *p, uint64_t now_ms);

void yusb_proxy_status(const yusb_proxy_t *p, yusb_proxy_status_t *out);
const yusb_proxy_counters_t *yusb_proxy_counters(const yusb_proxy_t *p);
