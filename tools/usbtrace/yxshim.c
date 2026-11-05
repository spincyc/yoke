/* ctypes shim over the YX encoder and decoder (ADR 0028) for replay.py.
 *
 * Benchmark tooling, not product code: it owns the yusb_yx_* state on the
 * heap so Python needs no struct layout. Built at runtime by yxenc.py with
 * native/yusb/src/yx.c and native/third_party/lz4/lz4.c.
 */
#include <stdlib.h>

#include "yusb_yx.h"

static yusb_yx_params_t shim_params(unsigned levers, unsigned cache_blocks)
{
    yusb_yx_params_t params = {
        .version = YUSB_YX_VERSION,
        .levers = (uint8_t)levers,
        .cache_blocks = (uint16_t)cache_blocks,
    };
    return params;
}

/* Returns NULL and stores the yusb error in *err on failure. */
void *shim_enc_new(unsigned levers, unsigned cache_blocks, int *err)
{
    yusb_yx_enc_t *e = calloc(1, sizeof *e);
    yusb_yx_params_t params = shim_params(levers, cache_blocks);
    *err = e ? yusb_yx_enc_init(e, &params) : YUSB_ERR_NOMEM;
    if (*err != YUSB_OK) {
        free(e);
        return NULL;
    }
    return e;
}

void shim_enc_free(void *e)
{
    if (e) {
        yusb_yx_enc_free(e);
        free(e);
    }
}

size_t shim_bound(size_t length)
{
    return yusb_yx_bound(length);
}

size_t shim_encode(void *e, const uint8_t *src, size_t length, uint8_t *out)
{
    return yusb_yx_encode(e, src, length, out);
}

/* out: blocks raw, fill, lz4, ref, raw_bytes, wire_bytes, lz4_tries, lz4_bypass */
void shim_enc_stats(const void *e, uint64_t out[8])
{
    const yusb_yx_stats_t *s = &((const yusb_yx_enc_t *)e)->stats;
    for (int kind = 0; kind < 4; kind++) {
        out[kind] = s->blocks[kind];
    }
    out[4] = s->raw_bytes;
    out[5] = s->wire_bytes;
    out[6] = s->lz4_tries;
    out[7] = s->lz4_bypass;
}

void *shim_dec_new(unsigned levers, unsigned cache_blocks, int *err)
{
    yusb_yx_dec_t *d = calloc(1, sizeof *d);
    yusb_yx_params_t params = shim_params(levers, cache_blocks);
    *err = d ? yusb_yx_dec_init(d, &params) : YUSB_ERR_NOMEM;
    if (*err != YUSB_OK) {
        free(d);
        return NULL;
    }
    return d;
}

void shim_dec_free(void *d)
{
    if (d) {
        yusb_yx_dec_free(d);
        free(d);
    }
}

int shim_decode(void *d, const uint8_t *wire, size_t wire_len, uint8_t *dst, size_t length,
                size_t *consumed)
{
    return yusb_yx_decode(d, wire, wire_len, dst, length, consumed);
}
