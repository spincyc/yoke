/* USB/IP client half of the proxy core (reference: client.py): the import
 * handshake, the URB table keyed by sequence number, unlinks with
 * tombstones, and the receive dispatch.
 *
 * Unlink races (stub_tx sends every queued RET_SUBMIT before every queued
 * RET_UNLINK on each pass):
 *  - RET_UNLINK(-ECONNRESET/-ENOENT) resolves its target as unlinked and
 *    tombstones the sequence number, so a straggling RET_SUBMIT is consumed;
 *  - RET_UNLINK carrying the URB's own status (0, -EPIPE) is ambiguous: the
 *    URB gets unlink_grace_ms for a late RET_SUBMIT, whose data it keeps,
 *    then it is abandoned (tombstoned) with that status.
 * Neither ever drops the connection. */
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "proxy_int.h"

enum { RS_OP, RS_DEVICE, RS_HEADER, RS_PAYLOAD, RS_LATE };

static uint32_t get32(const uint8_t *p)
{
    return (uint32_t)p[0] << 24 | (uint32_t)p[1] << 16 | (uint32_t)p[2] << 8 | p[3];
}

/* Every presentable configuration fits the UDC budget, so at most
 * max_in + max_out pumps exist; each keeps at most `depth` URBs in flight,
 * each possibly with a CMD_UNLINK entry, plus control traffic. */
void cl_sizes(const yusb_policy_t *pol, unsigned *nurbs, unsigned *ntombs, size_t *stage)
{
    unsigned depth = pol->in_depth_bulk;
    unsigned extra[] = {pol->in_depth_interrupt, pol->out_window, pol->bot_out_window,
                        pol->bot_in_slots};
    for (unsigned i = 0; i < 4; ++i) if (extra[i] > depth) depth = extra[i];
    unsigned pumps = (unsigned)pol->budget.max_in + pol->budget.max_out;
    *nurbs = 2u * (pumps * depth + 16u);
    /* Unlinked URBs whose RET_SUBMIT may still arrive: never more than the
     * table holds (the reference keeps 4096). */
    *ntombs = *nurbs;
    *stage = rx_stage_size(pol->max_transfer);
}

/* Transmit segments: a header and a payload per URB and per CMD_UNLINK,
 * plus OP traffic; a YX bulk OUT payload is sent as up to
 * yusb_yx_segments() pieces (RAW bodies straight from the slot buffer),
 * and at most max_out x window of them are in flight. */
size_t cl_tx_segments(const yusb_policy_t *pol, unsigned nurbs)
{
    size_t cap = 2u * nurbs + 8u;
    yusb_yx_params_t offer = {YUSB_YX_VERSION, pol->yx_levers, pol->yx_cache_blocks};
    if (yusb_yx_framed(&offer)) {
        unsigned window = pol->out_window > pol->bot_out_window ? pol->out_window : pol->bot_out_window;
        cap += (size_t)pol->budget.max_out * window * yusb_yx_segments(pol->max_transfer);
    }
    return cap;
}

int cl_init(yusb_proxy_t *p)
{
    const yusb_policy_t *pol = &p->policy;
    size_t stage;
    cl_sizes(pol, &p->nurbs, &p->ntombs, &stage);
    p->urbs = calloc(p->nurbs, sizeof(*p->urbs));
    p->tombs = calloc(p->ntombs, sizeof(*p->tombs));
    if (!p->urbs || !p->tombs) return YUSB_ERR_NOMEM;
    if (txq_init(&p->tx, cl_tx_segments(pol, p->nurbs)) || rx_init(&p->rx, stage)) return YUSB_ERR_NOMEM;
    p->receiving = NO_URB;
    yusb_yx_params_t offer = {YUSB_YX_VERSION, pol->yx_levers, pol->yx_cache_blocks};
    if (yusb_yx_framed(&offer)) {
        p->yx_nseg = yusb_yx_segments(pol->max_transfer);
        p->yx_seg = calloc(p->yx_nseg, sizeof(*p->yx_seg));
        if (!p->yx_seg) return YUSB_ERR_NOMEM;
    }
    yusb_req_import_encode(p->busid, p->op_out);
    if (pol->yx_levers)
        yusb_yx_offer_put(p->op_out + YUSB_OP_HEADER_SIZE, &offer);  /* refused if busid too long */
    txq_push(&p->tx, p->op_out, sizeof(p->op_out));
    rx_record(&p->rx, YUSB_OP_HEADER_SIZE);
    p->rstate = RS_OP;
    return YUSB_OK;
}

void cl_free_yx(yusb_proxy_t *p)
{
    if (p->enc) yusb_yx_enc_free(p->enc);
    if (p->dec) yusb_yx_dec_free(p->dec);
    free(p->enc);
    free(p->dec);
    free(p->late_buf);
    p->enc = NULL;
    p->dec = NULL;
    p->late_buf = NULL;
    p->yx = false;
}

void cl_free(yusb_proxy_t *p)
{
    cl_free_yx(p);
    free(p->yx_seg);
    free(p->urbs);
    free(p->tombs);
    txq_free(&p->tx);
    rx_free(&p->rx);
}

#define ORPHAN_RETRY_MS 100u

static uint16_t find(const yusb_proxy_t *p, uint32_t seqnum)
{
    unsigned i = seqnum % p->nurbs;
    return p->urbs[i].state != U_FREE && p->urbs[i].seqnum == seqnum ? (uint16_t)i : NO_URB;
}

/* A free entry whose header bytes are no longer referenced by the tx queue.
 * Sequence numbers map to entries directly (seqnum % nurbs). */
static uint16_t alloc_entry(yusb_proxy_t *p)
{
    for (unsigned tries = 0; tries < p->nurbs; ++tries) {
        p->seq = p->seq % 0xffffffffu + 1u;
        unsigned i = p->seq % p->nurbs;
        urb_t *u = &p->urbs[i];
        if (u->state == U_FREE && u->tx_end <= p->tx.sent) {
            memset(u, 0, sizeof(*u));
            u->seqnum = p->seq;
            u->unlink = NO_URB;
            return (uint16_t)i;
        }
    }
    return NO_URB;
}

static void tombstone(yusb_proxy_t *p, uint32_t seqnum, uint8_t dir)
{
    unsigned at = (p->tomb_head + p->tomb_count) % p->ntombs;
    if (p->tomb_count == p->ntombs) p->tomb_head = (p->tomb_head + 1) % p->ntombs;
    else p->tomb_count++;
    p->tombs[at].seqnum = seqnum;
    p->tombs[at].dir = dir;
}

/* Rare path (a straggler): a linear search is fine. 0 marks a used entry. */
static int tomb_take(yusb_proxy_t *p, uint32_t seqnum)
{
    for (unsigned k = 0; k < p->tomb_count; ++k) {
        tomb_t *t = &p->tombs[(p->tomb_head + p->tomb_count - 1 - k) % p->ntombs];
        if (t->seqnum == seqnum) {
            t->seqnum = 0;
            return t->dir;
        }
    }
    return -1;
}

uint16_t cl_submit(yusb_proxy_t *p, const cl_req_t *q)
{
    if (p->lost || !txq_room(&p->tx, 2)) return NO_URB;
    uint16_t i = alloc_entry(p);
    if (i == NO_URB) return NO_URB;
    urb_t *u = &p->urbs[i];
    u->state = U_PENDING;
    u->owner = q->owner;
    u->pump = q->pump;
    u->slot = q->slot;
    u->dir = (uint8_t)q->dir;
    u->bulk = q->bulk;
    u->length = q->length;
    u->buf = q->buf;
    if (q->timeout_ms) {
        u->deadline = p->now + q->timeout_ms;
        px_deadline(p, u->deadline);
    }
    uint32_t flags = q->dir == YUSB_USBIP_DIR_IN ? YUSB_URB_DIR_IN : 0;
    yusb_usbip_hdr_t h = yusb_cmd_submit(u->seqnum, p->devid, q->dir, q->ep, flags, q->length,
                                         q->interval, q->setup);
    const uint8_t *payload = q->buf ? q->buf + YUSB_HEADROOM : NULL;
    size_t length = q->length;
    yusb_usbip_encode(&h, u->hdr);
    /* ADR 0028: bulk OUT payloads may travel as a YX block stream; headers
     * and coded bodies go to the room the pump placed after the slot's raw
     * buffer, RAW bodies are sent from the slot buffer itself. */
    if (q->dir == YUSB_USBIP_DIR_OUT && p->yx &&
        yusb_yx_eligible(q->bulk ? YUSB_XFER_BULK : 0, length)) {
        yusb_iov_t one, *seg = p->yx_seg ? p->yx_seg : &one;
        size_t wire = 0, n = 0, want = yusb_yx_segments(length);
        if (p->yx_seg && want <= p->yx_nseg && txq_room(&p->tx, 1 + want))
            n = yusb_yx_encode_iov(p->enc, payload, length, q->buf + YUSB_HEADROOM + length, seg,
                                   want, &wire);
        if (!n) {             /* no segment room: one copied stream */
            seg[0].base = q->buf + YUSB_HEADROOM + length;
            seg[0].len = yusb_yx_encode(p->enc, payload, length, q->buf + YUSB_HEADROOM + length);
            wire = seg[0].len;
            n = 1;
        }
        p->counters.yx_raw_out += length;
        p->counters.yx_wire_out += wire;
        yusb_yx_frame(u->hdr, true);
        txq_push(&p->tx, u->hdr, sizeof(u->hdr));
        for (size_t k = 0; k < n; ++k) txq_push(&p->tx, seg[k].base, seg[k].len);
        u->tx_end = p->tx.queued;
        return i;
    }
    txq_push(&p->tx, u->hdr, sizeof(u->hdr));
    if (q->dir == YUSB_USBIP_DIR_OUT && length) txq_push(&p->tx, payload, length);
    u->tx_end = p->tx.queued;
    return i;
}

uint16_t cl_control(yusb_proxy_t *p, const yusb_setup_t *setup, uint8_t *buf, uint8_t owner,
                    uint8_t pump, uint64_t timeout_ms)
{
    /* Linux carries an IN request with wLength 0 as an OUT URB. */
    unsigned dir = yusb_setup_has_in_data(setup) ? YUSB_USBIP_DIR_IN : YUSB_USBIP_DIR_OUT;
    uint8_t raw[8];
    yusb_setup_pack(setup, raw);
    cl_req_t q = {.ep = 0, .dir = dir, .length = setup->length, .buf = buf, .setup = raw,
                  .owner = owner, .pump = pump, .slot = NO_URB, .timeout_ms = timeout_ms};
    return cl_submit(p, &q);
}

void cl_cancel(yusb_proxy_t *p, uint16_t index)
{
    urb_t *u = &p->urbs[index];
    if (u->state != U_PENDING || p->lost) return;
    if (!txq_room(&p->tx, 1)) return;
    uint16_t k = alloc_entry(p);
    if (k == NO_URB) {
        px_log(p, "unlink of seqnum %u deferred: URB table full", (unsigned)u->seqnum);
        return;
    }
    urb_t *v = &p->urbs[k];
    v->state = U_UNLINK;
    v->target = index;
    v->target_seq = u->seqnum;
    yusb_usbip_hdr_t h = yusb_cmd_unlink(v->seqnum, p->devid, u->seqnum);
    yusb_usbip_encode(&h, v->hdr);
    txq_push(&p->tx, v->hdr, sizeof(v->hdr));
    v->tx_end = p->tx.queued;
    u->state = U_UNLINKING;
    u->unlink = k;
    u->deadline = 0;
    p->counters.unlinks++;
}

/* An abandoned URB keeps its table entry until the exporter answers, so it
 * is unlinked (the table cannot fill with orphans), and a late IN payload is
 * discarded rather than written to a buffer its owner has reused. */
void cl_orphan(yusb_proxy_t *p, uint16_t index)
{
    if (index == NO_URB || p->urbs[index].state == U_FREE || p->urbs[index].state == U_UNLINK) return;
    urb_t *u = &p->urbs[index];
    u->owner = O_NONE;
    u->buf = NULL;
    u->deadline = 0;
    cl_cancel(p, index);
    if (u->state == U_PENDING && !p->lost) {   /* unlink deferred: retry from cl_poll */
        u->deadline = p->now + ORPHAN_RETRY_MS;
        px_deadline(p, u->deadline);
    }
}

bool cl_urb_receiving(const yusb_proxy_t *p, uint16_t index) { return p->receiving == index; }

/* Resolve entry i: free it first (owners may submit again), then tell its
 * owner. A pending CMD_UNLINK for it forgets its target. */
static void resolve(yusb_proxy_t *p, uint16_t i, const urb_result_t *r)
{
    urb_t copy = p->urbs[i];
    if (copy.unlink != NO_URB && p->urbs[copy.unlink].state == U_UNLINK)
        p->urbs[copy.unlink].target = NO_URB;
    p->urbs[i].state = U_FREE;
    px_urb_done(p, &copy, i, r);
}

void cl_fail_all(yusb_proxy_t *p)
{
    p->lost = true;
    urb_result_t r = {-YUSB_ESHUTDOWN, 0, false, true, false, NULL};
    for (unsigned i = 0; i < p->nurbs; ++i) {
        if (p->urbs[i].state == U_FREE) continue;
        if (p->urbs[i].state == U_UNLINK) { p->urbs[i].state = U_FREE; continue; }
        resolve(p, (uint16_t)i, &r);
    }
    p->receiving = NO_URB;
}

static int protocol(yusb_proxy_t *p, const char *fmt, ...)
{
    char text[160];
    va_list args;
    va_start(args, fmt);
    vsnprintf(text, sizeof(text), fmt, args);
    va_end(args);
    px_lost(p, "%s", text);
    return YUSB_ERR_PROTOCOL;
}

static void complete(yusb_proxy_t *p, uint16_t i)
{
    urb_t *u = &p->urbs[i];
    if (u->state == U_ACKED) p->counters.late_ret_submits++;   /* RET_UNLINK(own status) came first */
    urb_result_t r = {p->rhdr.status, p->rhdr.actual_length, false, false, false,
                      u->buf ? u->buf + YUSB_HEADROOM : NULL};
    resolve(p, i, &r);
}

static int on_import(yusb_proxy_t *p)
{
    uint16_t code;
    uint32_t status;
    if (yusb_op_decode(p->rx.record, &code, &status))
        return protocol(p, "unsupported USB/IP version %u", (unsigned)(p->rx.record[0] << 8 | p->rx.record[1]));
    if (code != YUSB_OP_REP_IMPORT) return protocol(p, "expected op 0x0003, got 0x%04x", code);
    if (status) {
        px_end(p, YUSB_END_IMPORT_REFUSED, "exporter refused busid '%s' (status %u)", p->busid,
               (unsigned)status);
        return YUSB_ERR_PROTOCOL;
    }
    rx_record(&p->rx, YUSB_DEVICE_INFO_SIZE);
    p->rstate = RS_DEVICE;
    return YUSB_OK;
}

static int on_device(yusb_proxy_t *p)
{
    if (yusb_device_info_decode(p->rx.record, &p->info))
        return protocol(p, "device record is malformed");
    if (strcmp(p->info.busid, p->busid)) {
        px_lost(p, "imported busid '%s', requested '%s'", p->info.busid, p->busid);
        return YUSB_ERR_PROTOCOL;
    }
    yusb_yx_params_t offer, accepted, agreed = {0, 0, 0};
    /* An exporter can never grant more than was offered. */
    if (p->policy.yx_levers && yusb_yx_offer_get(p->op_out + YUSB_OP_HEADER_SIZE, &offer) &&
        yusb_yx_accept_get(p->rx.record, &accepted) && accepted.version == YUSB_YX_VERSION)
        yusb_yx_agree(&offer, &accepted, &agreed);
    if (yusb_yx_framed(&agreed)) {
        size_t late = YUSB_HEADROOM + (p->policy.max_transfer > p->policy.ep0_max ? p->policy.max_transfer : p->policy.ep0_max);
        p->late_buf = calloc(1, late);
        p->enc = calloc(1, sizeof(*p->enc));
        p->dec = calloc(1, sizeof(*p->dec));
        if (!p->late_buf || !p->enc || !p->dec || yusb_yx_enc_init(p->enc, &agreed) ||
            yusb_yx_dec_init(p->dec, &agreed)) {
            cl_free_yx(p);
            px_end(p, YUSB_END_YX_UNAVAILABLE, "cannot allocate the YX codec: reconnect without the offer");
            return YUSB_ERR_PROTOCOL;
        }
        p->yx = true;
        p->yxp = agreed;
    }
    p->devid = yusb_devid(&p->info);
    rx_record(&p->rx, YUSB_USBIP_HEADER_SIZE);
    p->rstate = RS_HEADER;
    px_imported(p);
    return YUSB_OK;
}

/* Receive the payload of entry i (or of a tombstoned URB into scratch). */
static void receive(yusb_proxy_t *p, uint8_t *dst, uint32_t actual)
{
    if (p->rhdr_yx) {
        /* A YX stream is always decoded, so the cache stays in step. */
        yusb_yx_dec_begin(p->dec, dst ? dst : p->late_buf + YUSB_HEADROOM, actual);
        rx_decode(&p->rx, p->dec);
    } else if (dst) {
        rx_payload(&p->rx, dst, actual);
    } else {
        rx_discard(&p->rx, actual);
    }
}

static int on_ret_submit(yusb_proxy_t *p)
{
    const yusb_usbip_hdr_t *h = &p->rhdr;
    uint16_t i = find(p, h->seqnum);
    if (i == NO_URB || p->urbs[i].state == U_UNLINK) {
        int dir = tomb_take(p, h->seqnum);
        if (dir < 0) return protocol(p, "RET_SUBMIT for unknown seqnum %u", (unsigned)h->seqnum);
        p->counters.late_ret_submits++;
        if (dir == YUSB_USBIP_DIR_IN && h->actual_length) {
            if (p->rhdr_yx && h->actual_length > p->policy.ep0_max && h->actual_length > p->policy.max_transfer)
                return protocol(p, "late YX RET_SUBMIT of %u bytes", (unsigned)h->actual_length);
            receive(p, NULL, h->actual_length);
            p->rstate = RS_LATE;
            return YUSB_OK;
        }
        rx_record(&p->rx, YUSB_USBIP_HEADER_SIZE);
        return YUSB_OK;
    }
    urb_t *u = &p->urbs[i];
    if (h->actual_length > u->length)
        return protocol(p, "RET_SUBMIT actual_length exceeds the request");
    if (p->rhdr_yx && (u->dir != YUSB_USBIP_DIR_IN || !u->bulk || !h->actual_length))
        return protocol(p, "YX payload for an ineligible URB %u", (unsigned)h->seqnum);
    if (u->dir == YUSB_USBIP_DIR_IN && h->actual_length) {
        p->receiving = i;     /* stays pending until its payload is in */
        receive(p, u->buf ? u->buf + YUSB_HEADROOM : NULL, h->actual_length);
        p->rstate = RS_PAYLOAD;
        return YUSB_OK;
    }
    rx_record(&p->rx, YUSB_USBIP_HEADER_SIZE);
    p->rstate = RS_HEADER;
    complete(p, i);
    return YUSB_OK;
}

static int on_ret_unlink(yusb_proxy_t *p)
{
    const yusb_usbip_hdr_t *h = &p->rhdr;
    uint16_t k = find(p, h->seqnum);
    if (k == NO_URB || p->urbs[k].state != U_UNLINK)
        return protocol(p, "RET_UNLINK for unknown seqnum %u", (unsigned)h->seqnum);
    uint16_t t = p->urbs[k].target;
    uint32_t target_seq = p->urbs[k].target_seq;
    p->urbs[k].state = U_FREE;
    rx_record(&p->rx, YUSB_USBIP_HEADER_SIZE);
    if (t == NO_URB || p->urbs[t].seqnum != target_seq || p->urbs[t].state != U_UNLINKING)
        return YUSB_OK;       /* the URB already completed */
    urb_t *u = &p->urbs[t];
    u->unlink = NO_URB;
    if (h->status == -YUSB_ECONNRESET || h->status == -YUSB_ENOENT) {
        tombstone(p, u->seqnum, u->dir);
        urb_result_t r = {h->status, 0, true, false, false, NULL};
        resolve(p, t, &r);
    } else {
        u->state = U_ACKED;
        u->ack_status = h->status;
        u->deadline = p->now + p->policy.unlink_grace_ms;
        px_deadline(p, u->deadline);
    }
    return YUSB_OK;
}

static int on_header(yusb_proxy_t *p)
{
    uint8_t raw[YUSB_USBIP_HEADER_SIZE];
    memcpy(raw, p->rx.record, sizeof(raw));
    p->rhdr_yx = false;
    if (p->yx && yusb_yx_unframe(raw)) p->rhdr_yx = true;
    if (yusb_usbip_decode(raw, &p->rhdr))
        return protocol(p, "malformed USB/IP header (command 0x%08x)", (unsigned)get32(raw));
    if (p->rhdr.command == YUSB_RET_SUBMIT) return on_ret_submit(p);
    if (p->rhdr.command == YUSB_RET_UNLINK) return on_ret_unlink(p);
    return protocol(p, "exporter sent a command, not a reply (0x%08x)", (unsigned)p->rhdr.command);
}

static void count_yx_in(yusb_proxy_t *p)
{
    if (!p->rhdr_yx) return;
    p->counters.yx_raw_in = p->dec->stats.raw_bytes;
    p->counters.yx_wire_in = p->dec->stats.wire_bytes;
}

int cl_rx_element(void *owner)
{
    yusb_proxy_t *p = owner;
    if (p->lost) return YUSB_ERR_PROTOCOL;
    switch (p->rstate) {
    case RS_OP: return on_import(p);
    case RS_DEVICE: return on_device(p);
    case RS_HEADER: return on_header(p);
    case RS_PAYLOAD: {
        uint16_t i = p->receiving;
        p->receiving = NO_URB;
        count_yx_in(p);
        rx_record(&p->rx, YUSB_USBIP_HEADER_SIZE);
        p->rstate = RS_HEADER;
        if (i != NO_URB && p->urbs[i].state != U_FREE) complete(p, i);
        return YUSB_OK;
    }
    case RS_LATE:
        count_yx_in(p);
        rx_record(&p->rx, YUSB_USBIP_HEADER_SIZE);
        p->rstate = RS_HEADER;
        return YUSB_OK;
    default:
        return YUSB_ERR_PROTOCOL;
    }
}

void cl_poll(yusb_proxy_t *p)
{
    for (unsigned i = 0; i < p->nurbs; ++i) {
        urb_t *u = &p->urbs[i];
        if (!u->deadline) continue;
        if (u->deadline > p->now) {
            px_deadline(p, u->deadline);
            continue;
        }
        if ((uint16_t)i == p->receiving) {
            px_deadline(p, p->now + 1);   /* its payload is arriving: decide after */
        } else if (u->state == U_ACKED) {
            /* The grace for a late RET_SUBMIT expired: abandon it. */
            tombstone(p, u->seqnum, u->dir);
            urb_result_t r = {u->ack_status, 0, true, false, false, NULL};
            resolve(p, (uint16_t)i, &r);
        } else if (u->state == U_PENDING && u->owner == O_NONE) {
            cl_orphan(p, (uint16_t)i);    /* retry a deferred unlink */
        } else if (u->state == U_PENDING) {
            urb_t copy = *u;
            cl_orphan(p, (uint16_t)i);
            urb_result_t r = {-YUSB_ETIMEDOUT, 0, false, false, true, NULL};
            px_urb_done(p, &copy, i, &r);
        } else {
            u->deadline = 0;
        }
    }
}
