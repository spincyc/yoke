/* YX payload codec: negotiation bytes, block-stream encoder and incremental
 * decoder (ADR 0028 sections 2-6, yusb_yx.h). Portable C11: no OS calls, and
 * memory is allocated only in *_init and released in *_free.
 *
 * Sender policy (not wire): FILL first (smallest power-of-two period below
 * the block length), then REF (full blocks, mirror holds identical bytes),
 * then LZ4 (kept only when it saves at least one eighth), else RAW. After
 * LZ4_MISS_LIMIT consecutive misses the encoder skips LZ4 for the next
 * LZ4_SKIP_BLOCKS candidate blocks, then probes once; a missed probe skips
 * again and a success clears the miss count. */
#include <stdlib.h>
#include <string.h>
#include "lz4.h"
#define XXH_INLINE_ALL         /* static copies: no exported XXH symbols */
#include "xxhash.h"
#include "yusb_yx.h"

#define OFFER_AT 24u           /* busid field: string must end before this */
#define ACCEPT_AT 248u         /* path field: string must end before this */
#define LZ4_MISS_LIMIT 8u
#define LZ4_SKIP_BLOCKS 128u
#define DEC_FAILED 0x80u       /* in d->levers, outside YUSB_YX_LEVERS; sticky */

static void put_be16(uint8_t *p, uint16_t v)
{
    p[0] = (uint8_t)(v >> 8);
    p[1] = (uint8_t)v;
}

static void put_be32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v >> 24);
    p[1] = (uint8_t)(v >> 16);
    p[2] = (uint8_t)(v >> 8);
    p[3] = (uint8_t)v;
}

static uint16_t get_be16(const uint8_t *p)
{
    return (uint16_t)((unsigned)p[0] << 8 | p[1]);
}

static uint32_t get_be32(const uint8_t *p)
{
    return (uint32_t)p[0] << 24 | (uint32_t)p[1] << 16 | (uint32_t)p[2] << 8 | p[3];
}

/* ---- negotiation (ADR 0028 section 2) ---- */

static int params_put(uint8_t *field, size_t at, const yusb_yx_params_t *p)
{
    if (!field || !p || !p->version || (p->levers & ~YUSB_YX_LEVERS)) return YUSB_ERR_RANGE;
    /* Stock peers stop at the NUL; the trailer must not overlap the string. */
    if (!memchr(field, 0, at)) return YUSB_ERR_RANGE;
    uint8_t *x = field + at;
    x[0] = 'Y';
    x[1] = 'X';
    x[2] = p->version;
    x[3] = p->levers;
    put_be16(x + 4, (p->levers & YUSB_YX_CACHE) ? p->cache_blocks : 0);
    x[6] = x[7] = 0;
    return YUSB_OK;
}

/* The version is reported as given: a peer answers a higher one with
 * version 1 semantics, a client treats an unknown accept as absent. The
 * reserved trailing bytes are ignored for forward compatibility. */
static bool params_get(const uint8_t *field, size_t at, yusb_yx_params_t *p)
{
    memset(p, 0, sizeof(*p));
    const uint8_t *x = field + at;
    if (x[0] != 'Y' || x[1] != 'X' || !x[2] || !memchr(field, 0, at)) return false;
    p->version = x[2];
    p->levers = x[3] & YUSB_YX_LEVERS;
    if (p->levers & YUSB_YX_CACHE) p->cache_blocks = get_be16(x + 4);
    if (!p->cache_blocks) p->levers &= (uint8_t)~YUSB_YX_CACHE;
    return true;
}

int yusb_yx_offer_put(uint8_t busid_field[32], const yusb_yx_params_t *offer)
{
    return params_put(busid_field, OFFER_AT, offer);
}

bool yusb_yx_offer_get(const uint8_t busid_field[32], yusb_yx_params_t *offer)
{
    return params_get(busid_field, OFFER_AT, offer);
}

int yusb_yx_accept_put(uint8_t path_field[256], const yusb_yx_params_t *agreed)
{
    return params_put(path_field, ACCEPT_AT, agreed);
}

bool yusb_yx_accept_get(const uint8_t path_field[256], yusb_yx_params_t *agreed)
{
    return params_get(path_field, ACCEPT_AT, agreed);
}

void yusb_yx_agree(const yusb_yx_params_t *offer, const yusb_yx_params_t *limits,
                   yusb_yx_params_t *agreed)
{
    uint8_t levers = offer->levers & limits->levers & YUSB_YX_LEVERS;
    uint16_t cache = offer->cache_blocks < limits->cache_blocks ? offer->cache_blocks
                                                                : limits->cache_blocks;
    if (cache > YUSB_YX_MAX_CACHE_BLOCKS) cache = YUSB_YX_MAX_CACHE_BLOCKS;
    if (!(levers & YUSB_YX_CACHE) || !cache) {
        levers &= (uint8_t)~YUSB_YX_CACHE;
        cache = 0;
    }
    agreed->version = YUSB_YX_VERSION;  /* written last: agreed may alias */
    agreed->levers = levers;
    agreed->cache_blocks = cache;
}

size_t yusb_yx_bound(size_t length)
{
    size_t blocks = length / YUSB_YX_BLOCK + (length % YUSB_YX_BLOCK != 0);
    if (length > SIZE_MAX - YUSB_YX_BLOCK_HEADER * blocks) return SIZE_MAX;
    return length + YUSB_YX_BLOCK_HEADER * blocks;
}

static int params_check(const yusb_yx_params_t *p)
{
    if (!p || (p->levers & ~YUSB_YX_LEVERS)) return YUSB_ERR_RANGE;
    if ((p->levers & YUSB_YX_CACHE) &&
        (!p->cache_blocks || p->cache_blocks > YUSB_YX_MAX_CACHE_BLOCKS)) return YUSB_ERR_RANGE;
    return YUSB_OK;
}

/* Slots follow inserted % cache_blocks. Once every slot is filled the counter
 * stays in [cache_blocks, 2 * cache_blocks), so it never wraps and the slot
 * sequence is that of an unbounded count on both ends. */
static uint32_t next_inserted(uint32_t inserted, uint32_t blocks)
{
    return ++inserted == 2u * blocks ? blocks : inserted;
}

/* ---- encoder ---- */

/* Index buckets for a cache of `blocks` slots: a power of two >= 2 * blocks. */
static uint32_t index_buckets(uint32_t blocks)
{
    uint32_t buckets = 1;
    while (buckets < 2u * blocks) buckets <<= 1;
    return buckets;
}

size_t yusb_yx_enc_footprint(const yusb_yx_params_t *agreed)
{
    if (params_check(agreed)) return 0;
    size_t bytes = 0;
    if (agreed->levers & YUSB_YX_LZ4) bytes += (size_t)LZ4_sizeofState();
    if (agreed->levers & YUSB_YX_CACHE)
        bytes += (size_t)agreed->cache_blocks * (YUSB_YX_BLOCK + sizeof(uint32_t)) +
                 (size_t)index_buckets(agreed->cache_blocks) * sizeof(uint16_t);
    return bytes;
}

size_t yusb_yx_dec_footprint(const yusb_yx_params_t *agreed)
{
    if (params_check(agreed)) return 0;
    return (agreed->levers & YUSB_YX_CACHE) ? (size_t)agreed->cache_blocks * YUSB_YX_BLOCK : 0;
}

/* Allocations here and in dec_init must match the *_footprint functions. */
int yusb_yx_enc_init(yusb_yx_enc_t *e, const yusb_yx_params_t *agreed)
{
    if (!e) return YUSB_ERR_RANGE;
    memset(e, 0, sizeof(*e));
    int err = params_check(agreed);
    if (err) return err;
    e->levers = agreed->levers;
    if (e->levers & YUSB_YX_LZ4) e->lz4_state = calloc(1, (size_t)LZ4_sizeofState());
    bool ok = !(e->levers & YUSB_YX_LZ4) || e->lz4_state;
    if (ok && (e->levers & YUSB_YX_CACHE)) {
        uint32_t buckets = index_buckets(agreed->cache_blocks);
        e->cache_blocks = agreed->cache_blocks;
        e->cache = calloc(e->cache_blocks, YUSB_YX_BLOCK);
        e->cache_hash = e->cache ? calloc(e->cache_blocks, sizeof(*e->cache_hash)) : NULL;
        e->index = e->cache_hash ? calloc(buckets, sizeof(*e->index)) : NULL;
        e->index_mask = buckets - 1u;
        ok = e->index != NULL;
    }
    if (!ok) { yusb_yx_enc_free(e); return YUSB_ERR_NOMEM; }
    return YUSB_OK;
}

/* Safe on a zeroed struct, after a failed init and twice in a row. */
void yusb_yx_enc_free(yusb_yx_enc_t *e)
{
    if (!e) return;
    free(e->lz4_state);
    free(e->cache);
    free(e->cache_hash);
    free(e->index);
    memset(e, 0, sizeof(*e));
}

/* Smallest power-of-two period u < len (u <= YUSB_YX_MAX_FILL), or 0. A period
 * u implies 2u, so one compare at the largest candidate rejects a block and
 * halving finds the smallest. u == len would save nothing over RAW and would
 * keep LZ4 from short blocks, so it is not used. */
static size_t fill_unit(const uint8_t *b, size_t len)
{
    size_t u = 1;
    while (u * 2u <= YUSB_YX_MAX_FILL && u * 2u < len) u *= 2u;
    if (u >= len || memcmp(b, b + u, len - u)) return 0;
    while (u > 1 && !memcmp(b, b + u / 2u, len - u / 2u)) u /= 2u;
    return u;
}

static int cache_find(const yusb_yx_enc_t *e, const uint8_t *b, uint32_t hash)
{
    uint16_t entry = e->index[hash & e->index_mask];
    if (!entry) return -1;
    uint32_t slot = entry - 1u;
    /* The hash only nominates a slot; the bytes decide. */
    if (e->cache_hash[slot] != hash ||
        memcmp(e->cache + (size_t)slot * YUSB_YX_BLOCK, b, YUSB_YX_BLOCK)) return -1;
    return (int)slot;
}

static void cache_store(yusb_yx_enc_t *e, const uint8_t *b, uint32_t hash)
{
    uint32_t slot = e->inserted % e->cache_blocks;
    if (e->inserted >= e->cache_blocks) {
        uint32_t old = e->cache_hash[slot] & e->index_mask;
        if (e->index[old] == slot + 1u) e->index[old] = 0;
    }
    memcpy(e->cache + (size_t)slot * YUSB_YX_BLOCK, b, YUSB_YX_BLOCK);
    e->cache_hash[slot] = hash;
    e->index[hash & e->index_mask] = (uint16_t)(slot + 1u);
    e->inserted = next_inserted(e->inserted, e->cache_blocks);
}

/* LZ4 into body; true when it saves at least one eighth of the block. */
static bool try_lz4(yusb_yx_enc_t *e, const uint8_t *b, size_t len, uint8_t *body, size_t *wire)
{
    if (!(e->levers & YUSB_YX_LZ4)) return false;
    if (e->lz4_skip) { e->lz4_skip--; return false; }
    e->stats.lz4_tries++;
    int limit = (int)(len - len / 8u) - 1;
    int n = limit > 0 ? LZ4_compress_fast_extState(e->lz4_state, (const char *)b, (char *)body,
                                                   (int)len, limit, 1) : 0;
    if (n > 0) {
        e->lz4_misses = 0;
        *wire = (size_t)n;
        return true;
    }
    e->stats.lz4_bypass++;
    if (++e->lz4_misses >= LZ4_MISS_LIMIT) {
        e->lz4_misses = LZ4_MISS_LIMIT;  /* saturate: a missed probe skips again */
        e->lz4_skip = LZ4_SKIP_BLOCKS;
    }
    return false;
}

/* One block: header and body at out. With copy_raw false a RAW body is not
 * copied (the iov encoder points at the source instead); *raw tells. */
static size_t encode_block(yusb_yx_enc_t *e, const uint8_t *b, size_t len, uint8_t *out,
                           bool copy_raw, bool *raw)
{
    uint8_t *body = out + YUSB_YX_BLOCK_HEADER;
    bool cached = len == YUSB_YX_BLOCK && (e->levers & YUSB_YX_CACHE);
    size_t unit = (e->levers & YUSB_YX_FILL) ? fill_unit(b, len) : 0;
    unsigned kind = YUSB_YX_KIND_RAW;
    size_t wire = len;
    uint32_t hash = 0;
    int slot = -1;
    if (cached && !unit) {
        hash = XXH32(b, len, 0);
        slot = cache_find(e, b, hash);
    }
    if (unit) {
        kind = YUSB_YX_KIND_FILL;
        wire = unit;
        memcpy(body, b, unit);
    } else if (slot >= 0) {
        kind = YUSB_YX_KIND_REF;
        wire = 8;
        put_be32(body, (uint32_t)slot);
        put_be32(body + 4, hash);
    } else {
        if (try_lz4(e, b, len, body, &wire)) kind = YUSB_YX_KIND_LZ4;
        else if (copy_raw) memcpy(body, b, len);
        if (cached) cache_store(e, b, hash);
    }
    out[0] = (uint8_t)kind;
    out[1] = 0;
    put_be16(out + 2, (uint16_t)wire);
    e->stats.blocks[kind]++;
    e->stats.raw_bytes += len;
    e->stats.wire_bytes += YUSB_YX_BLOCK_HEADER + wire;
    *raw = kind == YUSB_YX_KIND_RAW;
    return YUSB_YX_BLOCK_HEADER + wire;
}

size_t yusb_yx_encode(yusb_yx_enc_t *e, const uint8_t *src, size_t length, uint8_t *out)
{
    size_t wire = 0;
    bool raw;
    for (size_t at = 0; at < length; at += YUSB_YX_BLOCK) {
        size_t len = length - at < YUSB_YX_BLOCK ? length - at : YUSB_YX_BLOCK;
        wire += encode_block(e, src + at, len, out + wire, true, &raw);
    }
    return wire;
}

/* Same blocks and encoder history as yusb_yx_encode. Out pieces are packed
 * (RAW bodies take no room in out), so consecutive ones share a segment. */
size_t yusb_yx_encode_iov(yusb_yx_enc_t *e, const uint8_t *src, size_t length, uint8_t *out,
                          yusb_iov_t *iov, size_t max_iov, size_t *wire)
{
    size_t n = 0, used = 0, total = 0;
    bool last_out = false;    /* iov[n - 1] is an out piece ending at out + used */
    if (wire) *wire = 0;
    if (!length || max_iov < yusb_yx_segments(length)) return 0;
    for (size_t at = 0; at < length; at += YUSB_YX_BLOCK) {
        size_t len = length - at < YUSB_YX_BLOCK ? length - at : YUSB_YX_BLOCK;
        bool raw;
        size_t w = encode_block(e, src + at, len, out + used, false, &raw);
        size_t piece = raw ? YUSB_YX_BLOCK_HEADER : w;
        if (last_out) iov[n - 1].len += piece;
        else iov[n++] = (yusb_iov_t){out + used, piece};
        used += piece;
        last_out = !raw;
        if (raw) iov[n++] = (yusb_iov_t){src + at, len};
        total += w;
    }
    if (wire) *wire = total;
    return n;
}

/* ---- decoder ---- */

/* A violation closes the connection, and the receiver cache may already be
 * out of step, so a failed decoder stays failed until yusb_yx_dec_free. */
static int fail(yusb_yx_dec_t *d, int err)
{
    d->levers |= DEC_FAILED;
    return err;
}

int yusb_yx_dec_init(yusb_yx_dec_t *d, const yusb_yx_params_t *agreed)
{
    if (!d) return YUSB_ERR_RANGE;
    memset(d, 0, sizeof(*d));
    int err = params_check(agreed);
    if (err) return fail(d, err);
    d->levers = agreed->levers;
    if (d->levers & YUSB_YX_CACHE) {
        d->cache_blocks = agreed->cache_blocks;
        d->cache = calloc(d->cache_blocks, YUSB_YX_BLOCK);
        if (!d->cache) { yusb_yx_dec_free(d); return fail(d, YUSB_ERR_NOMEM); }
    }
    return YUSB_OK;
}

/* Safe on a zeroed struct, after a failed init and twice in a row. */
void yusb_yx_dec_free(yusb_yx_dec_t *d)
{
    if (!d) return;
    free(d->cache);
    memset(d, 0, sizeof(*d));
}

void yusb_yx_dec_begin(yusb_yx_dec_t *d, uint8_t *dst, size_t length)
{
    d->dst = dst;
    d->length = length;
    d->done = 0;
    d->have = 0;
    d->kind = 0;
    d->wire = d->got = 0;
}

static size_t block_len(const yusb_yx_dec_t *d)
{
    size_t left = d->length - d->done;
    return left < YUSB_YX_BLOCK ? left : YUSB_YX_BLOCK;
}

/* A failed decoder offers no space (and *buf = NULL); dec_feed reports it. */
size_t yusb_yx_dec_space(yusb_yx_dec_t *d, uint8_t **buf)
{
    *buf = NULL;
    if ((d->levers & DEC_FAILED) || d->done >= d->length) return 0;
    if (d->have < YUSB_YX_BLOCK_HEADER) {
        *buf = d->header + d->have;
        return YUSB_YX_BLOCK_HEADER - d->have;
    }
    /* RAW lands in its final place; other bodies need a transform. */
    *buf = (d->kind == YUSB_YX_KIND_RAW ? d->dst + d->done : d->staging) + d->got;
    return (size_t)(d->wire - d->got);
}

/* Header complete: check it against ADR 0028 section 4. */
static int start_body(yusb_yx_dec_t *d)
{
    size_t len = block_len(d);
    unsigned kind = d->header[0];
    unsigned wire = get_be16(d->header + 2);
    bool ok = !d->header[1];
    switch (kind) {
    case YUSB_YX_KIND_RAW: ok = ok && wire == len; break;
    case YUSB_YX_KIND_FILL:
        ok = ok && (d->levers & YUSB_YX_FILL) && wire >= 1 && wire <= YUSB_YX_MAX_FILL &&
             wire <= len;
        break;
    case YUSB_YX_KIND_LZ4: ok = ok && (d->levers & YUSB_YX_LZ4) && wire >= 1 && wire < len; break;
    case YUSB_YX_KIND_REF:
        ok = ok && (d->levers & YUSB_YX_CACHE) && wire == 8 && len == YUSB_YX_BLOCK;
        break;
    default: ok = false; break;
    }
    if (!ok) return fail(d, YUSB_ERR_PROTOCOL);
    d->kind = (uint8_t)kind;
    d->wire = (uint16_t)wire;
    d->got = 0;
    return YUSB_OK;
}

static void tile(uint8_t *out, size_t len, const uint8_t *unit, size_t u)
{
    memcpy(out, unit, u);
    for (size_t n = u; n < len; n *= 2u) memcpy(out + n, out, n < len - n ? n : len - n);
}

/* Body complete: produce the block, then mirror the sender's cache rule. */
static int finish_block(yusb_yx_dec_t *d)
{
    size_t len = block_len(d);
    uint8_t *out = d->dst + d->done;
    if (d->kind == YUSB_YX_KIND_FILL) {
        tile(out, len, d->staging, d->wire);
    } else if (d->kind == YUSB_YX_KIND_LZ4) {
        int n = LZ4_decompress_safe((const char *)d->staging, (char *)out, d->wire, (int)len);
        if (n != (int)len) return fail(d, YUSB_ERR_PROTOCOL);
    } else if (d->kind == YUSB_YX_KIND_REF) {
        uint32_t slot = get_be32(d->staging);
        if (slot >= d->cache_blocks || slot >= d->inserted) return fail(d, YUSB_ERR_PROTOCOL);
        const uint8_t *cached = d->cache + (size_t)slot * YUSB_YX_BLOCK;
        if (XXH32(cached, YUSB_YX_BLOCK, 0) != get_be32(d->staging + 4))
            return fail(d, YUSB_ERR_PROTOCOL);
        memcpy(out, cached, YUSB_YX_BLOCK);
    }
    if ((d->kind == YUSB_YX_KIND_RAW || d->kind == YUSB_YX_KIND_LZ4) && len == YUSB_YX_BLOCK &&
        (d->levers & YUSB_YX_CACHE)) {
        memcpy(d->cache + (size_t)(d->inserted % d->cache_blocks) * YUSB_YX_BLOCK, out,
               YUSB_YX_BLOCK);
        d->inserted = next_inserted(d->inserted, d->cache_blocks);
    }
    d->stats.blocks[d->kind]++;
    d->stats.raw_bytes += len;
    d->stats.wire_bytes += YUSB_YX_BLOCK_HEADER + d->wire;
    d->done += len;
    d->have = 0;
    d->wire = d->got = 0;
    return YUSB_OK;
}

/* n larger than the space last offered is a caller bug (YUSB_ERR_RANGE). */
int yusb_yx_dec_feed(yusb_yx_dec_t *d, size_t n)
{
    uint8_t *buf;
    size_t want = yusb_yx_dec_space(d, &buf);
    if (d->levers & DEC_FAILED) return YUSB_ERR_PROTOCOL;
    if (!n) return YUSB_OK;
    if (n > want) return fail(d, YUSB_ERR_RANGE);
    if (d->have < YUSB_YX_BLOCK_HEADER) {
        d->have = (uint8_t)(d->have + n);
        return d->have < YUSB_YX_BLOCK_HEADER ? YUSB_OK : start_body(d);
    }
    d->got = (uint16_t)(d->got + n);
    return d->got < d->wire ? YUSB_OK : finish_block(d);
}

/* Wire bytes that end before `length` decoded bytes are a protocol error. */
int yusb_yx_decode(yusb_yx_dec_t *d, const uint8_t *wire, size_t wire_len, uint8_t *dst,
                   size_t length, size_t *consumed)
{
    size_t used = 0, want;
    uint8_t *buf;
    yusb_yx_dec_begin(d, dst, length);
    int err = (d->levers & DEC_FAILED) ? YUSB_ERR_PROTOCOL : YUSB_OK;
    while (!err && (want = yusb_yx_dec_space(d, &buf))) {
        if (used == wire_len) { err = fail(d, YUSB_ERR_PROTOCOL); break; }
        size_t n = want < wire_len - used ? want : wire_len - used;
        memcpy(buf, wire + used, n);
        used += n;
        err = yusb_yx_dec_feed(d, n);
    }
    if (consumed) *consumed = used;
    return err;
}
