/* YX codec unit tests (ADR 0028). Usage: yx_test <case> | list.
 * Every buffer handed to the codec is allocated at its exact size, so the
 * sanitizers catch any byte read or written out of bounds. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "lz4.h"
#define XXH_INLINE_ALL
#include "xxhash.h"
#include "yusb_yx.h"

#define BLOCK YUSB_YX_BLOCK
#define ALL (YUSB_YX_FILL | YUSB_YX_LZ4 | YUSB_YX_CACHE)
#define CHECK(cond)                                                                    \
    do {                                                                               \
        if (!(cond)) {                                                                 \
            fprintf(stderr, "%s:%d: CHECK failed: %s\n", __FILE__, __LINE__, #cond);    \
            exit(1);                                                                   \
        }                                                                              \
    } while (0)

/* ---- helpers ---- */

static uint64_t rng_state = 0x9e3779b97f4a7c15ull;

static uint32_t rnd(void)
{
    uint64_t z = (rng_state += 0x9e3779b97f4a7c15ull);
    z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ull;
    z = (z ^ (z >> 27)) * 0x94d049bb133111ebull;
    return (uint32_t)((z ^ (z >> 31)) >> 32);
}

static uint8_t *xmalloc(size_t n)
{
    uint8_t *p = malloc(n ? n : 1);
    CHECK(p);
    return p;
}

static void fill_random(uint8_t *p, size_t n)
{
    for (size_t i = 0; i < n; ++i) p[i] = (uint8_t)rnd();
}

/* Exactly `period`: the unit's last byte is the only even one. */
static void fill_period(uint8_t *p, size_t n, size_t period)
{
    for (size_t i = 0; i < n && i < period; ++i)
        p[i] = i + 1 == period ? 0 : (uint8_t)(rnd() | 1u);
    for (size_t i = period; i < n; ++i) p[i] = p[i - period];
}

static void fill_text(uint8_t *p, size_t n)
{
    static const char *words[] = {"the ", "quick ", "brown ", "fox ", "jumps ", "over ",
                                  "lazy ", "dog ", "and ", "a ", "USB ", "bulk ",
                                  "transfer ", "block ", "of ", "data\n"};
    size_t at = 0;
    while (at < n) {
        const char *w = words[rnd() % 16u];
        for (size_t i = 0; w[i] && at < n; ++i) p[at++] = (uint8_t)w[i];
    }
}

enum { ZEROS, ONES, PERIOD2, PERIOD4, PERIOD512, TEXT, RANDOM, MIXED, CLASSES };

static void fill_class(uint8_t *p, size_t n, int cls)
{
    switch (cls) {
    case ZEROS: memset(p, 0, n); break;
    case ONES: memset(p, 0xff, n); break;
    case PERIOD2: fill_period(p, n, 2); break;
    case PERIOD4: fill_period(p, n, 4); break;
    case PERIOD512: fill_period(p, n, 512); break;
    case TEXT: fill_text(p, n); break;
    case RANDOM: fill_random(p, n); break;
    default:
        for (size_t at = 0; at < n; at += BLOCK) {
            size_t len = n - at < BLOCK ? n - at : BLOCK;
            fill_class(p + at, len, (int)(rnd() % (RANDOM + 1u)));
        }
        break;
    }
}

static yusb_yx_params_t params(uint8_t levers, uint16_t cache_blocks)
{
    yusb_yx_params_t p = {YUSB_YX_VERSION, levers, cache_blocks};
    return p;
}

typedef struct {
    yusb_yx_enc_t enc;
    yusb_yx_dec_t dec;
} pair_t;

static pair_t *pair_new(uint8_t levers, uint16_t cache_blocks)
{
    pair_t *p = calloc(1, sizeof(*p));
    CHECK(p);
    yusb_yx_params_t a = params(levers, cache_blocks);
    CHECK(yusb_yx_enc_init(&p->enc, &a) == YUSB_OK);
    CHECK(yusb_yx_dec_init(&p->dec, &a) == YUSB_OK);
    return p;
}

static void pair_free(pair_t *p)
{
    yusb_yx_enc_free(&p->enc);
    yusb_yx_dec_free(&p->dec);
    free(p);
}

/* Feed `wire` in chunks: chunk 0 = random sizes, else that fixed size. */
static int feed(yusb_yx_dec_t *d, const uint8_t *wire, size_t wire_len, uint8_t *dst,
                size_t length, size_t chunk, size_t *used)
{
    size_t at = 0, want;
    uint8_t *buf;
    int err = YUSB_OK;
    yusb_yx_dec_begin(d, dst, length);
    while (!err && (want = yusb_yx_dec_space(d, &buf))) {
        CHECK(buf);
        if (at == wire_len) { err = -100; break; }
        size_t n = chunk ? chunk : 1u + rnd() % want;
        if (n > want) n = want;
        if (n > wire_len - at) n = wire_len - at;
        memcpy(buf, wire + at, n);
        at += n;
        err = yusb_yx_dec_feed(d, n);
    }
    if (used) *used = at;
    return err;
}

/* Encode, check the bound, decode (whole, random split or 1-byte), compare. */
static size_t roundtrip(pair_t *p, const uint8_t *src, size_t length, int mode)
{
    size_t bound = yusb_yx_bound(length);
    uint8_t *wire = xmalloc(bound), *dst = xmalloc(length);
    size_t n = yusb_yx_encode(&p->enc, src, length, wire), used = 0;
    CHECK(n > 0 && n <= bound);
    int err;
    if (mode == 0) err = yusb_yx_decode(&p->dec, wire, n, dst, length, &used);
    else err = feed(&p->dec, wire, n, dst, length, mode == 1 ? 0 : 1, &used);
    CHECK(err == YUSB_OK);
    CHECK(used == n);
    CHECK(!memcmp(dst, src, length));
    free(wire);
    free(dst);
    return n;
}

static void check_sync(const pair_t *p)
{
    CHECK(p->enc.inserted == p->dec.inserted);
    CHECK(p->enc.cache_blocks == p->dec.cache_blocks);
    if (p->enc.cache_blocks)
        CHECK(!memcmp(p->enc.cache, p->dec.cache, (size_t)p->enc.cache_blocks * BLOCK));
}

/* Hand-built wire streams. */
typedef struct {
    uint8_t b[3 * BLOCK + 64];
    size_t n;
} stream_t;

static void put_block(stream_t *s, unsigned kind, unsigned aux, unsigned wire, const void *body,
                      size_t body_len)
{
    CHECK(s->n + 4 + body_len <= sizeof(s->b));
    s->b[s->n++] = (uint8_t)kind;
    s->b[s->n++] = (uint8_t)aux;
    s->b[s->n++] = (uint8_t)(wire >> 8);
    s->b[s->n++] = (uint8_t)wire;
    if (body_len) memcpy(s->b + s->n, body, body_len);
    s->n += body_len;
}

static void put_ref(stream_t *s, uint32_t slot, uint32_t check)
{
    uint8_t body[8] = {(uint8_t)(slot >> 24), (uint8_t)(slot >> 16), (uint8_t)(slot >> 8),
                       (uint8_t)slot, (uint8_t)(check >> 24), (uint8_t)(check >> 16),
                       (uint8_t)(check >> 8), (uint8_t)check};
    put_block(s, YUSB_YX_KIND_REF, 0, 8, body, 8);
}

/* A stream must be rejected under whole and 1-byte feeding, and stay failed. */
static void expect_reject(uint8_t levers, uint16_t cache_blocks, const stream_t *prefix,
                          size_t prefix_len, const stream_t *s, size_t length)
{
    for (int mode = 0; mode < 2; ++mode) {
        yusb_yx_params_t a = params(levers, cache_blocks);
        yusb_yx_dec_t *d = calloc(1, sizeof(*d));
        uint8_t *dst = xmalloc(length), *buf;
        CHECK(d && yusb_yx_dec_init(d, &a) == YUSB_OK);
        if (prefix) {
            uint8_t *pre = xmalloc(prefix_len);
            CHECK(yusb_yx_decode(d, prefix->b, prefix->n, pre, prefix_len, NULL) == YUSB_OK);
            free(pre);
        }
        int err = mode ? feed(d, s->b, s->n, dst, length, 1, NULL)
                       : yusb_yx_decode(d, s->b, s->n, dst, length, NULL);
        CHECK(err == YUSB_ERR_PROTOCOL);
        /* Sticky: no more space, no more bytes accepted, not even after begin. */
        CHECK(yusb_yx_dec_space(d, &buf) == 0 && buf == NULL);
        CHECK(yusb_yx_dec_feed(d, 1) == YUSB_ERR_PROTOCOL);
        yusb_yx_dec_begin(d, dst, length);
        CHECK(yusb_yx_dec_space(d, &buf) == 0);
        uint8_t raw[4 + 600] = {0, 0, 0x02, 0x58};
        CHECK(yusb_yx_decode(d, raw, sizeof(raw), dst, 600 < length ? 600 : length, NULL) ==
              YUSB_ERR_PROTOCOL);
        yusb_yx_dec_free(d);
        free(d);
        free(dst);
    }
}

/* ---- cases ---- */

static void test_offer(void)
{
    uint8_t f[32];
    yusb_yx_params_t o = params(YUSB_YX_FILL | YUSB_YX_CACHE, 0x1234), g;
    memset(f, 0xaa, sizeof(f));
    memcpy(f, "1-1.2", 6);
    CHECK(yusb_yx_offer_put(f, &o) == YUSB_OK);
    static const uint8_t want[8] = {'Y', 'X', 1, 0x05, 0x12, 0x34, 0, 0};
    CHECK(!memcmp(f + 24, want, 8));
    CHECK(!memcmp(f, "1-1.2", 6) && f[6] == 0xaa && f[23] == 0xaa);  /* bytes before 24 kept */
    CHECK(yusb_yx_offer_get(f, &g));
    CHECK(g.version == 1 && g.levers == 0x05 && g.cache_blocks == 0x1234);
    /* The string may use bytes 0..22; its NUL at 23 is the last allowed. */
    memset(f, 'b', 23);
    f[23] = 0;
    CHECK(yusb_yx_offer_put(f, &o) == YUSB_OK && yusb_yx_offer_get(f, &g));
    f[23] = 'b';
    CHECK(yusb_yx_offer_put(f, &o) == YUSB_ERR_RANGE);
    CHECK(!yusb_yx_offer_get(f, &g) && g.levers == 0 && g.version == 0);
    /* Bad parameters. */
    memset(f, 0, sizeof(f));
    yusb_yx_params_t bad = params(0x10, 0);
    CHECK(yusb_yx_offer_put(f, &bad) == YUSB_ERR_RANGE);
    bad = params(YUSB_YX_FILL, 0);
    bad.version = 0;
    CHECK(yusb_yx_offer_put(f, &bad) == YUSB_ERR_RANGE);
    CHECK(yusb_yx_offer_put(f, NULL) == YUSB_ERR_RANGE);
    /* cache_blocks is written as 0 without CACHE. */
    o = params(YUSB_YX_LZ4, 77);
    CHECK(yusb_yx_offer_put(f, &o) == YUSB_OK && f[28] == 0 && f[29] == 0);
    /* Get: magic, version >= 1, levers masked, higher versions reported. */
    memset(f, 0, sizeof(f));
    CHECK(!yusb_yx_offer_get(f, &g));                                 /* stock: zeros */
    static const uint8_t v2[8] = {'Y', 'X', 2, 0xff, 0x00, 0x10, 0x55, 0x66};
    memcpy(f + 24, v2, 8);
    CHECK(yusb_yx_offer_get(f, &g) && g.version == 2 && g.levers == YUSB_YX_LEVERS &&
          g.cache_blocks == 16);
    f[26] = 0;
    CHECK(!yusb_yx_offer_get(f, &g));
    f[26] = 1;
    f[24] = 'y';
    CHECK(!yusb_yx_offer_get(f, &g));
    f[24] = 'Y';
    f[25] = 'Y';
    CHECK(!yusb_yx_offer_get(f, &g));
    f[25] = 'X';
    f[28] = f[29] = 0;                                                /* CACHE with 0 slots */
    CHECK(yusb_yx_offer_get(f, &g) && g.levers == (YUSB_YX_LEVERS & ~YUSB_YX_CACHE) &&
          g.cache_blocks == 0);
    f[27] = YUSB_YX_FILL;
    f[28] = 0x01;
    CHECK(yusb_yx_offer_get(f, &g) && g.levers == YUSB_YX_FILL && g.cache_blocks == 0);
}

static void test_accept(void)
{
    uint8_t f[256];
    yusb_yx_params_t a = params(ALL | YUSB_YX_READAHEAD, 4096), g;
    memset(f, 0, sizeof(f));
    strcpy((char *)f, "/sys/devices/pci0000:00/0000:00:14.0/usb1/1-1");
    CHECK(yusb_yx_accept_put(f, &a) == YUSB_OK);
    static const uint8_t want[8] = {'Y', 'X', 1, 0x0f, 0x10, 0x00, 0, 0};
    CHECK(!memcmp(f + 248, want, 8));
    CHECK(yusb_yx_accept_get(f, &g) && g.version == 1 && g.levers == 0x0f &&
          g.cache_blocks == 4096);
    memset(f, 'p', 247);
    f[247] = 0;
    CHECK(yusb_yx_accept_put(f, &a) == YUSB_OK && yusb_yx_accept_get(f, &g));
    f[247] = 'p';
    CHECK(yusb_yx_accept_put(f, &a) == YUSB_ERR_RANGE && !yusb_yx_accept_get(f, &g));
    memset(f, 0, sizeof(f));
    CHECK(!yusb_yx_accept_get(f, &g));                                /* stock exporter */
    /* The offer slot of a busid field is not an accept slot. */
    uint8_t busid[32] = "1-1";
    CHECK(yusb_yx_offer_put(busid, &a) == YUSB_OK);
    memcpy(f, busid, sizeof(busid));
    CHECK(!yusb_yx_accept_get(f, &g));
}

static void test_agree(void)
{
    yusb_yx_params_t o, l, g;
    o = params(YUSB_YX_FILL | YUSB_YX_LZ4 | YUSB_YX_CACHE, 64);
    l = params(YUSB_YX_LZ4 | YUSB_YX_CACHE | YUSB_YX_READAHEAD, 32);
    yusb_yx_agree(&o, &l, &g);
    CHECK(g.version == 1 && g.levers == (YUSB_YX_LZ4 | YUSB_YX_CACHE) && g.cache_blocks == 32);
    l.cache_blocks = 0;                                               /* min 0: CACHE dropped */
    yusb_yx_agree(&o, &l, &g);
    CHECK(g.levers == YUSB_YX_LZ4 && g.cache_blocks == 0);
    o = params(YUSB_YX_FILL, 64);                                     /* no CACHE: no slots */
    l = params(ALL, 64);
    yusb_yx_agree(&o, &l, &g);
    CHECK(g.levers == YUSB_YX_FILL && g.cache_blocks == 0);
    o = params(ALL, 65535);                                           /* clamp */
    l = params(ALL, 65000);
    yusb_yx_agree(&o, &l, &g);
    CHECK(g.levers == ALL && g.cache_blocks == YUSB_YX_MAX_CACHE_BLOCKS);
    o = params(0xff, 8);                                              /* bits outside LEVERS */
    o.version = 7;                                                    /* answered as version 1 */
    l = params(0xff, 9);
    yusb_yx_agree(&o, &l, &g);
    CHECK(g.version == 1 && g.levers == YUSB_YX_LEVERS && g.cache_blocks == 8);
    o = params(ALL, 10);                                              /* agreed may alias */
    l = params(ALL, 5);
    yusb_yx_agree(&o, &l, &o);
    CHECK(o.levers == ALL && o.cache_blocks == 5);
    o = params(0, 0);
    yusb_yx_agree(&o, &l, &g);
    CHECK(g.levers == 0 && g.cache_blocks == 0);
}

static void test_bound(void)
{
    CHECK(yusb_yx_bound(0) == 0);
    CHECK(yusb_yx_bound(1) == 5);
    CHECK(yusb_yx_bound(4095) == 4099);
    CHECK(yusb_yx_bound(4096) == 4100);
    CHECK(yusb_yx_bound(4097) == 4105);
    CHECK(yusb_yx_bound(65536) == 65536 + 64);
    CHECK(yusb_yx_bound(SIZE_MAX) == SIZE_MAX);
    CHECK(yusb_yx_eligible(YUSB_XFER_BULK, 512) && !yusb_yx_eligible(YUSB_XFER_BULK, 511));
}

static void test_init(void)
{
    yusb_yx_enc_t e;
    yusb_yx_dec_t *d = calloc(1, sizeof(*d));
    CHECK(d);
    yusb_yx_params_t p = params(0x10, 0);
    CHECK(yusb_yx_enc_init(&e, &p) == YUSB_ERR_RANGE);
    CHECK(yusb_yx_dec_init(d, &p) == YUSB_ERR_RANGE);
    p = params(YUSB_YX_CACHE, 0);
    CHECK(yusb_yx_enc_init(&e, &p) == YUSB_ERR_RANGE);
    p = params(YUSB_YX_CACHE, YUSB_YX_MAX_CACHE_BLOCKS + 1);
    CHECK(yusb_yx_enc_init(&e, &p) == YUSB_ERR_RANGE);
    CHECK(yusb_yx_dec_init(d, &p) == YUSB_ERR_RANGE);
    CHECK(yusb_yx_enc_init(&e, NULL) == YUSB_ERR_RANGE);
    /* A decoder whose init failed refuses everything. */
    uint8_t raw[4 + 512] = {0, 0, 0x02, 0x00}, dst[512];
    CHECK(yusb_yx_decode(d, raw, sizeof(raw), dst, sizeof(dst), NULL) == YUSB_ERR_PROTOCOL);
    /* cache_blocks is ignored without CACHE; free is idempotent. */
    p = params(YUSB_YX_FILL, 9999);
    CHECK(yusb_yx_enc_init(&e, &p) == YUSB_OK && !e.cache);
    yusb_yx_enc_free(&e);
    yusb_yx_enc_free(&e);
    p = params(ALL | YUSB_YX_READAHEAD, YUSB_YX_MAX_CACHE_BLOCKS);
    CHECK(yusb_yx_enc_init(&e, &p) == YUSB_OK && yusb_yx_dec_init(d, &p) == YUSB_OK);
    CHECK(e.index_mask + 1u == 2u * YUSB_YX_MAX_CACHE_BLOCKS);
    yusb_yx_enc_free(&e);
    yusb_yx_dec_free(d);
    yusb_yx_dec_free(d);
    free(d);
}

/* Lever sets and lengths exercised by the round trips. */
static const uint8_t LEVER_SETS[] = {0, YUSB_YX_FILL, YUSB_YX_LZ4, YUSB_YX_CACHE,
                                     YUSB_YX_FILL | YUSB_YX_LZ4, ALL};
static const size_t LENGTHS[] = {1, 2, 511, 512, 513, 4095, 4096, 4097, 8191, 65536};

static void test_roundtrip(void)
{
    for (size_t l = 0; l < sizeof(LEVER_SETS); ++l) {
        pair_t *p = pair_new(LEVER_SETS[l], 32);
        for (int cls = 0; cls < CLASSES; ++cls) {
            for (size_t i = 0; i < sizeof(LENGTHS) / sizeof(LENGTHS[0]); ++i) {
                uint8_t *src = xmalloc(LENGTHS[i]);
                fill_class(src, LENGTHS[i], cls);
                roundtrip(p, src, LENGTHS[i], (int)(i % 3));
                free(src);
            }
            check_sync(p);
        }
        pair_free(p);
    }
}

static void test_large(void)
{
    const size_t mib = 1u << 20;
    uint8_t *src = xmalloc(mib);
    for (int cls = 0; cls < CLASSES; ++cls) {
        pair_t *p = pair_new(ALL, 64);
        fill_class(src, mib, cls);
        size_t n = roundtrip(p, src, mib, cls & 1);
        if (cls == ZEROS || cls == ONES) CHECK(n == 256u * 5u);
        if (cls == RANDOM) CHECK(n == yusb_yx_bound(mib));
        if (cls == TEXT) CHECK(n < mib / 5u * 3u);                    /* LZ4 on word salad */
        roundtrip(p, src, mib, 0);                                    /* again: from cache */
        check_sync(p);
        pair_free(p);
    }
    free(src);
}

/* Exact FILL choice: smallest power-of-two period below the block length. */
static void test_fill(void)
{
    static const struct { size_t len, period, unit; } cases[] = {
        {4096, 1, 1}, {4096, 2, 2}, {4096, 4, 4}, {4096, 8, 8}, {4096, 512, 512},
        {4096, 1024, 0}, {4096, 3, 0}, {4096, 4096, 0}, {1000, 256, 256}, {512, 512, 0},
        {512, 256, 256}, {513, 512, 512}, {2, 1, 1}, {1, 1, 0}, {3, 2, 2}, {7, 7, 0},
    };
    for (size_t i = 0; i < sizeof(cases) / sizeof(cases[0]); ++i) {
        pair_t *p = pair_new(YUSB_YX_FILL, 0);
        size_t len = cases[i].len, unit = cases[i].unit;
        uint8_t *src = xmalloc(len), *wire = xmalloc(yusb_yx_bound(len));
        fill_period(src, len, cases[i].period);
        size_t n = yusb_yx_encode(&p->enc, src, len, wire);
        if (unit) {
            CHECK(n == 4 + unit && wire[0] == YUSB_YX_KIND_FILL && wire[1] == 0);
            CHECK((size_t)(wire[2] << 8 | wire[3]) == unit && !memcmp(wire + 4, src, unit));
        } else {
            CHECK(n == 4 + len && wire[0] == YUSB_YX_KIND_RAW);
        }
        free(src);
        free(wire);
        pair_free(p);
    }
    /* A zero block with one stray byte at the end is not a FILL. */
    pair_t *p = pair_new(YUSB_YX_FILL, 0);
    uint8_t *src = calloc(1, BLOCK), *wire = xmalloc(yusb_yx_bound(BLOCK));
    CHECK(src);
    src[BLOCK - 1] = 1;
    CHECK(yusb_yx_encode(&p->enc, src, BLOCK, wire) == 4 + BLOCK && wire[0] == 0);
    free(src);
    free(wire);
    pair_free(p);
}

static void test_lz4(void)
{
    pair_t *p = pair_new(YUSB_YX_LZ4, 0);
    uint8_t *src = xmalloc(16 * BLOCK + 100), *wire = xmalloc(yusb_yx_bound(16 * BLOCK + 100));
    fill_text(src, 16 * BLOCK + 100);
    size_t n = yusb_yx_encode(&p->enc, src, 16 * BLOCK + 100, wire);
    size_t at = 0, blocks = 0;
    while (at < n) {
        size_t len = blocks < 16 ? BLOCK : 100, wl = (size_t)(wire[at + 2] << 8 | wire[at + 3]);
        CHECK(wire[at] == YUSB_YX_KIND_LZ4 && wl < len - len / 8u);
        at += 4 + wl;
        blocks++;
    }
    CHECK(at == n && blocks == 17);
    CHECK(p->enc.stats.lz4_tries == 17 && p->enc.stats.lz4_bypass == 0);
    /* Zeros: FILL is not agreed, LZ4 takes them. */
    memset(src, 0, BLOCK);
    CHECK(yusb_yx_encode(&p->enc, src, BLOCK, wire) < 40 && wire[0] == YUSB_YX_KIND_LZ4);
    pair_free(p);
    /* The one-eighth rule at its boundary: a block LZ4 compresses to exactly
     * 4096 - 512 bytes is sent RAW, one byte less is sent as LZ4. */
    static char lz[LZ4_COMPRESSBOUND(BLOCK)];
    uint8_t *noise = src + BLOCK;
    fill_random(noise, BLOCK);
    unsigned seen = 0;
    for (size_t k = 3300; k < BLOCK && seen != 3; ++k) {
        memcpy(src, noise, k);
        memset(src + k, 0, BLOCK - k);
        int z = LZ4_compress_default((const char *)src, lz, BLOCK, (int)sizeof(lz));
        if (z != BLOCK - BLOCK / 8 && z != BLOCK - BLOCK / 8 - 1) continue;
        p = pair_new(YUSB_YX_LZ4, 0);
        size_t w = yusb_yx_encode(&p->enc, src, BLOCK, wire);
        if (z == BLOCK - BLOCK / 8) CHECK(wire[0] == YUSB_YX_KIND_RAW && w == 4 + BLOCK);
        else CHECK(wire[0] == YUSB_YX_KIND_LZ4 && w == 4u + (size_t)z &&
                   !memcmp(wire + 4, lz, w - 4u));
        seen |= z == BLOCK - BLOCK / 8 ? 1u : 2u;
        pair_free(p);
    }
    CHECK(seen == 3);
    free(src);
    free(wire);
}

/* Incompressible bypass: 8 misses, skip 128, probe; a missed probe skips again. */
static void test_skip(void)
{
    pair_t *p = pair_new(YUSB_YX_FILL | YUSB_YX_LZ4, 0);
    uint8_t *src = xmalloc(300 * BLOCK);
    fill_random(src, 300 * BLOCK);
    roundtrip(p, src, 300 * BLOCK, 1);
    /* tries: blocks 1-8, probe at 137, probe at 266; 34 of the next skip used. */
    CHECK(p->enc.stats.lz4_tries == 10 && p->enc.stats.lz4_bypass == 10);
    CHECK(p->enc.stats.blocks[YUSB_YX_KIND_RAW] == 300);
    CHECK(p->enc.lz4_skip == 94);
    /* Compressible again: 94 skipped blocks, then the probe succeeds. */
    fill_text(src, 200 * BLOCK);
    roundtrip(p, src, 200 * BLOCK, 0);
    CHECK(p->enc.stats.lz4_tries == 11 + 105 && p->enc.stats.lz4_bypass == 10);
    CHECK(p->enc.stats.blocks[YUSB_YX_KIND_RAW] == 300 + 94);
    CHECK(p->enc.stats.blocks[YUSB_YX_KIND_LZ4] == 106);
    CHECK(p->enc.lz4_misses == 0 && p->enc.lz4_skip == 0);
    /* FILL blocks neither try LZ4 nor use up the skip. */
    pair_free(p);
    p = pair_new(YUSB_YX_FILL | YUSB_YX_LZ4, 0);
    fill_random(src, 8 * BLOCK);
    memset(src + 8 * BLOCK, 0, 50 * BLOCK);
    roundtrip(p, src, 58 * BLOCK, 2);
    CHECK(p->enc.lz4_skip == 128 && p->enc.stats.blocks[YUSB_YX_KIND_FILL] == 50);
    pair_free(p);
    /* Any success clears the miss count: 7 misses, 1 hit, 7 misses never skip. */
    p = pair_new(YUSB_YX_LZ4, 0);
    fill_random(src, 15 * BLOCK);
    fill_text(src + 7 * BLOCK, BLOCK);
    roundtrip(p, src, 15 * BLOCK, 0);
    CHECK(p->enc.stats.lz4_tries == 15 && p->enc.stats.lz4_bypass == 14 && !p->enc.lz4_skip);
    CHECK(p->enc.lz4_misses == 7);
    pair_free(p);
    free(src);
}

static void test_cache(void)
{
    /* A repeated full block becomes REF within one message and across messages. */
    pair_t *p = pair_new(ALL, 8);
    uint8_t *src = xmalloc(4 * BLOCK + 10), *wire = xmalloc(yusb_yx_bound(4 * BLOCK + 10));
    fill_random(src, BLOCK);
    memcpy(src + BLOCK, src, BLOCK);
    fill_random(src + 2 * BLOCK, BLOCK);
    memcpy(src + 3 * BLOCK, src, BLOCK);
    fill_random(src + 4 * BLOCK, 10);
    roundtrip(p, src, 4 * BLOCK + 10, 1);
    CHECK(p->enc.stats.blocks[YUSB_YX_KIND_REF] == 2 && p->enc.inserted == 2);
    size_t n = yusb_yx_encode(&p->enc, src + 2 * BLOCK, BLOCK, wire);
    uint32_t h = XXH32(src + 2 * BLOCK, BLOCK, 0);
    static const uint8_t ref1[8] = {3, 0, 0, 8, 0, 0, 0, 1};
    CHECK(n == 12 && !memcmp(wire, ref1, 8));
    CHECK(wire[8] == (uint8_t)(h >> 24) && wire[11] == (uint8_t)h);
    yusb_yx_dec_begin(&p->dec, NULL, 0);
    uint8_t *dst = xmalloc(BLOCK);
    CHECK(yusb_yx_decode(&p->dec, wire, n, dst, BLOCK, NULL) == YUSB_OK);
    CHECK(!memcmp(dst, src + 2 * BLOCK, BLOCK));
    check_sync(p);
    /* FILL, REF and partial blocks are never stored; LZ4 blocks are. */
    memset(src, 0, BLOCK);
    fill_text(src + BLOCK, BLOCK);
    roundtrip(p, src, 2 * BLOCK + 10, 0);
    CHECK(p->enc.inserted == 3);
    check_sync(p);
    pair_free(p);
    /* Eviction with a slot count that is not a power of two; caches stay equal. */
    for (uint16_t slots = 1; slots <= 5; slots += 2) {
        p = pair_new(YUSB_YX_LZ4 | YUSB_YX_CACHE, slots);
        uint8_t *pool = xmalloc(8 * BLOCK);
        fill_random(pool, 8 * BLOCK);
        for (int msg = 0; msg < 200; ++msg) {
            size_t blocks = 1 + rnd() % 4;
            for (size_t b = 0; b < blocks; ++b)
                memcpy(src + b * BLOCK, pool + (rnd() % (slots + 2u)) * BLOCK, BLOCK);
            roundtrip(p, src, blocks * BLOCK, msg % 3);
            check_sync(p);
            CHECK(p->enc.inserted < 2u * slots);
        }
        CHECK(p->enc.stats.blocks[YUSB_YX_KIND_REF] > 0);
        CHECK(p->enc.stats.blocks[YUSB_YX_KIND_REF] == p->dec.stats.blocks[YUSB_YX_KIND_REF]);
        free(pool);
        pair_free(p);
    }
    free(src);
    free(wire);
    free(dst);
}

/* Many messages over a cache smaller than the working set. */
static void test_cache_sync(void)
{
    pair_t *p = pair_new(ALL, 16);
    uint8_t *pool = xmalloc(40 * BLOCK), *src = xmalloc(16 * BLOCK + 3000);
    for (int i = 0; i < 40; ++i) fill_class(pool + i * BLOCK, BLOCK, i % 3 ? RANDOM : TEXT);
    for (int msg = 0; msg < 300; ++msg) {
        size_t blocks = rnd() % 16, len = blocks * BLOCK + (rnd() % 2 ? rnd() % 3000 : 0);
        if (!len) len = 512;
        for (size_t b = 0; b < blocks; ++b)
            memcpy(src + b * BLOCK, pool + (rnd() % 40) * BLOCK, BLOCK);
        fill_random(src + blocks * BLOCK, len - blocks * BLOCK);
        roundtrip(p, src, len, msg % 3);
        check_sync(p);
    }
    CHECK(p->enc.stats.blocks[YUSB_YX_KIND_REF] > 100);
    CHECK(!memcmp(&p->enc.stats.blocks, &p->dec.stats.blocks, sizeof(p->enc.stats.blocks)));
    free(pool);
    free(src);
    pair_free(p);
}

static void test_ref_reject(void)
{
    uint8_t blk[BLOCK];
    fill_random(blk, BLOCK);
    uint32_t h = XXH32(blk, BLOCK, 0);
    stream_t *pre = calloc(1, sizeof(*pre)), *s = calloc(1, sizeof(*s));
    CHECK(pre && s);
    put_block(pre, YUSB_YX_KIND_RAW, 0, BLOCK, blk, BLOCK);          /* fills slot 0 */
    /* The control case decodes. */
    put_ref(s, 0, h);
    {
        yusb_yx_params_t a = params(YUSB_YX_CACHE, 4);
        yusb_yx_dec_t *d = calloc(1, sizeof(*d));
        uint8_t *dst = xmalloc(BLOCK), *tmp = xmalloc(BLOCK);
        CHECK(d && yusb_yx_dec_init(d, &a) == YUSB_OK);
        CHECK(yusb_yx_decode(d, pre->b, pre->n, tmp, BLOCK, NULL) == YUSB_OK);
        CHECK(yusb_yx_decode(d, s->b, s->n, dst, BLOCK, NULL) == YUSB_OK);
        CHECK(!memcmp(dst, blk, BLOCK) && d->inserted == 1);
        yusb_yx_dec_free(d);
        free(d);
        free(dst);
        free(tmp);
    }
    expect_reject(YUSB_YX_CACHE, 4, NULL, 0, s, BLOCK);              /* empty cache */
    s->n = 0;
    put_ref(s, 1, h);
    expect_reject(YUSB_YX_CACHE, 4, pre, BLOCK, s, BLOCK);           /* slot never written */
    s->n = 0;
    put_ref(s, 0, h ^ 1u);
    expect_reject(YUSB_YX_CACHE, 4, pre, BLOCK, s, BLOCK);           /* wrong check */
    s->n = 0;
    put_ref(s, 4, h);
    expect_reject(YUSB_YX_CACHE, 4, pre, BLOCK, s, BLOCK);           /* slot >= cache_blocks */
    s->n = 0;
    put_ref(s, 0xffffffffu, h);
    expect_reject(YUSB_YX_CACHE, 4, pre, BLOCK, s, BLOCK);
    s->n = 0;
    put_ref(s, 0, h);
    expect_reject(YUSB_YX_FILL | YUSB_YX_LZ4, 0, pre, BLOCK, s, BLOCK);  /* lever not agreed */
    expect_reject(YUSB_YX_CACHE, 4, pre, BLOCK, s, 4000);            /* partial block */
    expect_reject(YUSB_YX_CACHE, 4, pre, BLOCK, s, 1);
    uint8_t body[9] = {0};
    s->n = 0;
    put_block(s, YUSB_YX_KIND_REF, 0, 7, body, 7);
    expect_reject(YUSB_YX_CACHE, 4, pre, BLOCK, s, BLOCK);
    s->n = 0;
    put_block(s, YUSB_YX_KIND_REF, 0, 9, body, 9);
    expect_reject(YUSB_YX_CACHE, 4, pre, BLOCK, s, BLOCK);
    /* A FILL block is not cached, so a REF to "its" slot is empty. */
    pre->n = 0;
    put_block(pre, YUSB_YX_KIND_FILL, 0, 1, body, 1);
    memset(blk, 0, BLOCK);
    s->n = 0;
    put_ref(s, 0, XXH32(blk, BLOCK, 0));
    expect_reject(ALL, 4, pre, BLOCK, s, BLOCK);
    free(pre);
    free(s);
}

static void test_violations(void)
{
    uint8_t body[BLOCK + 16], small[4200];
    fill_random(body, sizeof(body));
    stream_t *s = calloc(1, sizeof(*s));
    CHECK(s);
    static const unsigned kinds[] = {4, 5, 0x7f, 0x80, 0xff};
    for (size_t i = 0; i < sizeof(kinds) / sizeof(kinds[0]); ++i) {
        s->n = 0;
        put_block(s, kinds[i], 0, 1, body, 1);
        expect_reject(0xff & YUSB_YX_LEVERS, 4, NULL, 0, s, BLOCK);
    }
    s->n = 0;                                                         /* aux != 0 */
    put_block(s, YUSB_YX_KIND_RAW, 1, 600, body, 600);
    expect_reject(ALL, 4, NULL, 0, s, 600);
    s->n = 0;
    put_block(s, YUSB_YX_KIND_FILL, 0x80, 1, body, 1);
    expect_reject(ALL, 4, NULL, 0, s, 600);
    /* RAW length must equal the block length. */
    s->n = 0;
    put_block(s, YUSB_YX_KIND_RAW, 0, 599, body, 599);
    expect_reject(ALL, 4, NULL, 0, s, 600);
    s->n = 0;
    put_block(s, YUSB_YX_KIND_RAW, 0, 601, body, 601);
    expect_reject(ALL, 4, NULL, 0, s, 600);
    s->n = 0;
    put_block(s, YUSB_YX_KIND_RAW, 0, BLOCK + 1, body, BLOCK + 1);
    expect_reject(ALL, 4, NULL, 0, s, BLOCK + 1);
    s->n = 0;
    put_block(s, YUSB_YX_KIND_RAW, 0, 0xffff, body, 0);
    expect_reject(ALL, 4, NULL, 0, s, 3 * BLOCK);
    /* FILL: 1..512 and <= block length, lever required. */
    static const struct { unsigned wire; size_t len; } fills[] = {
        {0, 600}, {513, 600}, {513, 4096}, {300, 200}, {0xffff, 4096}};
    for (size_t i = 0; i < sizeof(fills) / sizeof(fills[0]); ++i) {
        s->n = 0;
        put_block(s, YUSB_YX_KIND_FILL, 0, fills[i].wire, body,
                  fills[i].wire < 600 ? fills[i].wire : 0);
        expect_reject(ALL, 4, NULL, 0, s, fills[i].len);
    }
    s->n = 0;
    put_block(s, YUSB_YX_KIND_FILL, 0, 1, body, 1);
    expect_reject(YUSB_YX_LZ4 | YUSB_YX_CACHE, 4, NULL, 0, s, 600);
    /* LZ4: 1..block length - 1, lever required, exact decoded length. */
    memset(small, 'z', sizeof(small));
    char lz[64];
    int zn = LZ4_compress_default((const char *)small, lz, 600, sizeof(lz));
    CHECK(zn > 0 && zn < 40);
    s->n = 0;
    put_block(s, YUSB_YX_KIND_LZ4, 0, (unsigned)zn, lz, (size_t)zn);
    expect_reject(YUSB_YX_FILL | YUSB_YX_CACHE, 4, NULL, 0, s, 600);  /* lever not agreed */
    expect_reject(ALL, 4, NULL, 0, s, 601);                           /* decodes short */
    expect_reject(ALL, 4, NULL, 0, s, 599);                           /* decodes long */
    s->n = 0;
    put_block(s, YUSB_YX_KIND_LZ4, 0, 0, body, 0);
    expect_reject(ALL, 4, NULL, 0, s, 600);
    s->n = 0;
    put_block(s, YUSB_YX_KIND_LZ4, 0, 600, body, 600);                /* wire == block length */
    expect_reject(ALL, 4, NULL, 0, s, 600);
    s->n = 0;
    put_block(s, YUSB_YX_KIND_LZ4, 0, 300, body, 300);                /* garbage */
    expect_reject(ALL, 4, NULL, 0, s, 600);
    zn = LZ4_compress_default((const char *)small, lz, (int)sizeof(small), sizeof(lz));
    CHECK(zn > 0);
    s->n = 0;
    put_block(s, YUSB_YX_KIND_LZ4, 0, (unsigned)zn, lz, (size_t)zn);  /* 4200 into 4096 */
    expect_reject(ALL, 4, NULL, 0, s, BLOCK);
    /* The control: the same 600-byte LZ4 body decodes when lengths match. */
    zn = LZ4_compress_default((const char *)small, lz, 600, sizeof(lz));
    s->n = 0;
    put_block(s, YUSB_YX_KIND_LZ4, 0, (unsigned)zn, lz, (size_t)zn);
    yusb_yx_params_t a = params(YUSB_YX_LZ4, 0);
    yusb_yx_dec_t *d = calloc(1, sizeof(*d));
    uint8_t dst[600];
    CHECK(d && yusb_yx_dec_init(d, &a) == YUSB_OK);
    CHECK(yusb_yx_decode(d, s->b, s->n, dst, 600, NULL) == YUSB_OK && !memcmp(dst, small, 600));
    /* A violation in a later block, after good blocks. */
    s->n = 0;
    put_block(s, YUSB_YX_KIND_RAW, 0, BLOCK, body, BLOCK);
    put_block(s, YUSB_YX_KIND_RAW, 0, 9, body, 9);
    expect_reject(ALL, 4, NULL, 0, s, BLOCK + 10);
    yusb_yx_dec_free(d);
    free(d);
    free(s);
}

/* The decoder accepts any wire-valid FILL unit, not only what the encoder emits. */
static void test_fill_decode(void)
{
    yusb_yx_params_t a = params(YUSB_YX_FILL, 0);
    yusb_yx_dec_t *d = calloc(1, sizeof(*d));
    stream_t *s = calloc(1, sizeof(*s));
    CHECK(d && s && yusb_yx_dec_init(d, &a) == YUSB_OK);
    static const struct { size_t unit, len; } cases[] = {
        {3, 4096}, {7, 1000}, {512, 512}, {500, 700}, {1, 1}, {511, 4096}, {200, 200}};
    for (size_t i = 0; i < sizeof(cases) / sizeof(cases[0]); ++i) {
        uint8_t unit[512], *dst = xmalloc(cases[i].len);
        fill_random(unit, cases[i].unit);
        s->n = 0;
        put_block(s, YUSB_YX_KIND_FILL, 0, (unsigned)cases[i].unit, unit, cases[i].unit);
        CHECK(feed(d, s->b, s->n, dst, cases[i].len, 1 + i % 2, NULL) == YUSB_OK);
        for (size_t j = 0; j < cases[i].len; ++j) CHECK(dst[j] == unit[j % cases[i].unit]);
        free(dst);
    }
    yusb_yx_dec_free(d);
    free(d);
    free(s);
}

static void test_space(void)
{
    pair_t *p = pair_new(ALL, 4);
    uint8_t *src = xmalloc(3 * BLOCK), *dst = xmalloc(3 * BLOCK);
    uint8_t *wire = xmalloc(yusb_yx_bound(3 * BLOCK)), *buf;
    fill_random(src, BLOCK);                                          /* RAW */
    fill_text(src + BLOCK, BLOCK);                                    /* LZ4 */
    memset(src + 2 * BLOCK, 7, BLOCK);                                /* FILL */
    size_t n = yusb_yx_encode(&p->enc, src, 3 * BLOCK, wire), at = 0;
    yusb_yx_dec_t *d = &p->dec;
    CHECK(yusb_yx_dec_space(d, &buf) == 0);                           /* before begin */
    yusb_yx_dec_begin(d, dst, 3 * BLOCK);
    for (int block = 0; block < 3; ++block) {
        for (size_t h = 0; h < 4; ++h) {
            CHECK(yusb_yx_dec_space(d, &buf) == 4 - h && buf == d->header + h);
            *buf = wire[at++];
            CHECK(yusb_yx_dec_feed(d, 1) == YUSB_OK);
        }
        size_t body = (size_t)(wire[at - 2] << 8 | wire[at - 1]);
        size_t want = yusb_yx_dec_space(d, &buf);
        CHECK(want == body);
        if (block == 0) CHECK(buf == dst);                            /* RAW: zero copy */
        else CHECK(buf == d->staging);
        memcpy(buf, wire + at, body - 1);
        CHECK(yusb_yx_dec_feed(d, body - 1) == YUSB_OK);
        CHECK(yusb_yx_dec_space(d, &buf) == 1);
        CHECK(buf == (block == 0 ? dst + body - 1 : d->staging + body - 1));
        *buf = wire[at + body - 1];
        CHECK(yusb_yx_dec_feed(d, 1) == YUSB_OK);
        at += body;
        CHECK(d->done == (size_t)(block + 1) * BLOCK);
    }
    CHECK(at == n && yusb_yx_dec_space(d, &buf) == 0 && !memcmp(dst, src, 3 * BLOCK));
    CHECK(yusb_yx_dec_feed(d, 0) == YUSB_OK);
    /* Bytes beyond the space offered are a caller bug, and fatal. */
    CHECK(yusb_yx_dec_feed(d, 1) == YUSB_ERR_RANGE);
    CHECK(yusb_yx_dec_feed(d, 0) == YUSB_ERR_PROTOCOL);
    pair_free(p);
    p = pair_new(ALL, 4);
    yusb_yx_dec_begin(&p->dec, dst, BLOCK);
    CHECK(yusb_yx_dec_space(&p->dec, &buf) == 4);
    CHECK(yusb_yx_dec_feed(&p->dec, 5) == YUSB_ERR_RANGE);
    CHECK(yusb_yx_dec_space(&p->dec, &buf) == 0);
    pair_free(p);
    free(src);
    free(dst);
    free(wire);
}

static void test_split(void)
{
    for (size_t l = 0; l < sizeof(LEVER_SETS); ++l) {
        pair_t *p = pair_new(LEVER_SETS[l], 8);
        uint8_t *src = xmalloc(20 * BLOCK + 77);
        for (int msg = 0; msg < 40; ++msg) {
            size_t len = 1 + rnd() % (20 * BLOCK + 77);
            fill_class(src, len, MIXED);
            if (len > 2 * BLOCK) memcpy(src + BLOCK, src, BLOCK);
            roundtrip(p, src, len, 1 + msg % 2);
            check_sync(p);
        }
        free(src);
        pair_free(p);
    }
}

static void test_truncated(void)
{
    pair_t *p = pair_new(ALL, 4);
    uint8_t *src = xmalloc(2 * BLOCK), *dst = xmalloc(2 * BLOCK);
    uint8_t *wire = xmalloc(yusb_yx_bound(2 * BLOCK) + 9);
    fill_text(src, 2 * BLOCK);
    size_t n = yusb_yx_encode(&p->enc, src, 2 * BLOCK, wire), used = 0;
    memset(wire + n, 0xee, 9);                                        /* the next message */
    CHECK(yusb_yx_decode(&p->dec, wire, n + 9, dst, 2 * BLOCK, &used) == YUSB_OK);
    CHECK(used == n && !memcmp(dst, src, 2 * BLOCK));
    pair_free(p);
    for (size_t cut = 0; cut < n; cut += 1 + cut / 3) {
        p = pair_new(ALL, 4);
        yusb_yx_encode(&p->enc, src, 2 * BLOCK, wire);
        CHECK(yusb_yx_decode(&p->dec, wire, cut, dst, 2 * BLOCK, &used) == YUSB_ERR_PROTOCOL);
        CHECK(used == cut);
        pair_free(p);
    }
    p = pair_new(0, 0);
    CHECK(yusb_yx_decode(&p->dec, wire, n, dst, 0, &used) == YUSB_OK && used == 0);
    CHECK(yusb_yx_encode(&p->enc, src, 0, wire) == 0);
    pair_free(p);
    free(src);
    free(dst);
    free(wire);
}

static void test_determinism(void)
{
    pair_t *a = pair_new(ALL, 16), *b = pair_new(ALL, 16);
    uint8_t *src = xmalloc(24 * BLOCK);
    uint8_t *wa = xmalloc(yusb_yx_bound(24 * BLOCK)), *wb = xmalloc(yusb_yx_bound(24 * BLOCK));
    for (int msg = 0; msg < 60; ++msg) {
        size_t len = 512 + rnd() % (24 * BLOCK - 512);
        fill_class(src, len, MIXED);
        if (len > 3 * BLOCK) memcpy(src + 2 * BLOCK, src, BLOCK);
        size_t na = yusb_yx_encode(&a->enc, src, len, wa);
        size_t nb = yusb_yx_encode(&b->enc, src, len, wb);
        CHECK(na == nb && !memcmp(wa, wb, na));
    }
    CHECK(!memcmp(&a->enc.stats, &b->enc.stats, sizeof(a->enc.stats)));
    CHECK(a->enc.stats.blocks[0] && a->enc.stats.blocks[1] && a->enc.stats.blocks[2] &&
          a->enc.stats.blocks[3]);
    pair_free(a);
    pair_free(b);
    free(src);
    free(wa);
    free(wb);
}

static void test_stats(void)
{
    pair_t *p = pair_new(ALL, 4);
    uint8_t *src = xmalloc(5 * BLOCK + 100);
    fill_random(src, BLOCK);                                          /* RAW, slot 0 */
    memset(src + BLOCK, 0, BLOCK);                                    /* FILL unit 1 */
    fill_text(src + 2 * BLOCK, BLOCK);                                /* LZ4, slot 1 */
    memcpy(src + 3 * BLOCK, src, BLOCK);                              /* REF 0 */
    fill_period(src + 4 * BLOCK, BLOCK, 512);                         /* FILL unit 512 */
    fill_random(src + 5 * BLOCK, 100);                                /* RAW partial */
    size_t n = roundtrip(p, src, 5 * BLOCK + 100, 2);
    const yusb_yx_stats_t *s = &p->enc.stats;
    CHECK(s->blocks[YUSB_YX_KIND_RAW] == 2 && s->blocks[YUSB_YX_KIND_FILL] == 2);
    CHECK(s->blocks[YUSB_YX_KIND_LZ4] == 1 && s->blocks[YUSB_YX_KIND_REF] == 1);
    CHECK(s->raw_bytes == 5 * BLOCK + 100 && s->wire_bytes == n);
    CHECK(s->lz4_tries == 3 && s->lz4_bypass == 2);
    const yusb_yx_stats_t *r = &p->dec.stats;
    CHECK(!memcmp(r->blocks, s->blocks, sizeof(s->blocks)));
    CHECK(r->raw_bytes == s->raw_bytes && r->wire_bytes == s->wire_bytes);
    CHECK(r->lz4_tries == 0 && r->lz4_bypass == 0);
    CHECK(p->enc.inserted == 2);
    check_sync(p);
    pair_free(p);
    free(src);
}

/* The insertion counter stays bounded and keeps the inserted % slots order. */
static void test_inserted(void)
{
    pair_t *p = pair_new(YUSB_YX_CACHE, 3);
    uint8_t *src = xmalloc(BLOCK);
    for (uint32_t i = 0; i < 20; ++i) {
        fill_random(src, BLOCK);
        roundtrip(p, src, BLOCK, 0);
        uint32_t count = i + 1u;
        CHECK(p->enc.inserted == (count < 6 ? count : 3u + count % 3u));
        CHECK(!memcmp(p->enc.cache + (size_t)(i % 3u) * BLOCK, src, BLOCK));
        check_sync(p);
    }
    pair_free(p);
    free(src);
}

/* Two different blocks with one xxh32, from a birthday search over a counter
 * spread across two lanes (xxh32 is a bijection of any one 32-bit lane word):
 * the second must not become a REF to the first. */
static void stamp(uint8_t *b, uint32_t i)
{
    uint32_t j = i * 0x9e3779b9u;
    memcpy(b, &i, sizeof(i));
    memcpy(b + 4, &j, sizeof(j));
}

static void test_collision(void)
{
    enum { TABLE = 1u << 20 };
    uint32_t *seen = calloc(TABLE, 2 * sizeof(uint32_t));
    uint8_t *a = xmalloc(BLOCK), *b = xmalloc(BLOCK);
    CHECK(seen);
    fill_random(a, BLOCK);
    memcpy(b, a, BLOCK);
    uint32_t found = 0, hit = 0;
    for (uint32_t i = 1; !found && i < 600000u; ++i) {
        stamp(b, i);
        uint32_t h = XXH32(b, BLOCK, 0);
        for (uint32_t at = (h * 2654435761u) >> 12;; at = (at + 1u) & (TABLE - 1u)) {
            if (!seen[2 * at]) { seen[2 * at] = i; seen[2 * at + 1] = h; break; }
            if (seen[2 * at + 1] == h) { found = i; hit = seen[2 * at]; break; }
        }
    }
    CHECK(found);
    stamp(a, hit);
    stamp(b, found);
    CHECK(memcmp(a, b, BLOCK) && XXH32(a, BLOCK, 0) == XXH32(b, BLOCK, 0));
    pair_t *p = pair_new(YUSB_YX_CACHE, 4);
    CHECK(roundtrip(p, a, BLOCK, 0) == 4 + BLOCK);
    CHECK(roundtrip(p, b, BLOCK, 0) == 4 + BLOCK);                   /* not a REF to a */
    CHECK(p->enc.stats.blocks[YUSB_YX_KIND_REF] == 0 && p->enc.inserted == 2);
    /* b now owns the shared index bucket, so b is found and a is stored again. */
    CHECK(roundtrip(p, b, BLOCK, 0) == 4 + 8);
    CHECK(roundtrip(p, a, BLOCK, 0) == 4 + BLOCK && p->enc.inserted == 3);
    CHECK(roundtrip(p, a, BLOCK, 0) == 4 + 8);
    check_sync(p);
    pair_free(p);
    free(seen);
    free(a);
    free(b);
}

/* encode_iov: the same bytes as encode from the same history (two encoders
 * fed identically), RAW bodies taken from src, out pieces merged. */
static bool in_range(const uint8_t *p, const uint8_t *base, size_t n) { return p >= base && p < base + n; }

static void test_iov(void)
{
    for (size_t l = 0; l < sizeof(LEVER_SETS); ++l) {
        pair_t *a = pair_new(LEVER_SETS[l], 8), *b = pair_new(LEVER_SETS[l], 8);
        size_t max = 6 * BLOCK + 100;
        uint8_t *src = xmalloc(max), *wa = xmalloc(yusb_yx_bound(max)), *ob = xmalloc(yusb_yx_bound(max));
        uint8_t *cat = xmalloc(yusb_yx_bound(max));
        yusb_iov_t iov[16];
        for (int msg = 0; msg < 80; ++msg) {
            size_t len = msg < 9 ? LENGTHS[msg] : 1 + rnd() % max;   /* LENGTHS[9] > max */
            int cls = msg % 4 == 3 ? RANDOM : MIXED;
            fill_class(src, len, cls);
            if (len > 3 * BLOCK && msg % 2) memcpy(src + 2 * BLOCK, src, BLOCK);   /* REF */
            size_t segs = yusb_yx_segments(len), w = 1;
            CHECK(segs <= 16);
            /* Too few segments: nothing happens. */
            CHECK(yusb_yx_encode_iov(&b->enc, src, len, ob, iov, segs - 1, &w) == 0 && w == 0);
            size_t na = yusb_yx_encode(&a->enc, src, len, wa);
            size_t n = yusb_yx_encode_iov(&b->enc, src, len, ob, iov, segs, &w);
            CHECK(n >= 1 && n <= segs && w == na);
            size_t at = 0;
            bool prev_out = false;
            for (size_t i = 0; i < n; ++i) {
                bool from_out = in_range(iov[i].base, ob, yusb_yx_bound(len));
                CHECK(from_out || (in_range(iov[i].base, src, len) && iov[i].base + iov[i].len <= src + len));
                CHECK(!(from_out && prev_out));   /* adjacent out pieces share one */
                prev_out = from_out;
                memcpy(cat + at, iov[i].base, iov[i].len);
                at += iov[i].len;
            }
            CHECK(at == na && !memcmp(cat, wa, na));
            if (cls == RANDOM && len >= 64) CHECK(n >= 2 && !in_range(iov[1].base, ob, yusb_yx_bound(len)));
            CHECK(!memcmp(&a->enc.stats, &b->enc.stats, sizeof(a->enc.stats)));
            CHECK(a->enc.inserted == b->enc.inserted);
        }
        CHECK(yusb_yx_encode_iov(&b->enc, src, 0, ob, iov, 16, NULL) == 0);
        pair_free(a);
        pair_free(b);
        free(src);
        free(wa);
        free(ob);
        free(cat);
    }
}

static void test_frame(void)
{
    uint8_t raw[48] = {0};
    raw[3] = 1;   /* CMD_SUBMIT */
    CHECK(!yusb_yx_unframe(raw) && raw[3] == 1 && !raw[0]);
    yusb_yx_frame(raw, true);
    CHECK(raw[0] == 0x59 && raw[1] == 0x58 && raw[2] == 0 && raw[3] == 1);
    CHECK(yusb_yx_unframe(raw) && !raw[0] && !raw[1] && !raw[2] && raw[3] == 1);
    raw[3] = 3;   /* RET_SUBMIT */
    yusb_yx_frame(raw, true);
    CHECK(((uint32_t)raw[0] << 24 | (uint32_t)raw[1] << 16 | (uint32_t)raw[2] << 8 | raw[3]) ==
          YUSB_YX_RET_SUBMIT);
    yusb_yx_frame(raw, false);
    CHECK(!raw[0] && !raw[1] && !raw[2] && raw[3] == 3);
    /* Only the two YX codes unframe. */
    const uint8_t others[][4] = {{0x59, 0x58, 0, 2}, {0x59, 0x58, 0, 4}, {0x59, 0x58, 1, 1}, {0x59, 0, 0, 1}};
    for (size_t i = 0; i < sizeof(others) / sizeof(others[0]); ++i) {
        memcpy(raw, others[i], 4);
        CHECK(!yusb_yx_unframe(raw) && !memcmp(raw, others[i], 4));
    }
}

/* Free is safe on zeroed and failed structs and twice; footprints follow
 * the levers (exact byte counts are checked by export_test's counted build). */
static void test_free_safety(void)
{
    yusb_yx_enc_t e;
    yusb_yx_dec_t *d = calloc(1, sizeof(*d));
    CHECK(d);
    memset(&e, 0, sizeof(e));
    yusb_yx_enc_free(&e);
    yusb_yx_enc_free(NULL);
    yusb_yx_dec_free(d);
    yusb_yx_dec_free(NULL);
    yusb_yx_params_t bad = params(YUSB_YX_CACHE, 0);
    CHECK(yusb_yx_enc_init(&e, &bad) == YUSB_ERR_RANGE && yusb_yx_dec_init(d, &bad) == YUSB_ERR_RANGE);
    yusb_yx_enc_free(&e);
    yusb_yx_enc_free(&e);
    yusb_yx_dec_free(d);
    yusb_yx_dec_free(d);
    CHECK(yusb_yx_enc_footprint(&bad) == 0 && yusb_yx_dec_footprint(&bad) == 0);
    CHECK(yusb_yx_enc_footprint(NULL) == 0 && yusb_yx_dec_footprint(NULL) == 0);
    yusb_yx_params_t p = params(YUSB_YX_FILL, 99);
    CHECK(yusb_yx_enc_footprint(&p) == 0 && yusb_yx_dec_footprint(&p) == 0);
    p = params(YUSB_YX_LZ4, 0);
    CHECK(yusb_yx_enc_footprint(&p) == (size_t)LZ4_sizeofState() && yusb_yx_dec_footprint(&p) == 0);
    p = params(YUSB_YX_CACHE, 16);
    CHECK(yusb_yx_dec_footprint(&p) == 16 * BLOCK);
    CHECK(yusb_yx_enc_footprint(&p) == 16 * (BLOCK + 4) + 32 * 2);
    p = params(ALL, 17);
    CHECK(yusb_yx_enc_footprint(&p) == (size_t)LZ4_sizeofState() + 17 * (BLOCK + 4) + 64 * 2);
    free(d);
}

static const struct {
    const char *name;
    void (*fn)(void);
} CASES[] = {
    {"offer", test_offer},           {"accept", test_accept},
    {"agree", test_agree},           {"bound", test_bound},
    {"init", test_init},             {"roundtrip", test_roundtrip},
    {"large", test_large},           {"fill", test_fill},
    {"lz4", test_lz4},               {"skip", test_skip},
    {"cache", test_cache},           {"cache_sync", test_cache_sync},
    {"ref_reject", test_ref_reject}, {"violations", test_violations},
    {"fill_decode", test_fill_decode}, {"space", test_space},
    {"split", test_split},           {"truncated", test_truncated},
    {"determinism", test_determinism}, {"stats", test_stats},
    {"inserted", test_inserted},     {"collision", test_collision},
    {"iov", test_iov},               {"frame", test_frame},
    {"free_safety", test_free_safety},
};

int main(int argc, char **argv)
{
    size_t count = sizeof(CASES) / sizeof(CASES[0]);
    if (argc != 2) { fprintf(stderr, "usage: yx_test <case>|list\n"); return 2; }
    if (!strcmp(argv[1], "list")) {
        for (size_t i = 0; i < count; ++i) printf("%s\n", CASES[i].name);
        return 0;
    }
    for (size_t i = 0; i < count; ++i) {
        if (strcmp(argv[1], CASES[i].name)) continue;
        CASES[i].fn();
        printf("ok %s\n", CASES[i].name);
        return 0;
    }
    fprintf(stderr, "unknown case %s\n", argv[1]);
    return 2;
}
