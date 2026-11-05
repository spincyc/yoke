/* Endpoint pumps (reference: proxy.py _InPump/_OutPump) as explicit state
 * machines. Each pump owns a ring of slots in submission order; `wait`
 * names what an interrupted sequence waits for, so every blocking step of
 * the reference becomes one state:
 *
 *  IN  remote STALL: W_PARTIAL (write data before the STALL) -> W_CANCEL
 *      (unlink later URBs) -> local halt -> W_CLEAR (clear remote, probe
 *      with one URB) or W_PARKED (device stalled again: wait for host
 *      activity or backoff 50 ms..2 s) -> W_CLEAR.
 *  OUT remote STALL: W_CANCEL -> local halt -> W_CLEAR; the next submit
 *      after the host's clear re-clears the remote first (W_PRECLEAR).
 *  Host CLEAR_FEATURE(HALT) seen as HALT_CLEARED on a started request:
 *      W_HCANCEL (unlink in-flight URBs) -> W_HCLEAR -> retry the I/O.
 */
#include <stdlib.h>
#include <string.h>
#include "proxy_int.h"

static bool cancelled_status(int32_t s) { return s == -YUSB_ECONNRESET || s == -YUSB_ENOENT; }
static bool gone_status(int32_t s) { return s == -YUSB_ESHUTDOWN || s == -YUSB_ENODEV; }

/* Slot payloads start aligned (buffers.h) and own whole alignment units;
 * an OUT slot also holds its YX-encoded payload after the raw bytes. */
static size_t stride(const yusb_proxy_t *p, const pump_t *u)
{
    size_t s = ybuf_offset(&p->layout) + u->slot_size;
    if (!u->in && p->yx) s += yusb_yx_bound(u->slot_size);
    return ybuf_round(&p->layout, s);
}

static uint8_t *slot_buf(const yusb_proxy_t *p, const pump_t *u, unsigned s)
{
    return ybuf_at(&p->layout, u->block, (size_t)s * u->stride);
}

static unsigned ring(const pump_t *u, unsigned k) { return (u->head + k) % u->nslots; }
static unsigned idx_of(const pump_t *u) { return pump_index(u->address); }

/* --- BOT size queue ---------------------------------------------------- */

static void bot_command(yusb_proxy_t *p, bot_t *b, uint32_t data, uint32_t packet, uint32_t csw)
{
    if (b->count == YUSB_BOT_COMMANDS) { px_fault(p, "BOT command queue overflow"); return; }
    b->cmds[(b->head + b->count++) % YUSB_BOT_COMMANDS] = (bot_cmd_t){data, packet, csw};
}

/* Keep only the final queued size: the current command's CSW (proxy
 * _BotLink.trim_in). Its remaining data-in sizes will not be asked for. */
static void bot_trim(bot_t *b)
{
    if (b->count) {
        bot_cmd_t last = b->cmds[(b->head + b->count - 1) % YUSB_BOT_COMMANDS];
        last.left = 0;
        b->head = 0;
        b->count = 1;
        b->cmds[0] = last;
        b->nrequeue = 0;
    } else if (b->nrequeue) {
        b->nrequeue = 1;      /* requeue[0] is the last in order */
    }
}

/* Put sizes back at the front, keeping their order (requeue_in). */
static void bot_requeue(yusb_proxy_t *p, bot_t *b, const uint32_t *sizes, unsigned n)
{
    for (unsigned k = n; k-- > 0;) {
        if (b->nrequeue == YUSB_BOT_REQUEUE) { px_fault(p, "BOT size queue overflow"); return; }
        b->requeue[b->nrequeue++] = sizes[k];
    }
}

static bool bot_pop(const yusb_proxy_t *p, bot_t *b, uint32_t *size)
{
    if (b->nrequeue) {
        *size = b->requeue[--b->nrequeue];
        return true;
    }
    if (!b->count) return false;
    bot_cmd_t *c = &b->cmds[b->head];
    if (c->left) {
        uint32_t chunk = c->left < p->policy.max_transfer ? c->left : p->policy.max_transfer;
        c->left -= chunk;
        *size = (chunk + c->packet - 1) / c->packet * c->packet;
        return true;
    }
    *size = c->csw;
    b->head = (b->head + 1) % YUSB_BOT_COMMANDS;
    b->count--;
    return true;
}

/* --- shared ------------------------------------------------------------ */

/* The most a pump of this direction can allocate (yusb_proxy_footprint):
 * the larger of the BOT and the packet-per-URB layouts. */
size_t pump_footprint(const yusb_policy_t *pol, const ybuf_layout_t *l, bool in)
{
    const unsigned packet = 3072;             /* high-bandwidth 1024 x 3 */
    size_t off = ybuf_offset(l), worst = 0;
    for (int bot = 0; bot < 2; ++bot) {
        unsigned depth, slots;
        size_t slot, stride;
        if (in) {
            depth = pol->in_depth_bulk > pol->in_depth_interrupt ? pol->in_depth_bulk : pol->in_depth_interrupt;
            slots = bot && pol->bot_in_slots < depth ? pol->bot_in_slots : depth;
            slot = bot ? pol->max_transfer : packet;
            stride = ybuf_round(l, off + slot);
        } else {
            slots = pol->out_window > pol->bot_out_window ? pol->out_window : pol->bot_out_window;
            slot = bot ? (pol->max_transfer > packet ? pol->max_transfer : packet) : packet;
            stride = ybuf_round(l, off + slot + (pol->yx_levers ? yusb_yx_bound(slot) : 0));
            depth = slots;
        }
        size_t bytes = slots * (stride + sizeof(slot_t));
        if (in && pol->coalesce_in && depth > 1) bytes += ybuf_round(l, off + pol->max_transfer);
        if (bytes > worst) worst = bytes;
    }
    return worst;
}

int pump_create(yusb_proxy_t *p, const yusb_alt_t *alt, const yusb_endpoint_t *ep, int handle,
                bot_t *bot)
{
    pump_t *u = &p->pumps[pump_index(ep->address)];
    const yusb_policy_t *pol = &p->policy;
    memset(u, 0, sizeof(*u));
    u->used = true;
    u->in = yusb_ep_is_in(ep);
    u->address = ep->address;
    u->interface = alt->number;
    u->type = (uint8_t)yusb_ep_type(ep);
    u->handle = handle;
    u->packet = yusb_packet_size(ep->max_packet);
    u->size = yusb_transfer_size(ep->max_packet);
    u->interval = (int32_t)yusb_urb_interval(p->pres.remote_speed, u->type, ep->interval);
    u->bot = u->type == YUSB_XFER_BULK ? bot : NULL;
    u->run = P_RUN;
    u->clear_urb = NO_URB;
    u->backoff = PARK_MIN_MS;
    u->bot_in_packet = 512;
    if (u->in) {
        u->depth = u->type == YUSB_XFER_INT ? pol->in_depth_interrupt : pol->in_depth_bulk;
        u->nslots = u->bot && pol->bot_in_slots < u->depth ? pol->bot_in_slots : u->depth;
        u->slot_size = u->bot ? pol->max_transfer : u->size;
        if (pol->coalesce_in && u->depth > 1) {
            u->scratch_block = ybuf_alloc(&p->layout, ybuf_offset(&p->layout) + pol->max_transfer);
            if (u->scratch_block) u->scratch = ybuf_at(&p->layout, u->scratch_block, 0);
        }
        if (u->bot) u->bot->in_pump = (int)pump_index(ep->address);
    } else {
        u->depth = pol->out_window;
        if (u->bot && pol->bot_out_window > u->depth) u->depth = pol->bot_out_window;
        u->nslots = u->depth;
        u->slot_size = u->bot ? (pol->max_transfer > u->size ? pol->max_transfer : u->size) : u->size;
    }
    u->stride = stride(p, u);
    u->block = ybuf_alloc(&p->layout, u->nslots * u->stride);
    u->slots = calloc(u->nslots, sizeof(*u->slots));
    if (!u->block || !u->slots || (u->in && pol->coalesce_in && u->depth > 1 && !u->scratch)) {
        pump_free(p, u);
        return YUSB_ERR_NOMEM;
    }
    return YUSB_OK;
}

void pump_free(yusb_proxy_t *p, pump_t *u)
{
    ybuf_free(&p->layout, u->block);
    ybuf_free(&p->layout, u->scratch_block);
    free(u->slots);
    memset(u, 0, sizeof(*u));
}

static void check_stopped(yusb_proxy_t *p, pump_t *u)
{
    (void)p;
    if (u->run == P_STOPPING && !u->busy && !u->cancel_wait) {
        u->run = P_STOPPED;       /* reaped by proxy.c settle() */
    }
}

static void dead(yusb_proxy_t *p, pump_t *u)
{
    if (u->run == P_RUN) u->run = P_DEAD;
    check_stopped(p, u);
}

static void start_write(yusb_proxy_t *p, pump_t *u)
{
    u->busy = true;
    p->ops.ep_write(p->ops.ctx, u->handle, u->wbuf + YUSB_HEADROOM, u->wlen, u->wzero);
}

static void start_read(yusb_proxy_t *p, pump_t *u)
{
    u->busy = true;
    p->ops.ep_read(p->ops.ctx, u->handle, slot_buf(p, u, u->read_slot) + YUSB_HEADROOM, u->read_size);
}

static void retry_io(yusb_proxy_t *p, pump_t *u)
{
    if (u->in) start_write(p, u);
    else start_read(p, u);
}

static void start_clear(yusb_proxy_t *p, pump_t *u, uint8_t wait)
{
    yusb_setup_t s = yusb_setup(YUSB_RECIP_ENDPOINT, YUSB_REQ_CLEAR_FEATURE,
                                YUSB_FEATURE_ENDPOINT_HALT, u->address, 0);
    p->counters.remote_clears++;
    u->wait = wait;
    u->clear_urb = cl_control(p, &s, NULL, O_CLEAR, (uint8_t)idx_of(u), p->policy.control_timeout_ms);
}

static void local_halt(yusb_proxy_t *p, pump_t *u)
{
    int rc = p->ops.ep_set_halt(p->ops.ctx, u->handle);
    if (rc < 0) px_fault(p, "halt 0x%02x: gadget error %d", u->address, rc);
    else p->control.halted |= 1u << idx_of(u);    /* GET_STATUS(endpoint) */
    p->counters.local_stalls++;
}

/* Cancel ring positions [from, count): unlink those in flight. */
static void cancel_from(yusb_proxy_t *p, pump_t *u, unsigned from, uint32_t *sizes, unsigned *n)
{
    for (unsigned k = from; k < u->count; ++k) {
        slot_t *s = &u->slots[ring(u, k)];
        if (sizes) sizes[(*n)++] = s->requested;
        if (s->state == S_INFLIGHT && !p->lost) {
            cl_cancel(p, s->urb);
            s->state = S_CANCEL;
            u->cancel_wait++;
        } else {
            s->state = S_CANCEL;   /* done or lost: its data is dropped */
        }
    }
    u->cancel_deadline = p->now + p->policy.stop_timeout_ms;
    px_deadline(p, u->cancel_deadline);
}

static void free_head(pump_t *u, unsigned n)
{
    for (unsigned k = 0; k < n; ++k) u->slots[ring(u, k)].state = S_FREE;
    u->head = ring(u, n);
    u->count -= n;
}

static void truncate_ring(pump_t *u, unsigned keep)
{
    for (unsigned k = keep; k < u->count; ++k) u->slots[ring(u, k)].state = S_FREE;
    u->count = keep;
}

/* --- IN ---------------------------------------------------------------- */

static void in_run(yusb_proxy_t *p, pump_t *u);

static void in_after_cancel(yusb_proxy_t *p, pump_t *u)
{
    truncate_ring(u, 0);
    local_halt(p, u);
    if (u->remote_halted && !u->kicked) {
        /* Stalled again right after a clear: it waits for something only
         * the host can do (BOT reset recovery). Park instead of spinning. */
        u->parked_until = p->now + u->backoff;
        u->backoff = u->backoff * 2 > PARK_MAX_MS ? PARK_MAX_MS : u->backoff * 2;
        p->counters.parked++;
        u->wait = W_PARKED;
        px_deadline(p, u->parked_until);
    } else {
        u->kicked = false;
        start_clear(p, u, W_CLEAR);
    }
}

/* The head slot stalled; its partial data (if any) is written. */
static void in_stall_cancel(yusb_proxy_t *p, pump_t *u)
{
    uint32_t sizes[65];
    unsigned n = 0;
    sizes[n++] = u->size;          /* the stalled URB requeues at the packet size (as Python) */
    free_head(u, 1);
    cancel_from(p, u, 0, sizes, &n);
    if (u->bot) bot_requeue(p, u->bot, sizes, n);
    u->wait = W_CANCEL;
    if (!u->cancel_wait) in_after_cancel(p, u);
}

static void in_submit(yusb_proxy_t *p, pump_t *u)
{
    unsigned limit = u->probing ? 1 : u->depth;
    unsigned submitted = 0;
    while (u->count < limit && u->count < u->nslots) {
        uint32_t size = u->size;
        if (u->bot && !bot_pop(p, u->bot, &size)) break;
        unsigned s = ring(u, u->count);
        cl_req_t q = {.ep = u->address & 0x0fu, .dir = YUSB_USBIP_DIR_IN, .length = size,
                      .buf = slot_buf(p, u, s), .interval = u->interval,
                      .bulk = u->type == YUSB_XFER_BULK, .owner = O_PUMP,
                      .pump = (uint8_t)idx_of(u), .slot = (uint16_t)s};
        uint16_t urb = cl_submit(p, &q);
        if (urb == NO_URB) {
            if (u->bot) bot_requeue(p, u->bot, &size, 1);
            break;
        }
        u->slots[s] = (slot_t){S_INFLIGHT, false, false, urb, size, 0, 0};
        u->count++;
        submitted++;
    }
    p->counters.in_urbs += submitted;
}

static void in_deliver(yusb_proxy_t *p, pump_t *u)
{
    while (!u->busy && u->wait == W_NONE && u->run == P_RUN && u->count) {
        slot_t *s = &u->slots[u->head];
        if (s->state != S_DONE) return;
        if (s->unlinked || cancelled_status(s->status)) {
            if (u->bot) bot_requeue(p, u->bot, &s->requested, 1);
            free_head(u, 1);
            continue;
        }
        if (s->status == 0) {
            uint32_t actual[64], requested[64];
            unsigned n = 0;
            for (unsigned k = 0; k < u->count && n < 64 && (n == 0 || p->policy.coalesce_in); ++k) {
                const slot_t *t = &u->slots[ring(u, k)];
                if (t->state != S_DONE || t->status || t->unlinked) break;
                actual[n] = t->actual;
                requested[n++] = t->requested;
            }
            uint32_t total;
            size_t count = yusb_in_write_plan(actual, requested, n, u->packet,
                                              p->policy.max_transfer, &total, &u->wzero);
            if (count == 1 || !u->scratch) {
                u->wbuf = slot_buf(p, u, u->head);
                u->writing = 1;
            } else {
                /* Several URBs into one write: copy, then the slots can be reused. */
                size_t off = YUSB_HEADROOM;
                for (unsigned k = 0; k < count; ++k) {
                    unsigned i = ring(u, k);
                    memcpy(u->scratch + off, slot_buf(p, u, i) + YUSB_HEADROOM, u->slots[i].actual);
                    off += u->slots[i].actual;
                }
                free_head(u, (unsigned)count);
                u->wbuf = u->scratch;
                u->writing = 0;
            }
            u->wlen = total;
            u->probing = false;
            u->remote_halted = false;
            u->backoff = PARK_MIN_MS;
            start_write(p, u);
            return;
        }
        if (gone_status(s->status)) {
            px_lost(p, "device gone (status %d)", (int)s->status);
            return;
        }
        /* STALL, or a transaction error presented as STALL. */
        if (s->status == -YUSB_EPIPE) p->counters.remote_stalls++;
        else p->counters.transaction_errors++;
        if (s->actual) {
            u->wait = W_PARTIAL;
            u->wbuf = slot_buf(p, u, u->head);
            u->wlen = s->actual;
            u->wzero = false;
            u->writing = 1;
            start_write(p, u);
            return;
        }
        in_stall_cancel(p, u);
        return;
    }
}

static void in_run(yusb_proxy_t *p, pump_t *u)
{
    if (u->run != P_RUN) return;
    in_deliver(p, u);
    if (u->run == P_RUN && u->wait == W_NONE) in_submit(p, u);
}

static void unpark(yusb_proxy_t *p, pump_t *u)
{
    u->kicked = false;
    u->parked_until = 0;
    start_clear(p, u, W_CLEAR);
}

/* --- OUT --------------------------------------------------------------- */

static void out_run(yusb_proxy_t *p, pump_t *u);

/* The BOT data-out phase ended early (the device STALLed it, the host
 * cleared the halt, or a Bulk-Only Mass Storage Reset): the next 31-byte
 * read may be a CBW again, and the command's data-in will not come. */
void pump_bot_abort(yusb_proxy_t *p, pump_t *u)
{
    (void)p;
    if (u->in || !u->bot) return;
    u->out_head = u->out_count = 0;
    u->read_planned = false;  /* a read in progress is checked for a CBW */
    bot_trim(u->bot);
}

static void out_after_cancel(yusb_proxy_t *p, pump_t *u, bool halt)
{
    truncate_ring(u, 0);
    if (halt) {
        local_halt(p, u);
        pump_bot_abort(p, u);
    }
    u->remote_halted = halt;   /* consulted when W_CLEAR completes */
    start_clear(p, u, W_CLEAR);
}

static void out_failure(yusb_proxy_t *p, pump_t *u)
{
    slot_t *s = &u->slots[u->head];
    int32_t status = s->status;
    bool planned = s->planned;
    if (gone_status(status)) {
        px_lost(p, "device gone (status %d)", (int)status);
        return;
    }
    if (s->unlinked || cancelled_status(status)) {
        free_head(u, 1);
        out_run(p, u);
        return;
    }
    if (status == -YUSB_EPIPE) p->counters.remote_stalls++;
    else p->counters.transaction_errors++;
    free_head(u, 1);
    if (p->policy.out_window > 1) p->counters.late_out_stalls++;
    /* Write-behind (ADR 0028 §7): once the host has sent the whole BOT data
     * phase, a local halt would land on its next CBW, which BOT forbids;
     * the device's CSW reports the failure instead. */
    bool behind = u->bot && p->policy.bot_out_window > 1 && planned && !u->out_count;
    if (behind && p->policy.out_window <= 1) p->counters.late_out_stalls++;
    cancel_from(p, u, 0, NULL, NULL);
    u->wait = W_CANCEL;
    u->halt_after = !behind;
    if (!u->cancel_wait) out_after_cancel(p, u, u->halt_after);
}

/* proxy._plan: data-in sizes go to the IN pump, data-out sizes to reads. */
static void out_plan(yusb_proxy_t *p, pump_t *u, uint32_t length, bool data_in)
{
    unsigned packet = u->bot_in_packet;
    uint32_t csw = packet > YUSB_CSW_SIZE ? packet : YUSB_CSW_SIZE;
    if (!data_in && length) {
        if (u->out_count == sizeof(u->out_left) / sizeof(u->out_left[0])) {
            px_fault(p, "BOT data-out queue overflow");
        } else {
            u->out_left[(u->out_head + u->out_count++) % 4] = length;
        }
    }
    bot_command(p, u->bot, data_in ? length : 0, packet, csw);
}

static void out_submit(yusb_proxy_t *p, pump_t *u, uint32_t len)
{
    unsigned s = u->read_slot;
    uint8_t *buf = slot_buf(p, u, s);
    uint32_t length;
    bool data_in, cbw = false;
    if (u->bot && len == YUSB_CBW_SIZE && !u->read_planned &&
        yusb_parse_cbw(buf + YUSB_HEADROOM, len, &length, &data_in)) {
        out_plan(p, u, length, data_in);
        cbw = true;
    }
    cl_req_t q = {.ep = u->address & 0x0fu, .dir = YUSB_USBIP_DIR_OUT, .length = len, .buf = buf,
                  .interval = u->interval, .bulk = u->type == YUSB_XFER_BULK, .owner = O_PUMP,
                  .pump = (uint8_t)idx_of(u), .slot = (uint16_t)s};
    uint16_t urb = cl_submit(p, &q);
    if (urb == NO_URB) {
        u->slots[s].state = S_FREE;
        if (!p->lost) { px_fault(p, "endpoint 0x%02x: URB table full", u->address); dead(p, u); }
        return;
    }
    u->slots[s] = (slot_t){S_INFLIGHT, false, u->read_planned, urb, len, 0, 0};
    u->tx_mark = p->tx.queued;
    u->count++;
    p->counters.out_urbs++;
    p->counters.out_bytes += len;
    /* The CBW goes out before the IN URBs it sizes, as a host orders them. */
    if (cbw) pump_bot_wake(p, u->bot);
    px_kick_parked(p);
    out_run(p, u);
}

static void out_run(yusb_proxy_t *p, pump_t *u)
{
    if (u->run != P_RUN || u->busy || u->wait != W_NONE) return;
    while (u->count && u->slots[u->head].state == S_DONE) {
        slot_t *s = &u->slots[u->head];
        if (s->unlinked || s->status) {
            out_failure(p, u);
            return;
        }
        u->remote_halted = false;
        free_head(u, 1);
    }
    unsigned window = p->policy.out_window;
    if (u->bot && u->out_count && p->policy.bot_out_window > window) window = p->policy.bot_out_window;
    if (u->count >= window || u->count >= u->nslots) return;
    u->read_planned = u->out_count > 0;
    u->read_size = u->size;
    if (u->read_planned) {
        uint32_t *left = &u->out_left[u->out_head];
        u->read_size = *left < p->policy.max_transfer ? *left : p->policy.max_transfer;
        *left -= u->read_size;
        if (!*left) {
            u->out_head = (u->out_head + 1) % 4;
            u->out_count--;
        }
    }
    u->read_slot = ring(u, u->count);
    u->slots[u->read_slot].state = S_READING;
    start_read(p, u);
}

/* --- events ------------------------------------------------------------ */

static void host_cleared(yusb_proxy_t *p, pump_t *u)
{
    p->counters.host_clear_halts++;
    p->control.halted &= ~(1u << idx_of(u));
    uint32_t sizes[65];
    unsigned n = 0, from = u->in ? u->writing : 0;
    while (u->in && from < u->count && u->slots[ring(u, from)].state == S_DONE) from++;
    u->hc_from = from;
    u->hc_resume = u->wait;
    cancel_from(p, u, from, sizes, &n);
    if (u->in && u->bot) bot_requeue(p, u->bot, sizes, n);
    pump_bot_abort(p, u);
    u->wait = W_HCANCEL;
    if (!u->cancel_wait) {
        truncate_ring(u, u->hc_from);
        start_clear(p, u, W_HCLEAR);
    }
}

static void cancelled(yusb_proxy_t *p, pump_t *u)
{
    if (u->run == P_STOPPING) { check_stopped(p, u); return; }
    if (u->wait == W_CANCEL) {
        if (u->in) in_after_cancel(p, u);
        else out_after_cancel(p, u, u->halt_after);
    } else if (u->wait == W_HCANCEL) {
        truncate_ring(u, u->hc_from);
        start_clear(p, u, W_HCLEAR);
    }
}

void pump_urb(yusb_proxy_t *p, pump_t *u, uint16_t slot, const urb_result_t *r)
{
    if (!u->used || slot >= u->nslots) return;
    slot_t *s = &u->slots[slot];
    if (s->state == S_CANCEL) {
        if (u->cancel_wait && --u->cancel_wait == 0) cancelled(p, u);
        return;
    }
    if (s->state != S_INFLIGHT) return;
    s->state = S_DONE;
    s->status = r->status;
    s->actual = r->actual;
    s->unlinked = r->unlinked;
    if (r->lost) { dead(p, u); return; }
    if (u->in) in_run(p, u);
    else out_run(p, u);
}

void pump_clear_done(yusb_proxy_t *p, pump_t *u, const urb_result_t *r)
{
    u->clear_urb = NO_URB;
    if (u->run == P_STOPPING) { check_stopped(p, u); return; }
    if (r->lost || u->run != P_RUN) return;
    if (r->timeout || (r->status && r->status != -YUSB_EPIPE)) {
        px_fault(p, "endpoint 0x%02x: remote clear-halt failed (status %d)", u->address, (int)r->status);
        dead(p, u);
        return;
    }
    if (r->status == -YUSB_EPIPE) px_fault(p, "remote refused to clear halt on 0x%02x", u->address);
    uint8_t wait = u->wait;
    u->wait = W_NONE;
    switch (wait) {
    case W_CLEAR:
        if (u->in) { u->remote_halted = true; u->probing = true; in_run(p, u); }
        else out_run(p, u);        /* remote_halted was set by out_after_cancel */
        break;
    case W_HCLEAR:
        u->remote_halted = false;
        u->wait = u->hc_resume == W_PARTIAL ? W_PARTIAL : W_NONE;
        retry_io(p, u);
        break;
    case W_PRECLEAR:
        out_submit(p, u, u->wlen);
        break;
    default:
        break;
    }
}

static void in_write_done(yusb_proxy_t *p, pump_t *u, int result)
{
    if (result >= 0) {
        p->counters.gadget_writes++;
        p->counters.in_bytes += u->wlen;
        if (u->wait == W_PARTIAL) {
            u->wait = W_NONE;
            u->writing = 0;
            in_stall_cancel(p, u);
            return;
        }
        free_head(u, u->writing);
        u->writing = 0;
        in_run(p, u);
        return;
    }
    if (result == YUSB_GADGET_INTERRUPTED) { start_write(p, u); return; }
    if (result == YUSB_GADGET_HALT_CLEARED) { host_cleared(p, u); return; }
    if (result != YUSB_GADGET_SHUTDOWN) px_fault(p, "write 0x%02x: gadget error %d", u->address, result);
    dead(p, u);
}

static void out_read_done(yusb_proxy_t *p, pump_t *u, int result)
{
    if (result == YUSB_GADGET_INTERRUPTED) { start_read(p, u); return; }
    if (result == YUSB_GADGET_HALT_CLEARED) { host_cleared(p, u); return; }
    if (result < 0) {
        if (result != YUSB_GADGET_SHUTDOWN) px_fault(p, "read 0x%02x: gadget error %d", u->address, result);
        u->slots[u->read_slot].state = S_FREE;
        dead(p, u);
        return;
    }
    p->counters.gadget_reads++;
    bool late = false;
    for (unsigned k = 0; k < u->count && !late; ++k) {
        const slot_t *s = &u->slots[ring(u, k)];
        late = s->state == S_DONE && (s->status || s->unlinked);
    }
    if (late) {
        /* The UDC already acknowledged data the device would have refused
         * (out_window > 1 semantics). */
        p->counters.dropped_out++;
        u->slots[u->read_slot].state = S_FREE;
        out_run(p, u);
        return;
    }
    if (u->remote_halted && !u->count) {
        u->wlen = (uint32_t)result;   /* the host cleared; mirror it first */
        start_clear(p, u, W_PRECLEAR);
        return;
    }
    out_submit(p, u, (uint32_t)result);
}

void pump_ep_done(yusb_proxy_t *p, pump_t *u, int result)
{
    u->busy = false;
    u->cancelled = false;
    if (u->run == P_STOPPING) {
        if (!u->in) u->slots[u->read_slot].state = S_FREE;
        if (u->in && result >= 0) {
            p->counters.gadget_writes++;
            p->counters.in_bytes += u->wlen;
        }
        check_stopped(p, u);
        return;
    }
    if (u->run != P_RUN) return;
    if (u->in) in_write_done(p, u, result);
    else out_read_done(p, u, result);
}

void pump_start(yusb_proxy_t *p, pump_t *u)
{
    if (u->in) in_run(p, u);
    else out_run(p, u);
}

void pump_stop(yusb_proxy_t *p, pump_t *u)
{
    if (u->run == P_STOPPING || u->run == P_STOPPED) return;
    u->run = P_STOPPING;
    if (u->busy && !u->cancelled) {
        u->cancelled = true;
        p->ops.ep_cancel(p->ops.ctx, u->handle);
    }
    if (u->clear_urb != NO_URB) {
        cl_orphan(p, u->clear_urb);
        u->clear_urb = NO_URB;
    }
    u->wait = W_NONE;
    unsigned already = u->cancel_wait;
    for (unsigned k = 0; k < u->count; ++k) {
        slot_t *s = &u->slots[ring(u, k)];
        if (s->state == S_INFLIGHT && !p->lost) {
            cl_cancel(p, s->urb);
            s->state = S_CANCEL;
            u->cancel_wait++;
        } else if (s->state != S_CANCEL) {
            s->state = S_CANCEL;
        }
    }
    if (u->cancel_wait != already || u->busy) {
        u->cancel_deadline = p->now + p->policy.stop_timeout_ms;
        px_deadline(p, u->cancel_deadline);
    }
    check_stopped(p, u);
}

void pump_poll(yusb_proxy_t *p, pump_t *u)
{
    if (u->wait == W_PARKED && u->run == P_RUN) {
        if (p->now >= u->parked_until) unpark(p, u);
        else px_deadline(p, u->parked_until);
    }
    bool waiting = u->cancel_wait || (u->run == P_STOPPING && u->busy);
    if (!waiting || !u->cancel_deadline) return;
    if (p->now < u->cancel_deadline) { px_deadline(p, u->cancel_deadline); return; }
    u->cancel_deadline = 0;
    if (u->run == P_STOPPING) {
        px_end(p, YUSB_END_FAULT, "teardown unconfirmed: endpoint pumps did not stop: 0x%02x",
               u->address);
    } else {
        px_fault(p, "endpoint 0x%02x: unlink unconfirmed", u->address);
        dead(p, u);
    }
}

void pump_kick(yusb_proxy_t *p, pump_t *u)
{
    if (u->run != P_RUN || u->wait != W_PARKED) return;
    u->kicked = true;
    unpark(p, u);
}

void pump_bot_wake(yusb_proxy_t *p, bot_t *b)
{
    if (b && b->in_pump >= 0 && p->pumps[b->in_pump].used) in_run(p, &p->pumps[b->in_pump]);
}
