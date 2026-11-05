#pragma once
/* Crypto seam of the YWP/1 core (ADR 0005, PROTOCOL.md Authentication).
 *
 * The platform supplies streaming SHA-256 (mbedTLS, with its hardware
 * engine, on ESP32; the bundled portable implementation on hosts) and a
 * constant-time comparison. The core composes HMAC-SHA256 (RFC 2104) from
 * the hash, so a signed message made of two slices of a datagram, or
 * canonical bytes generated on the fly, is hashed without a copy. */
#include <stdalign.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define YWP_SHA256_LEN 32
#define YWP_SHA256_BLOCK 64
/* Opaque hash state reserved by the core for the platform's context
 * (mbedtls_sha256_context fits); max_align_t aligned. */
#define YWP_SHA256_STATE_MAX 256

typedef struct {
    void *ctx;
    void (*sha256_start)(void *ctx, void *state);
    void (*sha256_update)(void *ctx, void *state, const uint8_t *data, size_t len);
    void (*sha256_finish)(void *ctx, void *state, uint8_t digest[YWP_SHA256_LEN]);
    /* Constant-time equality, no data-dependent early exit. NULL selects
     * ywp_ct_equal. */
    bool (*equal)(void *ctx, const uint8_t *a, const uint8_t *b, size_t len);
} ywp_crypto_ops_t;

/* Bundled portable SHA-256 (FIPS 180-4); permissive, allocation-free. */
extern const ywp_crypto_ops_t ywp_crypto_portable;

bool ywp_ct_equal(const uint8_t *a, const uint8_t *b, size_t len);

/* HMAC-SHA256 with a key prepared once. Streaming use: start, any number
 * of updates (buffered into whole blocks), finish. */
typedef struct {
    const ywp_crypto_ops_t *ops;
    uint8_t ipad[YWP_SHA256_BLOCK];
    uint8_t opad[YWP_SHA256_BLOCK];
    uint8_t buf[YWP_SHA256_BLOCK];
    size_t used;
    alignas(max_align_t) unsigned char state[YWP_SHA256_STATE_MAX];
} ywp_hmac_t;

void ywp_hmac_init(ywp_hmac_t *mac, const ywp_crypto_ops_t *ops,
                   const uint8_t *key, size_t key_len);
void ywp_hmac_start(ywp_hmac_t *mac);
void ywp_hmac_update(ywp_hmac_t *mac, const void *data, size_t len);
void ywp_hmac_finish(ywp_hmac_t *mac, uint8_t out[YWP_SHA256_LEN]);
/* Constant-time comparison through the configured ops. */
bool ywp_hmac_equal(const ywp_hmac_t *mac, const uint8_t *a, const uint8_t *b,
                    size_t len);
