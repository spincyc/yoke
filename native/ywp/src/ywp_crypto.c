/* Portable SHA-256 (FIPS 180-4) and HMAC-SHA256 (RFC 2104) over the
 * platform hash seam. Written for this project; no third-party code. */
#include <string.h>
#include "ywp_crypto.h"

typedef struct {
    uint32_t h[8];
    uint64_t bytes;
    uint8_t buf[YWP_SHA256_BLOCK];
    size_t used;
} sha256_t;

_Static_assert(sizeof(sha256_t) <= YWP_SHA256_STATE_MAX, "portable SHA-256 state");

static const uint32_t K[64] = {
    0x428a2f98u, 0x71374491u, 0xb5c0fbcfu, 0xe9b5dba5u, 0x3956c25bu, 0x59f111f1u,
    0x923f82a4u, 0xab1c5ed5u, 0xd807aa98u, 0x12835b01u, 0x243185beu, 0x550c7dc3u,
    0x72be5d74u, 0x80deb1feu, 0x9bdc06a7u, 0xc19bf174u, 0xe49b69c1u, 0xefbe4786u,
    0x0fc19dc6u, 0x240ca1ccu, 0x2de92c6fu, 0x4a7484aau, 0x5cb0a9dcu, 0x76f988dau,
    0x983e5152u, 0xa831c66du, 0xb00327c8u, 0xbf597fc7u, 0xc6e00bf3u, 0xd5a79147u,
    0x06ca6351u, 0x14292967u, 0x27b70a85u, 0x2e1b2138u, 0x4d2c6dfcu, 0x53380d13u,
    0x650a7354u, 0x766a0abbu, 0x81c2c92eu, 0x92722c85u, 0xa2bfe8a1u, 0xa81a664bu,
    0xc24b8b70u, 0xc76c51a3u, 0xd192e819u, 0xd6990624u, 0xf40e3585u, 0x106aa070u,
    0x19a4c116u, 0x1e376c08u, 0x2748774cu, 0x34b0bcb5u, 0x391c0cb3u, 0x4ed8aa4au,
    0x5b9cca4fu, 0x682e6ff3u, 0x748f82eeu, 0x78a5636fu, 0x84c87814u, 0x8cc70208u,
    0x90befffau, 0xa4506cebu, 0xbef9a3f7u, 0xc67178f2u,
};

static uint32_t ror(uint32_t x, unsigned n)
{
    return (x >> n) | (x << (32u - n));
}

static void sha256_block(uint32_t h[8], const uint8_t *p)
{
    uint32_t w[64];
    for (unsigned i = 0; i < 16; ++i)
        w[i] = (uint32_t)p[4 * i] << 24 | (uint32_t)p[4 * i + 1] << 16 |
               (uint32_t)p[4 * i + 2] << 8 | p[4 * i + 3];
    for (unsigned i = 16; i < 64; ++i) {
        uint32_t s0 = ror(w[i - 15], 7) ^ ror(w[i - 15], 18) ^ (w[i - 15] >> 3);
        uint32_t s1 = ror(w[i - 2], 17) ^ ror(w[i - 2], 19) ^ (w[i - 2] >> 10);
        w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }
    uint32_t a = h[0], b = h[1], c = h[2], d = h[3], e = h[4], f = h[5], g = h[6], k = h[7];
    for (unsigned i = 0; i < 64; ++i) {
        uint32_t t1 = k + (ror(e, 6) ^ ror(e, 11) ^ ror(e, 25)) + ((e & f) ^ (~e & g)) + K[i] + w[i];
        uint32_t t2 = (ror(a, 2) ^ ror(a, 13) ^ ror(a, 22)) + ((a & b) ^ (a & c) ^ (b & c));
        k = g; g = f; f = e; e = d + t1; d = c; c = b; b = a; a = t1 + t2;
    }
    h[0] += a; h[1] += b; h[2] += c; h[3] += d; h[4] += e; h[5] += f; h[6] += g; h[7] += k;
}

static void portable_start(void *ctx, void *state)
{
    static const uint32_t iv[8] = {0x6a09e667u, 0xbb67ae85u, 0x3c6ef372u, 0xa54ff53au,
                                   0x510e527fu, 0x9b05688cu, 0x1f83d9abu, 0x5be0cd19u};
    sha256_t *s = state;
    (void)ctx;
    memcpy(s->h, iv, sizeof(iv));
    s->bytes = 0;
    s->used = 0;
}

static void portable_update(void *ctx, void *state, const uint8_t *data, size_t len)
{
    sha256_t *s = state;
    (void)ctx;
    s->bytes += len;
    if (s->used) {
        size_t take = YWP_SHA256_BLOCK - s->used < len ? YWP_SHA256_BLOCK - s->used : len;
        memcpy(s->buf + s->used, data, take);
        s->used += take;
        data += take;
        len -= take;
        if (s->used < YWP_SHA256_BLOCK) return;
        sha256_block(s->h, s->buf);
        s->used = 0;
    }
    for (; len >= YWP_SHA256_BLOCK; data += YWP_SHA256_BLOCK, len -= YWP_SHA256_BLOCK)
        sha256_block(s->h, data);
    if (len) memcpy(s->buf, data, len);
    s->used = len;
}

static void portable_finish(void *ctx, void *state, uint8_t digest[YWP_SHA256_LEN])
{
    sha256_t *s = state;
    uint64_t bits = s->bytes * 8u;
    uint8_t pad[YWP_SHA256_BLOCK + 8] = {0x80};
    size_t padlen = (s->used < 56 ? 56 : 120) - s->used;
    for (unsigned i = 0; i < 8; ++i) pad[padlen + i] = (uint8_t)(bits >> (56 - 8 * i));
    portable_update(ctx, state, pad, padlen + 8);
    for (unsigned i = 0; i < 8; ++i) {
        digest[4 * i] = (uint8_t)(s->h[i] >> 24);
        digest[4 * i + 1] = (uint8_t)(s->h[i] >> 16);
        digest[4 * i + 2] = (uint8_t)(s->h[i] >> 8);
        digest[4 * i + 3] = (uint8_t)s->h[i];
    }
}

const ywp_crypto_ops_t ywp_crypto_portable = {
    .ctx = NULL,
    .sha256_start = portable_start,
    .sha256_update = portable_update,
    .sha256_finish = portable_finish,
    .equal = NULL,
};

bool ywp_ct_equal(const uint8_t *a, const uint8_t *b, size_t len)
{
    volatile uint8_t diff = 0;
    for (size_t i = 0; i < len; ++i) diff = (uint8_t)(diff | (a[i] ^ b[i]));
    return diff == 0;
}

void ywp_hmac_init(ywp_hmac_t *mac, const ywp_crypto_ops_t *ops,
                   const uint8_t *key, size_t key_len)
{
    uint8_t block[YWP_SHA256_BLOCK] = {0};
    mac->ops = ops;
    mac->used = 0;
    if (key_len > YWP_SHA256_BLOCK) {
        ops->sha256_start(ops->ctx, mac->state);
        ops->sha256_update(ops->ctx, mac->state, key, key_len);
        ops->sha256_finish(ops->ctx, mac->state, block);
    } else if (key_len) {
        memcpy(block, key, key_len);
    }
    for (unsigned i = 0; i < YWP_SHA256_BLOCK; ++i) {
        mac->ipad[i] = (uint8_t)(block[i] ^ 0x36u);
        mac->opad[i] = (uint8_t)(block[i] ^ 0x5cu);
    }
}

void ywp_hmac_start(ywp_hmac_t *mac)
{
    const ywp_crypto_ops_t *ops = mac->ops;
    ops->sha256_start(ops->ctx, mac->state);
    ops->sha256_update(ops->ctx, mac->state, mac->ipad, YWP_SHA256_BLOCK);
    mac->used = 0;
}

/* Whole blocks go to the platform hash: a hardware engine pays per call. */
void ywp_hmac_update(ywp_hmac_t *mac, const void *data, size_t len)
{
    const ywp_crypto_ops_t *ops = mac->ops;
    const uint8_t *p = data;
    if (mac->used) {
        size_t take = YWP_SHA256_BLOCK - mac->used < len ? YWP_SHA256_BLOCK - mac->used : len;
        memcpy(mac->buf + mac->used, p, take);
        mac->used += take;
        p += take;
        len -= take;
        if (mac->used < YWP_SHA256_BLOCK) return;
        ops->sha256_update(ops->ctx, mac->state, mac->buf, YWP_SHA256_BLOCK);
        mac->used = 0;
    }
    size_t whole = len - len % YWP_SHA256_BLOCK;
    if (whole) ops->sha256_update(ops->ctx, mac->state, p, whole);
    if (len - whole) memcpy(mac->buf, p + whole, len - whole);
    mac->used = len - whole;
}

void ywp_hmac_finish(ywp_hmac_t *mac, uint8_t out[YWP_SHA256_LEN])
{
    const ywp_crypto_ops_t *ops = mac->ops;
    uint8_t inner[YWP_SHA256_LEN];
    if (mac->used) ops->sha256_update(ops->ctx, mac->state, mac->buf, mac->used);
    mac->used = 0;
    ops->sha256_finish(ops->ctx, mac->state, inner);
    ops->sha256_start(ops->ctx, mac->state);
    ops->sha256_update(ops->ctx, mac->state, mac->opad, YWP_SHA256_BLOCK);
    ops->sha256_update(ops->ctx, mac->state, inner, YWP_SHA256_LEN);
    ops->sha256_finish(ops->ctx, mac->state, out);
}

bool ywp_hmac_equal(const ywp_hmac_t *mac, const uint8_t *a, const uint8_t *b, size_t len)
{
    const ywp_crypto_ops_t *ops = mac->ops;
    return ops->equal ? ops->equal(ops->ctx, a, b, len) : ywp_ct_equal(a, b, len);
}
