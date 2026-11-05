/* libFuzzer target for the YX payload codec (ADR 0028).
 *
 * Input: levers, slots - 1, decoded length - 1 (u16 BE), split seed, then
 * the body bytes.
 *  (a) The body is decoded as consecutive wire streams of `length` decoded
 *      bytes, once with yusb_yx_decode and once incrementally with random
 *      feed sizes. Both must agree on the outcome, the bytes and the cache.
 *  (b) Messages derived from the body (as is, tiled, sparse, repeated,
 *      noise) are encoded and decoded by a matching pair; the decoded bytes
 *      must equal the source and both caches must stay identical. A second
 *      encoder fed the same messages through yusb_yx_encode_iov must give
 *      the same bytes (RAW bodies pointing into the source, at most
 *      yusb_yx_segments segments, adjacent out pieces merged).
 *  (c) The body's first bytes, as busid and path fields, go through the
 *      negotiation helpers, whose results must keep their invariants.
 * Every object is created per input, so runs are independent.
 *
 * clang -g -O1 -fsanitize=fuzzer,address,undefined -I native/yusb/include
 *   -I native/third_party/lz4 native/yusb/fuzz/fuzz_yx.c native/yusb/src/yx.c
 *   native/third_party/lz4/lz4.c -o fuzz_yx
 */
#include <stdlib.h>
#include <string.h>
#include "yusb_yx.h"

#define MAX_LENGTH (4u * YUSB_YX_BLOCK)
#define MAX_STREAMS 16
#define MAX_MESSAGES 6

static uint32_t rng;

static uint32_t next(void)
{
    rng ^= rng << 13;
    rng ^= rng >> 17;
    rng ^= rng << 5;
    return rng;
}

static void check(bool ok)
{
    if (!ok) abort();
}

static bool same_cache(const uint8_t *a, uint32_t ia, const yusb_yx_dec_t *d, uint16_t slots)
{
    return ia == d->inserted && (!slots || !memcmp(a, d->cache, (size_t)slots * YUSB_YX_BLOCK));
}

static bool same_stats(const yusb_yx_stats_t *a, const yusb_yx_stats_t *b)
{
    return !memcmp(a->blocks, b->blocks, sizeof(a->blocks)) && a->raw_bytes == b->raw_bytes &&
           a->wire_bytes == b->wire_bytes;
}

/* Feed wire[0, wire_len) in random-sized pieces; *used gets bytes fed. */
static int feed(yusb_yx_dec_t *d, const uint8_t *wire, size_t wire_len, uint8_t *dst,
                size_t length, size_t *used)
{
    size_t at = 0, want;
    uint8_t *buf;
    int err = YUSB_OK;
    yusb_yx_dec_begin(d, dst, length);
    while ((want = yusb_yx_dec_space(d, &buf))) {
        check(buf != NULL);
        if (at == wire_len) { err = YUSB_ERR_PROTOCOL; break; }  /* truncated */
        size_t n = 1u + next() % (next() & 1u ? want : 8u);
        if (n > want) n = want;
        if (n > wire_len - at) n = wire_len - at;
        memcpy(buf, wire + at, n);
        at += n;
        if ((err = yusb_yx_dec_feed(d, n))) break;
    }
    *used = at;
    return err;
}

static void decode_arbitrary(const yusb_yx_params_t *p, const uint8_t *in, size_t n,
                             size_t length)
{
    yusb_yx_dec_t *a = malloc(sizeof(*a)), *b = malloc(sizeof(*b));
    uint8_t *da = malloc(length), *db = malloc(length);
    check(a && b && da && db);
    check(yusb_yx_dec_init(a, p) == YUSB_OK && yusb_yx_dec_init(b, p) == YUSB_OK);
    size_t at = 0;
    for (int s = 0; s < MAX_STREAMS && at < n; ++s) {
        size_t used_a = 0, used_b = 0;
        int ea = yusb_yx_decode(a, in + at, n - at, da, length, &used_a);
        int eb = feed(b, in + at, n - at, db, length, &used_b);
        check(ea == eb && (ea == YUSB_OK || ea == YUSB_ERR_PROTOCOL));
        if (ea) break;
        check(used_a == used_b && used_a <= yusb_yx_bound(length) && !memcmp(da, db, length));
        check(same_cache(a->cache, a->inserted, b, p->cache_blocks));
        check(same_stats(&a->stats, &b->stats));
        at += used_a;
    }
    yusb_yx_dec_free(a);
    yusb_yx_dec_free(b);
    free(a);
    free(b);
    free(da);
    free(db);
}

/* One message derived from the input; returns its length (> 0). */
static size_t message(uint8_t *src, const uint8_t *in, size_t n, size_t last)
{
    unsigned shape = next() % 5u;
    if (shape == 3 && last) return last;                              /* repeat: REF */
    if (shape == 4) {                                                 /* noise: LZ4 bypass */
        size_t len = MAX_LENGTH - next() % 64u;
        for (size_t i = 0; i < len; ++i) src[i] = (uint8_t)(next() >> 7);
        return len;
    }
    if (shape == 0 && n) {
        size_t len = n < MAX_LENGTH ? n : MAX_LENGTH;
        memcpy(src, in, len);
        return len;
    }
    size_t len = 1u + next() % MAX_LENGTH;
    if (shape == 1 && n) {                                            /* tiled: FILL, LZ4 */
        size_t period = 1u + next() % n, off = next() % n;
        for (size_t i = 0; i < len; ++i) src[i] = in[(off + i % period) % n];
        return len;
    }
    memset(src, 0, len);                                              /* sparse */
    for (size_t i = 0; i < n && i < 64; ++i) src[next() % len] = in[i];
    return len;
}

/* The iov encoder's segments, concatenated, equal `wire`. */
static void check_iov(yusb_yx_enc_t *e, const uint8_t *src, size_t len, const uint8_t *wire, size_t w)
{
    size_t bound = yusb_yx_bound(len), segs = yusb_yx_segments(len), got = 0, at = 0;
    uint8_t *out = malloc(bound), *cat = malloc(bound);
    yusb_iov_t *iov = malloc(segs * sizeof(*iov));
    check(out && cat && iov);
    size_t n = yusb_yx_encode_iov(e, src, len, out, iov, segs, &got);
    check(n >= 1 && n <= segs && got == w);
    bool prev_out = false;
    for (size_t i = 0; i < n; ++i) {
        bool in_out = iov[i].base >= out && iov[i].base + iov[i].len <= out + bound;
        check(in_out || (iov[i].base >= src && iov[i].base + iov[i].len <= src + len));
        check(!(in_out && prev_out) && iov[i].len);
        prev_out = in_out;
        memcpy(cat + at, iov[i].base, iov[i].len);
        at += iov[i].len;
    }
    check(at == w && !memcmp(cat, wire, w));
    free(out);
    free(cat);
    free(iov);
}

static void roundtrip(const yusb_yx_params_t *p, const uint8_t *in, size_t n)
{
    yusb_yx_enc_t enc, twin;
    yusb_yx_dec_t *dec = malloc(sizeof(*dec));
    uint8_t *src = malloc(MAX_LENGTH);
    check(dec && src);
    check(yusb_yx_enc_init(&enc, p) == YUSB_OK && yusb_yx_dec_init(dec, p) == YUSB_OK);
    check(yusb_yx_enc_init(&twin, p) == YUSB_OK);
    size_t len = 0;
    for (unsigned m = 0, count = 1u + next() % MAX_MESSAGES; m < count; ++m) {
        len = message(src, in, n, len);
        size_t bound = yusb_yx_bound(len), used = 0;
        uint8_t *wire = malloc(bound), *dst = malloc(len);
        check(wire && dst);
        size_t w = yusb_yx_encode(&enc, src, len, wire);
        check(w > 0 && w <= bound);
        check_iov(&twin, src, len, wire, w);
        check(same_stats(&enc.stats, &twin.stats) && enc.inserted == twin.inserted);
        check(feed(dec, wire, w, dst, len, &used) == YUSB_OK && used == w);
        check(!memcmp(dst, src, len));
        check(same_cache(enc.cache, enc.inserted, dec, p->cache_blocks));
        check(same_stats(&enc.stats, &dec->stats));
        free(wire);
        free(dst);
    }
    yusb_yx_enc_free(&enc);
    yusb_yx_enc_free(&twin);
    yusb_yx_enc_free(&twin);   /* free is idempotent */
    yusb_yx_dec_free(dec);
    free(dec);
    free(src);
}

static bool params_ok(const yusb_yx_params_t *p)
{
    return !(p->levers & ~YUSB_YX_LEVERS) && p->version &&
           (p->levers & YUSB_YX_CACHE ? p->cache_blocks != 0 : p->cache_blocks == 0);
}

static void negotiate(const uint8_t *in, size_t n, uint8_t limit_levers, uint16_t limit_slots)
{
    uint8_t busid[32] = {0}, path[256] = {0};
    memcpy(busid, in, n < sizeof(busid) ? n : sizeof(busid));
    memcpy(path, in, n < sizeof(path) ? n : sizeof(path));
    yusb_yx_params_t offer, limits = {YUSB_YX_VERSION, limit_levers, limit_slots}, agreed, got;
    if (yusb_yx_offer_get(busid, &offer)) {
        check(params_ok(&offer));
        yusb_yx_agree(&offer, &limits, &agreed);
        check(agreed.version == YUSB_YX_VERSION && params_ok(&agreed));
        check(!(agreed.levers & ~(offer.levers & limits.levers)));
        check(agreed.cache_blocks <= offer.cache_blocks && agreed.cache_blocks <= limit_slots);
        check(agreed.cache_blocks <= YUSB_YX_MAX_CACHE_BLOCKS);
        if (yusb_yx_accept_put(path, &agreed) == YUSB_OK) {
            check(yusb_yx_accept_get(path, &got) && !memcmp(&got, &agreed, sizeof(got)));
        } else {
            check(!memchr(path, 0, 248));
        }
    } else {
        check(!offer.version && !offer.levers && !offer.cache_blocks);
    }
    if (yusb_yx_accept_get(path, &got)) check(params_ok(&got));
    /* An offer written is read back, normalized: no CACHE without slots. */
    yusb_yx_params_t mine = {YUSB_YX_VERSION, limit_levers & YUSB_YX_LEVERS, limit_slots};
    uint16_t slots = (mine.levers & YUSB_YX_CACHE) ? mine.cache_blocks : 0;
    uint8_t levers = slots ? mine.levers : (uint8_t)(mine.levers & ~YUSB_YX_CACHE);
    if (yusb_yx_offer_put(busid, &mine) == YUSB_OK) {
        check(yusb_yx_offer_get(busid, &got) && got.version == YUSB_YX_VERSION);
        check(got.levers == levers && got.cache_blocks == slots);
    } else {
        check(!memchr(busid, 0, 24));
    }
}

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size);

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)
{
    if (size < 5) return 0;
    yusb_yx_params_t p = {YUSB_YX_VERSION, data[0] & (YUSB_YX_FILL | YUSB_YX_LZ4 | YUSB_YX_CACHE),
                          0};
    if (p.levers & YUSB_YX_CACHE) p.cache_blocks = (uint16_t)(1u + data[1] % 64u);
    size_t length = 1u + ((size_t)data[2] << 8 | data[3]) % MAX_LENGTH;
    rng = 0x9e3779b9u ^ data[4] ^ (uint32_t)size << 8;
    decode_arbitrary(&p, data + 5, size - 5, length);
    roundtrip(&p, data + 5, size - 5);
    negotiate(data + 5, size - 5, data[0], (uint16_t)(data[1] << 8 | data[4]));
    return 0;
}
