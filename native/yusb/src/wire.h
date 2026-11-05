#pragma once
/* Private: stream framing shared by the proxy and export cores.
 *
 * rx_t receives the USB/IP byte stream as a sequence of elements the owner
 * announces one at a time: a fixed-size record (headers), a payload into
 * its final buffer, a discard, or a YX block stream. Small elements are
 * parsed from a staging buffer (many messages per recv); a large payload
 * remainder is received straight into its destination (no copy).
 *
 * txq_t is the ordered list of transmit segments; the owner keeps the
 * memory valid until the bytes are reported sent.
 */
#include "yusb_yx.h"

#define RX_STAGE_MAX 16384u
#define RX_STAGE_MIN 2048u
#define RX_DIRECT_MIN 4096u
#define RX_RECORD_MAX 320u
#define RX_WAIT 1   /* element handler: complete, but cannot proceed yet */

typedef enum { RX_NONE, RX_RECORD, RX_PAYLOAD, RX_DISCARD, RX_DECODE } rx_kind_t;

typedef struct {
    rx_kind_t kind;
    bool dead, blocked;
    size_t record_size, have;
    uint8_t *dst;
    size_t remaining;
    yusb_yx_dec_t *dec;
    size_t stage_len, stage_pos;
    uint8_t direct;          /* 1: last space was the element's own buffer */
    size_t stage_size;
    uint8_t *stage;
    uint8_t record[RX_RECORD_MAX];
} rx_t;

/* Called when the current element is complete. It announces the next one
 * and returns 0, RX_WAIT (call rx_resume later) or < 0 (protocol error). */
typedef int (*rx_element_fn)(void *owner);

/* Staging sized for the largest expected transfer, within 2..16 KiB. */
size_t rx_stage_size(size_t transfer);
int rx_init(rx_t *rx, size_t stage_size);
void rx_free(rx_t *rx);
void rx_record(rx_t *rx, size_t size);
void rx_payload(rx_t *rx, uint8_t *dst, size_t len);
void rx_discard(rx_t *rx, size_t len);
void rx_decode(rx_t *rx, yusb_yx_dec_t *dec);
size_t rx_space(rx_t *rx, uint8_t **buf);
int rx_done(rx_t *rx, size_t n, rx_element_fn fn, void *owner);
/* Re-run the waiting element's handler and continue with staged bytes. */
int rx_resume(rx_t *rx, rx_element_fn fn, void *owner);

typedef struct {
    yusb_iov_t *ring;
    size_t cap, head, count, offset;  /* offset: bytes of ring[head] already sent */
    uint64_t queued, sent;            /* absolute byte counts */
} txq_t;

int txq_init(txq_t *q, size_t cap);
void txq_free(txq_t *q);
/* false if the ring is full (a sizing bug; callers treat it as fatal). */
bool txq_push(txq_t *q, const void *base, size_t len);
size_t txq_iov(const txq_t *q, yusb_iov_t *iov, size_t max);
void txq_done(txq_t *q, size_t n);
static inline bool txq_room(const txq_t *q, size_t segments) { return q->cap - q->count >= segments; }
