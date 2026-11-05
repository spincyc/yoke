#include "usbd_core.h"
#include <errno.h>
#include <string.h>

#define SLOTS 32u               /* endpoint numbers 0..15, both directions */
#define MAX_PACKETS 127u        /* DWC2's packet counter: 7 bits on the S3 (8 on the P4's HS port) */
#define REQ_CLEAR_FEATURE 1u
#define REQ_SET_FEATURE 3u
#define REQ_SET_CONFIGURATION 9u
#define FEATURE_ENDPOINT_HALT 0u

/* ep0: WAIT holds the host off (nothing armed, DWC2 NAKs); DATA and STATUS
 * have a stage armed. TinyUSB sends the status of OUT data and of
 * SET_CONFIGURATION itself: nothing is owed after those. */
typedef enum { EP0_IDLE, EP0_WAIT, EP0_DATA, EP0_STATUS } ep0_state_t;
/* CANCEL: aborted in hardware; a fence posted behind any completion the
 * controller already queued decides between that real result and `fence`. */
typedef enum { EP_FREE, EP_IDLE, EP_BUSY, EP_HALT_WAIT, EP_CANCEL } ep_state_t;

typedef struct {
    ep_state_t state;
    uint8_t desc[7];        /* the enabled endpoint descriptor */
    const uint8_t *fresh;   /* alternate 0 descriptor open() activated, not yet enabled */
    bool halted, in, zero, zlp;
    uint8_t *data;
    size_t len, done;
    uint16_t chunk;
    int fence;
    uint32_t aborted;       /* abort sequence number */
    bool pending;           /* completion awaiting delivery */
    int result;
} ep_t;

static struct {
    const usbd_port_t *port;
    usbd_limits_t limits;
    const yoke_usbd_descriptors_t *d;
    const yoke_usbd_sink_t *sink;
    void *ctx;
    int config;             /* active configuration index, -1 */
    ep0_state_t ep0;
    const uint8_t *live;    /* usbd.c's copy of the current SETUP */
    uint8_t setup[8];
    uint16_t ep0_len;
    uint32_t deadline;
    bool ep0_pending;
    int ep0_result;
    bool drain_posted, fence_posted;
    uint32_t aborts;
    ep_t ep[SLOTS];
} s;

static unsigned slot_of(uint8_t address) { return (address & 0x0fu) | (address & 0x80u) >> 3; }
static uint16_t packet_size(const uint8_t *desc) { return usb_le16(desc + 4) & 0x7ffu; }

/* ---- deferred completions: never delivered inside an operation ---- */

static void deliver(void)
{
    for (bool again = true; again && s.sink;) {
        again = false;
        if (s.ep0_pending) {
            s.ep0_pending = false;
            s.sink->ep0_done(s.ctx, s.ep0_result);
            again = true;
            continue;
        }
        for (unsigned i = 0; i < SLOTS && !again; ++i) {
            if (!s.ep[i].pending) continue;
            s.ep[i].pending = false;
            s.sink->ep_done(s.ctx, (int)i, s.ep[i].result);
            again = true;
        }
    }
}

static void drain(void *arg)
{
    (void)arg;
    s.drain_posted = false;
    deliver();
}

static void schedule(void)
{
    if (s.drain_posted) return;
    s.drain_posted = true;
    s.port->post(drain, NULL);
}

static void complete_ep0(int result)
{
    s.ep0_pending = true;
    s.ep0_result = result;
    schedule();
}

static void complete_ep(unsigned slot, int result)
{
    ep_t *e = &s.ep[slot];
    if (e->state != EP_FREE) e->state = EP_IDLE;
    e->pending = true;
    e->result = result;
    schedule();
}

/* Events flush earlier completions first, so the sink sees them in order. */
static void emit(yoke_usbd_event_t event, const uint8_t *setup)
{
    deliver();
    if (s.sink) s.sink->event(s.ctx, event, setup);
}

static void void_all(int result)
{
    if (s.ep0 == EP0_DATA || s.ep0 == EP0_STATUS) complete_ep0(result);
    s.ep0 = EP0_IDLE;
    for (unsigned i = 0; i < SLOTS; ++i) {
        ep_t *e = &s.ep[i];
        bool outstanding = e->state == EP_BUSY || e->state == EP_HALT_WAIT ||
                           e->state == EP_CANCEL;
        bool pending = e->pending;
        int previous = e->result;
        *e = (ep_t){0};
        if (outstanding) complete_ep(i, result);
        else if (pending) { e->pending = true; e->result = previous; }
    }
    s.config = -1;
}

void usbd_core_init(const usbd_port_t *port, const usbd_limits_t *limits)
{
    memset(&s, 0, sizeof(s));
    s.port = port;
    s.limits = *limits;
    s.config = -1;
}

const usbd_limits_t *usbd_core_limits(void) { return &s.limits; }

void usbd_core_present(const yoke_usbd_descriptors_t *d, const yoke_usbd_sink_t *sink,
                       void *ctx)
{
    s.d = d;
    s.sink = sink;
    s.ctx = ctx;
}

const yoke_usbd_descriptors_t *usbd_core_descriptors(void) { return s.d; }

/* ---- class driver inputs ---- */

uint16_t usbd_core_open(const uint8_t *interface_descriptor, uint16_t max_len)
{
    int index = s.d ? usbd_plan_config_of(s.d, interface_descriptor) : -1;
    if (index < 0) return 0;
    const uint8_t *cfg = s.d->configs[index];
    usbd_fifo_t fifos[SLOTS];
    size_t count = usbd_plan_fifos(cfg, fifos, SLOTS);
    for (size_t i = 0; i < count; ++i)
        if (!s.port->fifo_alloc(fifos[i].address, fifos[i].max_packet)) return 0;
    /* Alternate 0 is live at once: TinyUSB acknowledged SET_CONFIGURATION
     * already, so the host must see NAK (not silence) until the sink arms. */
    usb_walk_t w;
    usb_walk_init(&w, cfg);
    for (const uint8_t *p; (p = usb_walk_next(&w));) {
        if (p[1] != USB_DT_ENDPOINT || w.alternate != 0) continue;
        if (!s.port->activate(p)) return 0;
        s.ep[slot_of(p[2])].fresh = p;
    }
    s.config = index;
    return max_len; /* the whole configuration (see usbd_plan.h) */
}

void usbd_core_reset(void)
{
    void_all(YOKE_USBD_SHUTDOWN);
}

static void post_fence(void);

/* One fence post at a time (the port's queue is shared with the controller's
 * events): it ends the aborts made before it was posted, whose real
 * completions the controller queued ahead of it, and follows later aborts
 * with a new post. */
static void fence(void *arg)
{
    uint32_t upto = (uint32_t)(uintptr_t)arg;
    bool later = false;
    s.fence_posted = false;
    for (unsigned i = 0; i < SLOTS; ++i) {
        ep_t *e = &s.ep[i];
        if (e->state != EP_CANCEL) continue;
        if ((int32_t)(upto - e->aborted) >= 0) complete_ep(i, e->fence);
        else later = true;
    }
    if (later) post_fence();
}

static void post_fence(void)
{
    if (s.fence_posted) return;
    s.fence_posted = true;
    s.port->post(fence, (void *)(uintptr_t)s.aborts);
}

/* Abort an armed transfer. DWC2 aborts by re-activating the endpoint, which
 * also resets DATA0 (see USB-TIER1.md); a completion the controller queued
 * first still wins over the fence. */
static void abort_op(unsigned slot, int result)
{
    ep_t *e = &s.ep[slot];
    (void)s.port->activate(e->desc);
    e->state = EP_CANCEL;
    e->fence = result;
    e->aborted = ++s.aborts;
    post_fence();
}

static void host_halt(uint8_t address, bool set)
{
    ep_t *e = &s.ep[slot_of(address)];
    if (set) {
        /* usbd stalled the endpoint, aborting any armed transfer. */
        e->halted = true;
        if (e->state == EP_BUSY) e->state = EP_HALT_WAIT;
        return;
    }
    /* usbd cleared the stall and reset DATA0; a started operation ends
     * with HALT_CLEARED (the dwc3 -EPIPE the presentation core expects). */
    e->halted = false;
    if (e->state == EP_HALT_WAIT) {
        complete_ep(slot_of(address), YOKE_USBD_HALT_CLEARED);
    } else if (e->state == EP_BUSY) {
        abort_op(slot_of(address), YOKE_USBD_HALT_CLEARED);
    }
}

static bool current(void) { return s.live && !memcmp(s.live, s.setup, 8); }

bool usbd_core_control(uint8_t stage, const uint8_t *request)
{
    if (stage == USBD_STAGE_SETUP) {
        if ((request[0] & 0x7fu) == 0x02u &&
            (request[1] == REQ_CLEAR_FEATURE || request[1] == REQ_SET_FEATURE) &&
            usb_le16(request + 2) == FEATURE_ENDPOINT_HALT) {
            host_halt(request[4], request[1] == REQ_SET_FEATURE);
            return true; /* usbd sends the status stage itself */
        }
        if (!s.sink) return false;
        if (s.ep0 == EP0_DATA || s.ep0 == EP0_STATUS) complete_ep0(YOKE_USBD_INTERRUPTED);
        s.live = request;
        memcpy(s.setup, request, sizeof(s.setup));
        s.ep0 = EP0_WAIT;
        s.deadline = s.port->now_ms() + YOKE_USBD_CONTROL_TIMEOUT_MS;
        emit(YOKE_USBD_CONTROL, s.setup);
        return true;
    }
    if (request != s.live || !current()) return true;
    if (stage == USBD_STAGE_DATA && s.ep0 == EP0_DATA) {
        complete_ep0(s.ep0_len);
        /* IN: usbd now runs the status stage. OUT: returning true makes
         * usbd send the status ZLP, so the request is acknowledged. */
        s.ep0 = EP0_IDLE;
    } else if (stage == USBD_STAGE_ACK && s.ep0 == EP0_STATUS) {
        complete_ep0(0);
        s.ep0 = EP0_IDLE;
    }
    return true;
}

static void arm(unsigned slot)
{
    ep_t *e = &s.ep[slot];
    uint16_t mps = packet_size(e->desc);
    size_t n = e->zlp ? 0 : e->len - e->done;
    /* Whole packets per chunk, within the packet counter and TinyUSB's
     * 16-bit length (dcd_dwc2 programs a transfer without splitting it). */
    size_t max_chunk = 0xffffu - 0xffffu % mps;
    if (max_chunk > MAX_PACKETS * mps) max_chunk = MAX_PACKETS * mps;
    if (n > max_chunk) n = max_chunk;
    e->chunk = (uint16_t)n;
    e->state = EP_BUSY;
    if (!s.port->xfer(e->desc[2], e->data + e->done, e->chunk)) complete_ep(slot, YOKE_USBD_ERROR);
}

void usbd_core_xfer_done(uint8_t address, bool ok, uint32_t length)
{
    unsigned slot = slot_of(address);
    ep_t *e = &s.ep[slot];
    if (e->state == EP_CANCEL) { /* it finished before the abort took effect */
        complete_ep(slot, ok ? (int)(e->done + length) : YOKE_USBD_ERROR);
        return;
    }
    if (e->state != EP_BUSY) return; /* aborted: a stale completion */
    if (!ok) { complete_ep(slot, YOKE_USBD_ERROR); return; }
    e->done += length;
    bool more = e->in ? !e->zlp && e->done < e->len : length == e->chunk && e->done < e->len;
    if (!more && e->in && !e->zlp && e->zero && e->len && e->len % packet_size(e->desc) == 0) {
        e->zlp = true;
        more = true;
    }
    if (more) arm(slot);
    else complete_ep(slot, (int)e->done);
}

void usbd_core_configured(void)
{
    if (s.ep0 == EP0_DATA || s.ep0 == EP0_STATUS) complete_ep0(YOKE_USBD_INTERRUPTED);
    uint8_t value = s.config >= 0 ? s.d->configs[s.config][5] : 0;
    const uint8_t setup[8] = {0x00, REQ_SET_CONFIGURATION, value, 0, 0, 0, 0, 0};
    memcpy(s.setup, setup, sizeof(setup));
    s.live = NULL;
    s.ep0 = EP0_IDLE;
    emit(YOKE_USBD_CONTROL_ACKED, s.setup);
}

void usbd_core_bus(yoke_usbd_event_t event)
{
    if (event == YOKE_USBD_RESET || event == YOKE_USBD_DISCONNECT) void_all(YOKE_USBD_SHUTDOWN);
    emit(event, NULL);
}

void usbd_core_withdraw(void)
{
    void_all(YOKE_USBD_SHUTDOWN);
    s.d = NULL;
}

/* The host abandoned an armed request for a newer one usbd answered itself
 * (no DATA or ACK stage follows): end it. */
static void drop_abandoned(void)
{
    if ((s.ep0 != EP0_DATA && s.ep0 != EP0_STATUS) || current()) return;
    complete_ep0(YOKE_USBD_INTERRUPTED);
    s.ep0 = EP0_IDLE;
}

void usbd_core_tick(void)
{
    drop_abandoned();
    if (s.ep0 != EP0_WAIT || (int32_t)(s.port->now_ms() - s.deadline) < 0) return;
    if (current()) s.port->control_stall();
    s.ep0 = EP0_IDLE;
}

/* ---- yoke_usbd.h operations ---- */

static bool ep0_live(void)
{
    drop_abandoned();
    if (s.ep0 == EP0_WAIT && !current()) s.ep0 = EP0_IDLE; /* usbd answered a newer SETUP */
    return s.ep0 != EP0_IDLE;
}

void yoke_usbd_ep0_write(const uint8_t *data, size_t len, bool zero)
{
    (void)zero; /* usbd ends a short reply on a packet boundary with a ZLP itself */
    if (!ep0_live()) { complete_ep0(YOKE_USBD_INTERRUPTED); return; }
    uint16_t wlength = usb_le16(s.setup + 6);
    if (s.ep0 != EP0_WAIT || !(s.setup[0] & 0x80u) || !wlength) {
        complete_ep0(YOKE_USBD_ERROR);
        return;
    }
    s.ep0_len = (uint16_t)(len < wlength ? len : wlength);
    if (s.port->control_xfer((void *)data, s.ep0_len)) {
        s.ep0 = EP0_DATA;
    } else {
        s.port->control_stall();
        s.ep0 = EP0_IDLE;
        complete_ep0(YOKE_USBD_ERROR);
    }
}

void yoke_usbd_ep0_read(uint8_t *data, size_t len)
{
    if (!ep0_live()) { complete_ep0(YOKE_USBD_INTERRUPTED); return; }
    uint16_t wlength = usb_le16(s.setup + 6);
    bool status = len == 0 && wlength == 0;
    bool out_data = len && wlength && !(s.setup[0] & 0x80u);
    if (s.ep0 != EP0_WAIT || (!status && !out_data)) { complete_ep0(YOKE_USBD_ERROR); return; }
    s.ep0_len = (uint16_t)(len < wlength ? len : wlength);
    bool ok = status ? s.port->control_status() : s.port->control_xfer(data, s.ep0_len);
    if (ok) {
        s.ep0 = status ? EP0_STATUS : EP0_DATA;
    } else {
        s.port->control_stall();
        s.ep0 = EP0_IDLE;
        complete_ep0(YOKE_USBD_ERROR);
    }
}

void yoke_usbd_ep0_stall(void)
{
    if (!ep0_live()) { complete_ep0(0); return; } /* nothing owed: no bus effect */
    if (s.ep0 != EP0_WAIT) { complete_ep0(YOKE_USBD_ERROR); return; } /* a stage is armed */
    s.port->control_stall();
    s.ep0 = EP0_IDLE;
    complete_ep0(0);
}

static bool configured_fifo(uint8_t address, uint16_t packet)
{
    usbd_fifo_t fifos[SLOTS];
    size_t count = usbd_plan_fifos(s.d->configs[s.config], fifos, SLOTS);
    for (size_t i = 0; i < count; ++i)
        if (fifos[i].address == address) return packet <= fifos[i].max_packet;
    return false;
}

int yoke_usbd_ep_enable(const uint8_t *desc)
{
    if (s.config < 0) return -ENODEV;
    if (!desc || desc[0] < 7 || desc[1] != USB_DT_ENDPOINT || !configured_fifo(desc[2], packet_size(desc)))
        return -EINVAL;
    unsigned slot = slot_of(desc[2]);
    ep_t *e = &s.ep[slot];
    if (e->state != EP_FREE) return -EBUSY;
    /* open() activated alternate 0 already; anything else resets DATA0. */
    if (!(e->fresh && !memcmp(e->fresh, desc, 7)) && !s.port->activate(desc)) return -EIO;
    memcpy(e->desc, desc, sizeof(e->desc));
    e->fresh = NULL;
    e->halted = false;
    e->in = desc[2] & 0x80u;
    e->state = EP_IDLE;
    return (int)slot;
}

static ep_t *enabled_slot(int handle)
{
    if (handle < 0 || handle >= (int)SLOTS || s.ep[handle].state == EP_FREE) return NULL;
    return &s.ep[handle];
}

int yoke_usbd_ep_disable(int handle)
{
    ep_t *e = enabled_slot(handle);
    if (!e) return -EINVAL;
    if (e->state != EP_IDLE) return -EBUSY;
    e->state = EP_FREE; /* stays active in hardware, NAKing: DWC2 has no close */
    return 0;
}

int yoke_usbd_ep_set_halt(int handle)
{
    ep_t *e = enabled_slot(handle);
    if (!e) return -EINVAL;
    if (e->state != EP_IDLE) return -EBUSY;
    s.port->stall(e->desc[2]);
    e->halted = true;
    return 0;
}

static void start(int handle, uint8_t *data, size_t len, bool zero, bool in)
{
    ep_t *e = enabled_slot(handle);
    if (!e || e->state != EP_IDLE || e->in != in) {
        /* Report on the handle without disturbing an operation in flight. */
        if (handle >= 0 && handle < (int)SLOTS && (!e || e->state == EP_IDLE)) {
            s.ep[handle].pending = true;
            s.ep[handle].result = YOKE_USBD_ERROR;
            schedule();
        }
        return;
    }
    e->data = data;
    e->len = len;
    e->done = 0;
    e->zero = zero;
    e->zlp = false;
    if (e->halted) e->state = EP_HALT_WAIT; /* until the host clears the halt */
    else arm((unsigned)handle);
}

void yoke_usbd_ep_write(int handle, uint8_t *data, size_t len, bool zero)
{
    start(handle, data, len, zero, true);
}

void yoke_usbd_ep_read(int handle, uint8_t *data, size_t len)
{
    start(handle, data, len, false, false);
}

void yoke_usbd_ep_cancel(int handle)
{
    ep_t *e = enabled_slot(handle);
    if (!e) return;
    if (e->state == EP_HALT_WAIT) {
        complete_ep((unsigned)handle, YOKE_USBD_INTERRUPTED);
    } else if (e->state == EP_BUSY) {
        abort_op((unsigned)handle, YOKE_USBD_INTERRUPTED);
    }
}

yoke_usbd_speed_t yoke_usbd_link_speed(void) { return s.port->speed(); }

unsigned yoke_usbd_endpoints(unsigned *in_numbers)
{
    *in_numbers = s.limits.ep_in_count - 1u;
    return s.limits.ep_count - 1u;
}

void yoke_usbd_post(void (*fn)(void *arg), void *arg) { s.port->post(fn, arg); }
