/* Private stream framing (see wire.h). */
#include <stdlib.h>
#include <string.h>
#include "wire.h"

size_t rx_stage_size(size_t transfer)
{
    return transfer < RX_STAGE_MIN ? RX_STAGE_MIN : transfer > RX_STAGE_MAX ? RX_STAGE_MAX : transfer;
}

int rx_init(rx_t *rx, size_t stage_size)
{
    memset(rx, 0, sizeof(*rx));
    rx->kind = RX_NONE;
    rx->stage_size = stage_size;
    rx->stage = malloc(stage_size);
    return rx->stage ? YUSB_OK : YUSB_ERR_NOMEM;
}

void rx_free(rx_t *rx)
{
    free(rx->stage);
    rx->stage = NULL;
}

void rx_record(rx_t *rx, size_t size)
{
    rx->kind = RX_RECORD;
    rx->record_size = size;
    rx->have = 0;
}

void rx_payload(rx_t *rx, uint8_t *dst, size_t len)
{
    rx->kind = RX_PAYLOAD;
    rx->dst = dst;
    rx->remaining = len;
}

void rx_discard(rx_t *rx, size_t len)
{
    rx->kind = RX_DISCARD;
    rx->remaining = len;
}

void rx_decode(rx_t *rx, yusb_yx_dec_t *dec)
{
    rx->kind = RX_DECODE;
    rx->dec = dec;
}

/* Bytes the current element can take, and where (element's own buffer). */
static size_t element_space(rx_t *rx, uint8_t **buf)
{
    switch (rx->kind) {
    case RX_RECORD: *buf = rx->record + rx->have; return rx->record_size - rx->have;
    case RX_PAYLOAD: *buf = rx->dst; return rx->remaining;
    case RX_DECODE: return yusb_yx_dec_space(rx->dec, buf);
    default: *buf = NULL; return rx->remaining;
    }
}

/* n bytes are now at the element's buffer; 1 when the element completed. */
static int element_take(rx_t *rx, size_t n)
{
    switch (rx->kind) {
    case RX_RECORD:
        rx->have += n;
        return rx->have == rx->record_size;
    case RX_PAYLOAD:
        rx->dst += n;
        rx->remaining -= n;
        return rx->remaining == 0;
    case RX_DISCARD:
        rx->remaining -= n;
        return rx->remaining == 0;
    case RX_DECODE: {
        uint8_t *next;
        if (yusb_yx_dec_feed(rx->dec, n)) return -1;
        return yusb_yx_dec_space(rx->dec, &next) == 0;
    }
    default:
        return -1;
    }
}

size_t rx_space(rx_t *rx, uint8_t **buf)
{
    if (rx->dead || rx->blocked || rx->stage_pos < rx->stage_len) return 0;
    rx->stage_len = rx->stage_pos = 0;
    if (rx->kind == RX_PAYLOAD || rx->kind == RX_DECODE) {
        size_t n = element_space(rx, buf);
        if (n >= RX_DIRECT_MIN || n >= rx->stage_size) {
            rx->direct = 1;
            return n;
        }
    }
    rx->direct = 0;
    *buf = rx->stage;
    return rx->stage_size;
}

static int fail(rx_t *rx)
{
    rx->dead = true;
    return YUSB_ERR_PROTOCOL;
}

static int complete(rx_t *rx, rx_element_fn fn, void *owner)
{
    rx->kind = RX_NONE;
    int rc = fn(owner);
    if (rc < 0) return fail(rx);
    if (rc == RX_WAIT) rx->blocked = true;
    return YUSB_OK;
}

/* Consume staged bytes into successive elements. */
static int drain(rx_t *rx, rx_element_fn fn, void *owner)
{
    while (!rx->dead && !rx->blocked && rx->stage_pos < rx->stage_len) {
        if (rx->kind == RX_NONE) return fail(rx);
        size_t avail = rx->stage_len - rx->stage_pos;
        uint8_t *buf;
        size_t want = element_space(rx, &buf);
        size_t n = want < avail ? want : avail;
        if (buf && n) memcpy(buf, rx->stage + rx->stage_pos, n);
        rx->stage_pos += n;
        int done = element_take(rx, n);
        if (done < 0) return fail(rx);
        if (done && complete(rx, fn, owner)) return YUSB_ERR_PROTOCOL;
    }
    return rx->dead ? YUSB_ERR_PROTOCOL : YUSB_OK;
}

int rx_done(rx_t *rx, size_t n, rx_element_fn fn, void *owner)
{
    if (rx->dead) return YUSB_ERR_PROTOCOL;
    if (rx->direct) {
        rx->direct = 0;
        int done = element_take(rx, n);
        if (done < 0) return fail(rx);
        if (done) return complete(rx, fn, owner);
        return YUSB_OK;
    }
    rx->stage_len += n;
    return drain(rx, fn, owner);
}

int rx_resume(rx_t *rx, rx_element_fn fn, void *owner)
{
    if (!rx->blocked || rx->dead) return rx->dead ? YUSB_ERR_PROTOCOL : YUSB_OK;
    rx->blocked = false;
    if (complete(rx, fn, owner)) return YUSB_ERR_PROTOCOL;
    return drain(rx, fn, owner);
}

int txq_init(txq_t *q, size_t cap)
{
    memset(q, 0, sizeof(*q));
    q->ring = calloc(cap, sizeof(*q->ring));
    if (!q->ring) return YUSB_ERR_NOMEM;
    q->cap = cap;
    return YUSB_OK;
}

void txq_free(txq_t *q)
{
    free(q->ring);
    q->ring = NULL;
}

bool txq_push(txq_t *q, const void *base, size_t len)
{
    if (!len) return true;
    if (q->count == q->cap) return false;
    yusb_iov_t *slot = &q->ring[(q->head + q->count) % q->cap];
    slot->base = base;
    slot->len = len;
    q->count++;
    q->queued += len;
    return true;
}

size_t txq_iov(const txq_t *q, yusb_iov_t *iov, size_t max)
{
    size_t n = 0;
    for (; n < max && n < q->count; ++n) {
        iov[n] = q->ring[(q->head + n) % q->cap];
        if (n == 0) {
            iov[0].base += q->offset;
            iov[0].len -= q->offset;
        }
    }
    return n;
}

void txq_done(txq_t *q, size_t n)
{
    q->sent += n;
    while (n && q->count) {
        size_t left = q->ring[q->head].len - q->offset;
        if (n < left) {
            q->offset += n;
            return;
        }
        n -= left;
        q->offset = 0;
        q->head = (q->head + 1) % q->cap;
        q->count--;
    }
}
