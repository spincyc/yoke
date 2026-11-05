/* Computer-side presentation core (reference: proxy.py Proxy, session.py).
 *
 * The reference's event thread blocks in each step of a control request;
 * here a request is one explicit flow through e0.flow:
 *   E0_IDLE -> (E0_DATA: read OUT data) -> (E0_QUIESCE: stop the pumps a
 *   configuration change replaces) -> E0_GATE (wait for an in-flight remote
 *   port reset) -> E0_REMOTE (control URB) -> E0_REPLY (ep0 answer) -> IDLE.
 * Local answers go straight to E0_REPLY. A bus reset abandons the flow.
 *
 * A new SETUP abandons the current request only while ep0 still owes it its
 * answer and the request may be dropped. A request whose status stage the
 * UDC already completed (OUT-with-data once its data arrived, or
 * YUSB_EV_CONTROL_ACKED), one with an ep0 operation in progress, and a
 * SET_CONFIGURATION or SET_INTERFACE always run to completion; later SETUPs
 * wait in e0.queue and start whenever the flow is idle again (settle()).
 * Every public entry point ends in settle(), which reaps stopped pumps and
 * advances whatever waited for them, so no step runs re-entrantly.
 */
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "proxy_int.h"

/* --- diagnostics and ending -------------------------------------------- */

static void vformat(char *out, size_t cap, const char *fmt, va_list args)
{
    vsnprintf(out, cap, fmt, args);
}

void px_log(yusb_proxy_t *p, const char *fmt, ...)
{
    if (!p->ops.log) return;
    char text[240];
    va_list args;
    va_start(args, fmt);
    vformat(text, sizeof(text), fmt, args);
    va_end(args);
    p->ops.log(p->ops.ctx, text);
}

void px_fault(yusb_proxy_t *p, const char *fmt, ...)
{
    va_list args;
    va_start(args, fmt);
    vformat(p->error, sizeof(p->error), fmt, args);
    va_end(args);
    px_log(p, "%s", p->error);
}

void px_end(yusb_proxy_t *p, yusb_end_t end, const char *fmt, ...)
{
    if (p->end == YUSB_END_NONE) {
        va_list args;
        va_start(args, fmt);
        vformat(p->end_reason, sizeof(p->end_reason), fmt, args);
        va_end(args);
        p->end = end;
        if (end == YUSB_END_FAULT || end == YUSB_END_REJECTED)
            snprintf(p->error, sizeof(p->error), "%s", p->end_reason);
        px_log(p, "proxy ending: %s", p->end_reason);
    }
    if (p->phase < YUSB_PHASE_ENDED) p->phase = YUSB_PHASE_ENDED;
}

void px_lost(yusb_proxy_t *p, const char *fmt, ...)
{
    char text[180];
    va_list args;
    va_start(args, fmt);
    vformat(text, sizeof(text), fmt, args);
    va_end(args);
    px_end(p, YUSB_END_REMOTE_LOST, "remote lost: %s", text);
    if (!p->lost) cl_fail_all(p);
    p->rx.dead = true;
}

void px_deadline(yusb_proxy_t *p, uint64_t at)
{
    if (at < p->next_deadline) p->next_deadline = at;
}

/* --- lifecycle ---------------------------------------------------------- */

yusb_proxy_t *yusb_proxy_create(const yusb_policy_t *policy, const yusb_gadget_ops_t *ops,
                                const char *busid, uint64_t now_ms)
{
    size_t len = busid ? strlen(busid) : 0;
    if (!policy || !ops || yusb_policy_check(policy, NULL, 0) || len < 1 || len >= YUSB_BUSID_SIZE)
        return NULL;
    yusb_proxy_t *p = calloc(1, sizeof(*p));
    if (!p) return NULL;
    p->policy = *policy;
    p->ops = *ops;
    memcpy(p->busid, busid, len + 1);
    p->now = now_ms;
    p->next_deadline = UINT64_MAX;
    p->phase = YUSB_PHASE_IMPORT;
    p->state = "idle";
    p->remote_dirty = true;
    p->reset_urb = p->builder_urb = p->e0.urb = NO_URB;
    for (unsigned i = 0; i < YUSB_MAX_PUMPS; ++i) p->handles[i] = -1;
    yusb_control_reset(&p->control);
    ybuf_layout(&p->layout, ops->buffers);
    p->ep0_block = ybuf_alloc(&p->layout, ybuf_offset(&p->layout) + policy->ep0_max);
    if (p->ep0_block) p->ep0buf = ybuf_at(&p->layout, p->ep0_block, 0);
    if (!p->ep0buf || cl_init(p)) {
        yusb_proxy_destroy(p);
        return NULL;
    }
    p->import_deadline = now_ms + policy->import_timeout_ms;
    px_deadline(p, p->import_deadline);
    return p;
}

void yusb_proxy_destroy(yusb_proxy_t *p)
{
    if (!p) return;
    for (unsigned i = 0; i < YUSB_MAX_PUMPS; ++i)
        if (p->pumps[i].used) pump_free(p, &p->pumps[i]);
    cl_free(p);
    ybuf_free(&p->layout, p->ep0_block);
    free(p);
}

yusb_phase_t yusb_proxy_phase(const yusb_proxy_t *p) { return p->phase; }

const yusb_presentation_t *yusb_proxy_presentation(const yusb_proxy_t *p)
{
    return p->built ? &p->pres : NULL;
}

const yusb_device_info_t *yusb_proxy_device_info(const yusb_proxy_t *p) { return &p->info; }

/* --- descriptor fetch (session.open_session) ---------------------------- */

static void builder_step(yusb_proxy_t *p)
{
    yusb_setup_t req;
    int rc = yusb_builder_next(&p->builder, &req);
    if (rc == 1) {
        p->builder_urb = cl_control(p, &req, p->ep0buf, O_BUILDER, 0, p->policy.import_timeout_ms);
        if (p->builder_urb == NO_URB && !p->lost) px_end(p, YUSB_END_FAULT, "URB table full");
        return;
    }
    if (rc == 0) {
        p->built = true;
        p->phase = YUSB_PHASE_READY;
        return;
    }
    if (rc == YUSB_ERR_UNSUPPORTED) {
        snprintf(p->rejected, sizeof(p->rejected), "%s", p->builder.why);
        px_end(p, YUSB_END_UNSUPPORTED, "unsupported device: %s", p->builder.why);
    } else {
        px_end(p, YUSB_END_DESCRIPTOR, "%s", p->builder.why);
    }
}

void px_imported(yusb_proxy_t *p)
{
    p->phase = YUSB_PHASE_FETCH;
    yusb_builder_init(&p->builder, &p->pres, p->info.speed, p->policy.power_ma,
                      p->policy.ep0_size, &p->policy.budget);
    p->builder.prefetch_os = p->policy.prefetch_os_string;
    p->builder.max_request = p->policy.ep0_max;
    builder_step(p);
}

static void builder_done(yusb_proxy_t *p, const urb_result_t *r)
{
    p->builder_urb = NO_URB;
    if (r->lost || p->phase != YUSB_PHASE_FETCH) return;
    if (r->timeout) {
        px_end(p, YUSB_END_TIMEOUT, "descriptor request timed out");
        return;
    }
    if (r->status && r->status != -YUSB_EPIPE) {
        px_end(p, YUSB_END_DESCRIPTOR, "control transfer failed with status %d", (int)r->status);
        return;
    }
    yusb_builder_feed(&p->builder, r->data, r->actual, r->status == -YUSB_EPIPE);
    builder_step(p);
}

void yusb_proxy_gadget_started(yusb_proxy_t *p, const yusb_udc_ep_t *eps, size_t count,
                               uint64_t now_ms)
{
    p->now = now_ms;
    if (p->phase != YUSB_PHASE_READY) return;
    p->gadget = true;
    p->phase = YUSB_PHASE_PRESENTING;
    p->state = "presenting";
    char missing[160];
    if (eps && count && yusb_missing_endpoints(&p->pres, eps, count, missing, sizeof(missing))) {
        snprintf(p->rejected, sizeof(p->rejected), "UDC lacks endpoints: %s", missing);
        px_end(p, YUSB_END_REJECTED, "%s", p->rejected);
    }
}

/* --- endpoint zero -------------------------------------------------------- */

static void e0_write(yusb_proxy_t *p, const uint8_t *data, size_t len, bool zero)
{
    uint8_t *buf = p->ep0buf + YUSB_HEADROOM;
    if (len > p->policy.ep0_max) len = p->policy.ep0_max;
    if (data != buf) memmove(buf, data, len);
    p->e0.op = true;
    p->e0.flow = E0_REPLY;
    p->ops.ep0_write(p->ops.ctx, buf, len, zero);
}

static void e0_ack(yusb_proxy_t *p)
{
    if (p->e0.done) {         /* the UDC completed the status stage already */
        p->e0.flow = E0_IDLE;
        return;
    }
    p->e0.op = true;
    p->e0.flow = E0_REPLY;
    p->ops.ep0_read(p->ops.ctx, p->ep0buf + YUSB_HEADROOM, 0);
}

static void e0_stall(yusb_proxy_t *p, const char *reason)
{
    if (p->e0.done) {         /* too late to refuse on the bus: an ep0 STALL
                               * now would answer the next SETUP instead */
        p->counters.late_out_stalls++;
        px_log(p, "not stalled (status stage already complete): %s", reason);
        p->e0.flow = E0_IDLE;
        return;
    }
    px_log(p, "stall: %s", reason);
    p->e0.op = true;
    p->e0.flow = E0_REPLY;
    p->ops.ep0_stall(p->ops.ctx);
}

/* proxy._reply: IN data truncated to wLength, with a ZLP when short. */
static void e0_reply(yusb_proxy_t *p, const yusb_setup_t *s, const uint8_t *data, size_t len)
{
    if (!yusb_setup_has_in_data(s)) { e0_ack(p); return; }
    if (len > s->length) len = s->length;
    e0_write(p, data, len, len < s->length);
}

static void e0_abort(yusb_proxy_t *p)
{
    cl_orphan(p, p->e0.urb);
    p->e0.urb = NO_URB;
    p->e0.flow = E0_IDLE;
    p->e0.done = false;
}

void px_kick_parked(yusb_proxy_t *p)
{
    for (unsigned i = 0; i < YUSB_MAX_PUMPS; ++i)
        if (p->pumps[i].used && p->pumps[i].in) pump_kick(p, &p->pumps[i]);
}

static void disable_endpoints(yusb_proxy_t *p, uint32_t mask)
{
    for (unsigned i = 0; i < YUSB_MAX_PUMPS; ++i) {
        if (!(mask & (1u << i)) || p->handles[i] < 0) continue;
        p->ops.ep_disable(p->ops.ctx, p->handles[i]);
        p->handles[i] = -1;
        p->control.halted &= ~(1u << i);
    }
}

static uint32_t used_pumps(const yusb_proxy_t *p)
{
    uint32_t mask = 0;
    for (unsigned i = 0; i < YUSB_MAX_PUMPS; ++i)
        if (p->pumps[i].used) mask |= 1u << i;
    return mask;
}

static void stop_pumps(yusb_proxy_t *p, uint32_t mask)
{
    for (unsigned i = 0; i < YUSB_MAX_PUMPS; ++i)
        if ((mask & (1u << i)) && p->pumps[i].used) pump_stop(p, &p->pumps[i]);
}

static bot_t *bot_for(yusb_proxy_t *p, const yusb_config_t *c, const yusb_alt_t *a)
{
    if (!p->policy.bot_sizing || a->cls != YUSB_CLASS_MASS_STORAGE || a->protocol != YUSB_PROTOCOL_BOT)
        return NULL;
    bool bulk_in = false, bulk_out = false;
    for (unsigned i = 0; i < a->num_endpoints; ++i) {
        const yusb_endpoint_t *e = yusb_alt_endpoint(c, a, i);
        if (yusb_ep_type(e) != YUSB_XFER_BULK) continue;
        if (yusb_ep_is_in(e)) bulk_in = true;
        else bulk_out = true;
    }
    if (!bulk_in || !bulk_out) return NULL;
    for (unsigned i = 0; i < sizeof(p->bots) / sizeof(p->bots[0]); ++i) {
        bot_t *b = &p->bots[i];
        if (b->used && b->interface == a->number) b->used = false;
        if (!b->used) {
            memset(b, 0, sizeof(*b));
            b->used = true;
            b->interface = a->number;
            b->in_pump = -1;
            return b;
        }
    }
    return NULL;
}

/* proxy._enable_interface. Created pumps are added to *created. */
static int enable_interface(yusb_proxy_t *p, const yusb_config_t *c, unsigned number,
                            unsigned alternate, uint32_t *created)
{
    const yusb_alt_t *a = yusb_config_alt(c, number, alternate);
    if (!a) return YUSB_ERR_STATE;
    bot_t *bot = bot_for(p, c, a);
    unsigned in_packet = 0;
    for (unsigned i = 0; i < a->num_endpoints; ++i) {
        const yusb_endpoint_t *e = yusb_alt_endpoint(c, a, i);
        int handle = p->ops.ep_enable(p->ops.ctx, e);
        if (handle < 0) return handle;
        unsigned idx = pump_index(e->address);
        p->handles[idx] = handle;
        if (pump_create(p, a, e, handle, bot)) return YUSB_ERR_NOMEM;
        *created |= 1u << idx;
        if (yusb_ep_is_in(e) && !in_packet) in_packet = yusb_packet_size(e->max_packet);
    }
    for (unsigned i = 0; i < a->num_endpoints && in_packet; ++i) {
        pump_t *u = &p->pumps[pump_index(yusb_alt_endpoint(c, a, i)->address)];
        if (!u->in) u->bot_in_packet = in_packet;
    }
    return YUSB_OK;
}

static void free_pumps(yusb_proxy_t *p, uint32_t mask)
{
    for (unsigned i = 0; i < YUSB_MAX_PUMPS; ++i)
        if ((mask & (1u << i)) && p->pumps[i].used) pump_free(p, &p->pumps[i]);
}

static void start_pumps(yusb_proxy_t *p, uint32_t mask)
{
    for (unsigned i = 0; i < YUSB_MAX_PUMPS; ++i)
        if ((mask & (1u << i)) && p->pumps[i].used) pump_start(p, &p->pumps[i]);
}

static uint32_t alt_mask(const yusb_config_t *c, const yusb_alt_t *a)
{
    uint32_t mask = 0;
    for (unsigned i = 0; a && i < a->num_endpoints; ++i)
        mask |= 1u << pump_index(yusb_alt_endpoint(c, a, i)->address);
    return mask;
}

static void finish_set_configuration(yusb_proxy_t *p, const urb_result_t *r)
{
    if (r->timeout || (r->status && r->status != -YUSB_EPIPE)) {
        e0_stall(p, "remote SET_CONFIGURATION failed");
        return;
    }
    yusb_control_reset(&p->control);
    if (r->status == -YUSB_EPIPE) {
        e0_stall(p, "remote refused SET_CONFIGURATION");
        return;
    }
    unsigned value = p->e0.decision.value;
    const yusb_config_t *c = yusb_presentation_config(&p->pres, value);
    uint32_t created = 0;
    if (c) {
        int rc = YUSB_OK;
        for (unsigned n = 0; n < YUSB_MAX_INTERFACES && rc == YUSB_OK; ++n)
            if (yusb_config_has_interface(c, n)) rc = enable_interface(p, c, n, 0, &created);
        if (rc == YUSB_OK) rc = p->ops.vbus_draw(p->ops.ctx, p->policy.power_ma);
        if (rc == YUSB_OK) rc = p->ops.configure(p->ops.ctx);
        if (rc != YUSB_OK) {
            free_pumps(p, created);
            disable_endpoints(p, 0xffffffffu);
            px_fault(p, "UDC could not configure: error %d", rc);
            e0_stall(p, "UDC endpoint enable failed");
            return;
        }
        p->control.configuration = (uint8_t)value;
        for (unsigned n = 0; n < YUSB_MAX_INTERFACES; ++n)
            if (yusb_config_has_interface(c, n)) p->control.interfaces |= 1u << n;
        p->state = "configured";
    } else {
        p->state = "addressed";
    }
    start_pumps(p, created);
    e0_ack(p);
}

static void finish_set_interface(yusb_proxy_t *p, const urb_result_t *r)
{
    unsigned number = p->e0.decision.interface, alternate = p->e0.decision.alternate;
    const yusb_config_t *c = yusb_presentation_config(&p->pres, p->control.configuration);
    if (!c) { e0_stall(p, "not configured"); return; }
    unsigned previous = number < YUSB_MAX_INTERFACES ? p->control.alternates[number] : 0;
    uint32_t old = alt_mask(c, yusb_config_alt(c, number, previous)), created = 0;
    if (r->timeout || r->status) {
        /* The device keeps its previous alternate setting: restore it. */
        disable_endpoints(p, old);
        int rc = enable_interface(p, c, number, previous, &created);
        if (rc) px_fault(p, "UDC could not restore alternate setting: error %d", rc);
        start_pumps(p, created);
        e0_stall(p, "remote refused SET_INTERFACE");
        return;
    }
    disable_endpoints(p, old);
    int rc = enable_interface(p, c, number, alternate, &created);
    if (rc) {
        px_fault(p, "UDC could not enable alternate setting: error %d", rc);
        free_pumps(p, created);
        e0_stall(p, "UDC endpoint enable failed");
        return;
    }
    p->control.alternates[number] = (uint8_t)alternate;
    p->control.interfaces |= 1u << number;
    start_pumps(p, created);
    e0_ack(p);
}

static void e0_submit(yusb_proxy_t *p)
{
    p->remote_dirty = true;
    p->counters.forwarded++;
    p->e0.flow = E0_REMOTE;
    p->e0.urb = cl_control(p, &p->e0.setup, p->ep0buf, O_EP0, 0, p->policy.control_timeout_ms);
    if (p->e0.urb == NO_URB) {
        p->e0.flow = E0_IDLE;
        if (!p->lost) e0_stall(p, "URB table full");
        return;
    }
    /* An OUT payload is sent from ep0buf: no later request may reuse the
     * buffer until those bytes are on the wire, even if this one is
     * abandoned before they are. */
    p->e0.tx_mark = p->urbs[p->e0.urb].tx_end;
}

/* Advance a flow waiting for pumps to stop or for a remote reset. */
static void e0_advance(yusb_proxy_t *p)
{
    if (p->e0.flow == E0_QUIESCE && !(used_pumps(p) & p->e0.quiesce)) {
        if (p->e0.decision.kind == YUSB_D_SET_CONFIGURATION) disable_endpoints(p, 0xffffffffu);
        p->e0.flow = E0_GATE;
        p->e0.gate_deadline = p->now + p->policy.control_timeout_ms;
        px_deadline(p, p->e0.gate_deadline);
    }
    if (p->e0.flow == E0_GATE &&
        ((p->reset_urb == NO_URB && !p->reset_wanted) || p->now >= p->e0.gate_deadline))
        e0_submit(p);
}

static void e0_gate(yusb_proxy_t *p)
{
    p->e0.flow = E0_GATE;
    p->e0.gate_deadline = p->now + p->policy.control_timeout_ms;
    px_deadline(p, p->e0.gate_deadline);
    e0_advance(p);
}

static void e0_forward(yusb_proxy_t *p)
{
    const yusb_setup_t *s = &p->e0.setup;
    if (s->length > p->policy.ep0_max) {
        e0_stall(p, "control request longer than ep0_max");   /* MCU-sized buffers */
        return;
    }
    if (!yusb_setup_is_in(s) && s->length) {
        p->e0.flow = E0_DATA;
        p->e0.op = true;
        p->ops.ep0_read(p->ops.ctx, p->ep0buf + YUSB_HEADROOM, s->length);
        return;
    }
    e0_gate(p);
}

static void e0_remote_done(yusb_proxy_t *p, const urb_result_t *r)
{
    p->e0.urb = NO_URB;
    px_kick_parked(p);
    if (r->lost || p->e0.flow != E0_REMOTE) return;
    p->e0.flow = E0_IDLE;
    const yusb_setup_t *s = &p->e0.setup;
    switch (p->e0.decision.kind) {
    case YUSB_D_SET_CONFIGURATION: finish_set_configuration(p, r); return;
    case YUSB_D_SET_INTERFACE: finish_set_interface(p, r); return;
    default: break;
    }
    if (r->timeout) { e0_stall(p, "remote control request timed out"); return; }
    if (r->status && r->status != -YUSB_EPIPE) {
        char why[64];
        snprintf(why, sizeof(why), "control transfer failed with status %d", (int)r->status);
        e0_stall(p, why);
        return;
    }
    bool stalled = r->status == -YUSB_EPIPE;
    if (p->e0.decision.cache) yusb_control_remember(&p->control, s, r->data, r->actual, stalled);
    if (!yusb_setup_is_in(s) && s->length) {
        /* The UDC completed the status stage when the data arrived. */
        if (stalled) {
            p->counters.late_out_stalls++;
            px_log(p, "remote STALL after local status stage: request 0x%02x 0x%02x",
                   s->request_type, s->request);
        }
        return;
    }
    if (stalled) e0_stall(p, "remote STALL");
    else e0_reply(p, s, r->data, r->actual);
}

/* Bulk-Only Mass Storage Reset, on receipt: the interface's BOT plan ends. */
static void bot_reset(yusb_proxy_t *p, unsigned interface)
{
    for (unsigned i = 0; i < YUSB_MAX_PUMPS; ++i) {
        pump_t *u = &p->pumps[i];
        if (u->used && !u->in && u->bot && u->bot->interface == interface) pump_bot_abort(p, u);
    }
}

static void e0_handle(yusb_proxy_t *p, const uint8_t raw[8], bool acked)
{
    yusb_setup_t s = yusb_setup_unpack(raw);
    p->counters.control++;
    /* Only a request without a data stage can have been acknowledged. */
    p->e0.done = acked && !s.length;
    if (s.request_type == 0x21 && s.request == YUSB_BOT_RESET && !s.length)
        bot_reset(p, s.index & 0xffu);   /* on receipt, before any decision */
    if (!p->speed_checked) {
        p->speed_checked = true;
        p->link_speed = p->ops.link_speed ? p->ops.link_speed(p->ops.ctx) : YUSB_SPEED_UNKNOWN;
        if (p->link_speed > YUSB_SPEED_UNKNOWN && p->link_speed < p->pres.speed) {
            snprintf(p->rejected, sizeof(p->rejected),
                     "computer link is %s; device needs %s (unsupported)",
                     yusb_speed_name(p->link_speed), yusb_speed_name(p->pres.speed));
            px_fault(p, "%s", p->rejected);
        }
    }
    if (p->rejected[0]) { e0_stall(p, p->rejected); return; }
    yusb_decision_t *d = &p->e0.decision;
    yusb_decide(&s, &p->pres, &p->control, &p->policy, d);
    p->e0.setup = s;
    switch (d->kind) {
    case YUSB_D_REPLY:
        p->counters.cached++;
        e0_reply(p, &s, d->data, d->len);
        break;
    case YUSB_D_ACK: e0_ack(p); break;
    case YUSB_D_STALL: e0_stall(p, d->reason); break;
    case YUSB_D_FORWARD:
        e0_forward(p);
        break;
    case YUSB_D_ENDPOINT_HALT:
        if (!d->halt) p->control.halted &= ~(1u << pump_index(d->address));
        e0_forward(p);
        break;
    case YUSB_D_SET_CONFIGURATION:
        p->e0.quiesce = used_pumps(p);
        stop_pumps(p, p->e0.quiesce);
        p->e0.flow = E0_QUIESCE;
        break;
    case YUSB_D_SET_INTERFACE: {
        const yusb_config_t *c = yusb_presentation_config(&p->pres, p->control.configuration);
        unsigned current = d->interface < YUSB_MAX_INTERFACES ? p->control.alternates[d->interface] : 0;
        p->e0.quiesce = alt_mask(c, c ? yusb_config_alt(c, d->interface, current) : NULL) & used_pumps(p);
        stop_pumps(p, p->e0.quiesce);
        p->e0.flow = E0_QUIESCE;
        break;
    }
    }
}

static bool e0_ready(const yusb_proxy_t *p)
{
    return p->e0.flow == E0_IDLE && !p->e0.op && p->tx.sent >= p->e0.tx_mark &&
           p->phase == YUSB_PHASE_PRESENTING;
}

/* A new SETUP: every ep0 operation the core issues from now on answers
 * this SETUP or a later one, never an earlier request (the contract in
 * yusb_proxy.h). */
static void e0_control(yusb_proxy_t *p, const uint8_t raw[8], bool acked)
{
    if (p->e0.nqueued) {
        /* The host gave up on the newest waiting SETUP, unless its status
         * stage is already complete (then it still runs, without answer). */
        unsigned last = p->e0.nqueued - 1u;
        if (!p->e0.queue[last].acked) {
            px_log(p, "ep0: queued request 0x%02x 0x%02x abandoned by the host",
                   p->e0.queue[last].raw[0], p->e0.queue[last].raw[1]);
            p->e0.nqueued--;
        }
    } else if (e0_ready(p)) {
        e0_handle(p, raw, acked);
        return;
    } else if (p->e0.flow != E0_IDLE && p->e0.flow != E0_REPLY && !p->e0.op) {
        if (p->e0.done || p->e0.decision.kind == YUSB_D_SET_CONFIGURATION ||
            p->e0.decision.kind == YUSB_D_SET_INTERFACE) {
            /* It runs to completion (a configuration change abandoned
             * halfway would leave the endpoints unconfigured), but ep0 owes
             * it nothing more: the host has moved on. */
            p->e0.done = true;
        } else {
            e0_abort(p);   /* still owed and droppable: unlinked */
            if (e0_ready(p)) {
                e0_handle(p, raw, acked);
                return;
            }
        }
    }
    if (p->e0.nqueued == E0_QUEUE) {
        px_fault(p, "ep0: more than %u SETUPs waiting", E0_QUEUE);
        return;
    }
    memcpy(p->e0.queue[p->e0.nqueued].raw, raw, 8);
    p->e0.queue[p->e0.nqueued++].acked = acked;
}

/* Start waiting SETUPs while the flow is idle. */
static void e0_next(yusb_proxy_t *p)
{
    while (p->e0.nqueued && e0_ready(p)) {
        uint8_t raw[8];
        bool acked = p->e0.queue[0].acked;
        memcpy(raw, p->e0.queue[0].raw, 8);
        memmove(p->e0.queue, p->e0.queue + 1, --p->e0.nqueued * sizeof(p->e0.queue[0]));
        e0_handle(p, raw, acked);
    }
}

void yusb_proxy_ep0_done(yusb_proxy_t *p, int result, uint64_t now_ms)
{
    p->now = now_ms;
    p->e0.op = false;
    if (p->e0.flow == E0_DATA) {
        if (result < 0) {
            px_log(p, "ep0 OUT data: gadget error %d", result);
            p->e0.flow = E0_IDLE;
        } else {
            size_t n = (size_t)result;
            if (n < p->e0.setup.length)
                memset(p->ep0buf + YUSB_HEADROOM + n, 0, p->e0.setup.length - n);
            p->e0.out_len = (uint32_t)n;
            p->e0.done = true;    /* the UDC acknowledged the status stage */
            e0_gate(p);
        }
    } else if (p->e0.flow == E0_REPLY) {
        if (result < 0) px_log(p, "ep0: gadget error %d", result);
        p->e0.flow = E0_IDLE;
    }
    if (p->e0.stall_owed && p->phase == YUSB_PHASE_PRESENTING) {
        p->e0.stall_owed = false;
        e0_stall(p, "bus reset: release the abandoned request");
    }
    yusb_proxy_poll(p, now_ms);
}

/* --- bus events ---------------------------------------------------------- */

/* Whether the UDC still holds the newest delivered SETUP unanswered. Raw
 * Gadget keeps it pending across a reset or disconnect, and refuses every
 * later SETUP until ep0 answers it. */
static bool e0_owed(const yusb_proxy_t *p)
{
    if (p->e0.nqueued) return !p->e0.queue[p->e0.nqueued - 1u].acked;
    return !p->e0.op && !p->e0.done &&
           (p->e0.flow == E0_QUIESCE || p->e0.flow == E0_GATE || p->e0.flow == E0_REMOTE);
}

static void bus_reset(yusb_proxy_t *p, const char *state)
{
    p->counters.bus_resets++;
    bool owed = e0_owed(p);
    e0_abort(p);
    p->e0.nqueued = 0;
    if (owed) {
        /* After the outstanding ep0 operation, if any (one at a time). A
         * backend with nothing pending completes it without bus effect. */
        if (p->e0.op) p->e0.stall_owed = true;
        else e0_stall(p, "bus reset: release the abandoned request");
    }
    stop_pumps(p, used_pumps(p));
    yusb_control_reset(&p->control);
    p->bus_quiesce = true;
    p->state = state;
    p->suspended = false;
    p->speed_checked = false;
    if (p->policy.reset_remote_on_bus_reset && p->remote_dirty) {
        p->remote_dirty = false;
        p->reset_wanted = true;   /* sent once the pumps' URBs are unlinked */
    }
}

static void reset_done(yusb_proxy_t *p, const urb_result_t *r)
{
    p->reset_urb = NO_URB;
    if (r->lost) return;
    if (r->timeout) px_log(p, "remote reset failed: timed out");
    else if (r->status && r->status != -YUSB_EPIPE) px_log(p, "remote reset failed: status %d", (int)r->status);
    else p->counters.remote_resets++;
}

void yusb_proxy_event(yusb_proxy_t *p, yusb_event_t type, const uint8_t *setup, uint64_t now_ms)
{
    p->now = now_ms;
    if (p->phase != YUSB_PHASE_PRESENTING) return;
    switch (type) {
    case YUSB_EV_CONTROL:
    case YUSB_EV_CONTROL_ACKED:
        if (setup) e0_control(p, setup, type == YUSB_EV_CONTROL_ACKED);
        break;
    case YUSB_EV_RESET: bus_reset(p, "default"); break;
    case YUSB_EV_DISCONNECT:
        p->counters.disconnects++;
        bus_reset(p, "disconnected");
        break;
    case YUSB_EV_SUSPEND: p->suspended = true; break;
    case YUSB_EV_RESUME: p->suspended = false; break;
    case YUSB_EV_CONNECT: p->state = "attached"; break;
    }
    yusb_proxy_poll(p, now_ms);
}

/* --- dispatch, settle, poll ------------------------------------------------ */

void px_urb_done(yusb_proxy_t *p, const urb_t *u, unsigned index, const urb_result_t *r)
{
    switch (u->owner) {
    case O_BUILDER: builder_done(p, r); break;
    case O_EP0: if (index == p->e0.urb) e0_remote_done(p, r); break;
    case O_RESET: reset_done(p, r); break;
    case O_PUMP: pump_urb(p, &p->pumps[u->pump], u->slot, r); break;
    case O_CLEAR:
        if (p->pumps[u->pump].used && p->pumps[u->pump].clear_urb == index)
            pump_clear_done(p, &p->pumps[u->pump], r);
        break;
    default: break;
    }
}

static void settle(yusb_proxy_t *p)
{
    /* A stopped pump's buffers may still be named by a receive or transmit
     * the platform has in progress until the connection is quiet: free
     * them only once their bytes are sent, or during yusb_proxy_stop. */
    for (unsigned i = 0; i < YUSB_MAX_PUMPS; ++i) {
        pump_t *u = &p->pumps[i];
        if (u->used && u->run == P_STOPPED &&
            (p->phase == YUSB_PHASE_STOPPING || (!p->lost && p->tx.sent >= u->tx_mark)))
            pump_free(p, u);
    }
    uint32_t live = used_pumps(p);
    if (p->phase == YUSB_PHASE_STOPPING) {
        if (!live && !p->e0.op) {
            disable_endpoints(p, 0xffffffffu);
            p->phase = YUSB_PHASE_STOPPED;
        }
        return;
    }
    if (p->bus_quiesce && !live) {
        disable_endpoints(p, 0xffffffffu);   /* proxy._quiesce_all */
        p->bus_quiesce = false;
    }
    if (p->reset_wanted && !live && !p->lost) {
        p->reset_wanted = false;
        yusb_setup_t s = yusb_setup(YUSB_RT_PORT, YUSB_REQ_SET_FEATURE, YUSB_PORT_FEAT_RESET, 0, 0);
        p->reset_urb = cl_control(p, &s, NULL, O_RESET, 0, p->policy.control_timeout_ms);
    }
    e0_advance(p);
    e0_next(p);
}

uint64_t yusb_proxy_poll(yusb_proxy_t *p, uint64_t now_ms)
{
    p->now = now_ms;
    if (now_ms >= p->next_deadline) {
        p->next_deadline = UINT64_MAX;
        if (p->phase == YUSB_PHASE_IMPORT) {
            if (now_ms >= p->import_deadline) px_end(p, YUSB_END_TIMEOUT, "timed out");
            else px_deadline(p, p->import_deadline);
        }
        cl_poll(p);
        for (unsigned i = 0; i < YUSB_MAX_PUMPS; ++i)
            if (p->pumps[i].used) pump_poll(p, &p->pumps[i]);
        if (p->e0.flow == E0_GATE) px_deadline(p, p->e0.gate_deadline);
        if (p->phase == YUSB_PHASE_STOPPING) px_deadline(p, p->stop_deadline);
    }
    settle(p);
    return p->next_deadline;
}

/* --- transport --------------------------------------------------------------- */

size_t yusb_proxy_rx_space(yusb_proxy_t *p, uint8_t **buf)
{
    if (p->lost) return 0;
    return rx_space(&p->rx, buf);
}

bool yusb_proxy_rx_direct(const yusb_proxy_t *p) { return p->rx.direct; }

int yusb_proxy_rx_done(yusb_proxy_t *p, size_t n, uint64_t now_ms)
{
    p->now = now_ms;
    if (p->lost) return YUSB_ERR_STATE;
    int rc = rx_done(&p->rx, n, cl_rx_element, p);
    if (rc < 0 && !p->lost) {
        px_end(p, YUSB_END_REMOTE_LOST, "remote lost: malformed USB/IP stream");
        cl_fail_all(p);
    }
    yusb_proxy_poll(p, now_ms);
    return rc < 0 ? YUSB_ERR_PROTOCOL : YUSB_OK;
}

size_t yusb_proxy_tx_iov(const yusb_proxy_t *p, yusb_iov_t *iov, size_t max)
{
    return p->lost ? 0 : txq_iov(&p->tx, iov, max);
}

void yusb_proxy_tx_done(yusb_proxy_t *p, size_t n)
{
    txq_done(&p->tx, n);
    settle(p);   /* ep0buf or a stopped pump's buffers may be free now */
}

void yusb_proxy_transport_lost(yusb_proxy_t *p, const char *why, uint64_t now_ms)
{
    p->now = now_ms;
    if (!p->lost) px_lost(p, "%s", why && *why ? why : "USB/IP peer closed the connection");
    yusb_proxy_poll(p, now_ms);
}

void yusb_proxy_ep_done(yusb_proxy_t *p, int handle, int result, uint64_t now_ms)
{
    p->now = now_ms;
    for (unsigned i = 0; i < YUSB_MAX_PUMPS; ++i) {
        pump_t *u = &p->pumps[i];
        if (u->used && u->handle == handle && u->busy) {
            pump_ep_done(p, u, result);
            break;
        }
    }
    yusb_proxy_poll(p, now_ms);
}

/* --- teardown and status -------------------------------------------------- */

void yusb_proxy_stop(yusb_proxy_t *p, uint64_t now_ms)
{
    p->now = now_ms;
    if (p->phase == YUSB_PHASE_STOPPING || p->phase == YUSB_PHASE_STOPPED) return;
    px_end(p, YUSB_END_STOPPED, "stopped");
    p->phase = YUSB_PHASE_STOPPING;
    p->stop_deadline = now_ms + p->policy.stop_timeout_ms;
    px_deadline(p, p->stop_deadline);
    /* The transport closes next and the exporter cancels everything on
     * close, so URBs are resolved here instead of unlinked one by one. */
    if (!p->lost) cl_fail_all(p);
    e0_abort(p);
    stop_pumps(p, used_pumps(p));
    settle(p);
}

yusb_footprint_t yusb_proxy_footprint(const yusb_policy_t *policy, const yusb_buffers_t *buffers)
{
    yusb_footprint_t f = {0, 0};
    if (!policy || yusb_policy_check(policy, NULL, 0)) return f;
    ybuf_layout_t layout;
    ybuf_layout(&layout, buffers);
    unsigned nurbs, ntombs;
    size_t stage;
    cl_sizes(policy, &nurbs, &ntombs, &stage);
    f.create = sizeof(yusb_proxy_t) + nurbs * sizeof(urb_t) + ntombs * sizeof(tomb_t) +
               cl_tx_segments(policy, nurbs) * sizeof(yusb_iov_t) + stage +
               ybuf_round(&layout, ybuf_offset(&layout) + policy->ep0_max);
    yusb_yx_params_t offer = {YUSB_YX_VERSION, policy->yx_levers, policy->yx_cache_blocks};
    if (yusb_yx_framed(&offer)) {
        /* Segment scratch at create; at import, the most an agreement under
         * this offer allocates: the late-payload buffer and both codecs. */
        size_t late = policy->max_transfer > policy->ep0_max ? policy->max_transfer : policy->ep0_max;
        f.create += yusb_yx_segments(policy->max_transfer) * sizeof(yusb_iov_t) + YUSB_HEADROOM +
                    late + sizeof(yusb_yx_enc_t) + yusb_yx_enc_footprint(&offer) +
                    sizeof(yusb_yx_dec_t) + yusb_yx_dec_footprint(&offer);
    }
    f.pumps_max = pump_footprint(policy, &layout, true) * policy->budget.max_in +
                  pump_footprint(policy, &layout, false) * policy->budget.max_out;
    return f;
}

bool yusb_proxy_teardown_overdue(const yusb_proxy_t *p, uint64_t now_ms)
{
    return p->phase == YUSB_PHASE_STOPPING && now_ms >= p->stop_deadline;
}

void yusb_proxy_status(const yusb_proxy_t *p, yusb_proxy_status_t *out)
{
    memset(out, 0, sizeof(*out));
    out->state = p->suspended && !strcmp(p->state, "configured") ? "suspended" : p->state;
    out->configuration = p->control.configuration;
    out->vendor = p->pres.device.vendor;
    out->product = p->pres.device.product;
    out->remote_speed = p->pres.remote_speed;
    out->presented_speed = p->pres.speed;
    out->link_speed = p->link_speed;
    out->end = p->end;
    out->end_reason = p->end_reason;
    out->error = p->error;
    out->rejected = p->rejected;
    out->yx = p->yx;
    out->yx_params = p->yxp;
}

const yusb_proxy_counters_t *yusb_proxy_counters(const yusb_proxy_t *p) { return &p->counters; }
