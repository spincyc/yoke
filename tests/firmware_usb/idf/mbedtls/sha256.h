#pragma once
/* mbedTLS's streaming SHA-256 over native/ywp's portable implementation. */
#include <stddef.h>
#include <stdint.h>
#include "ywp_crypto.h"
typedef struct {
    _Alignas(16) unsigned char state[YWP_SHA256_STATE_MAX];
} mbedtls_sha256_context;
void mbedtls_sha256_init(mbedtls_sha256_context *ctx);
int mbedtls_sha256_starts(mbedtls_sha256_context *ctx, int is224);
int mbedtls_sha256_update(mbedtls_sha256_context *ctx, const unsigned char *data, size_t len);
int mbedtls_sha256_finish(mbedtls_sha256_context *ctx, unsigned char digest[32]);
void mbedtls_sha256_free(mbedtls_sha256_context *ctx);
