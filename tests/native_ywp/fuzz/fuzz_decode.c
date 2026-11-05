/* libFuzzer target: frame decode and the canonical writer.
 *
 * Every input is decoded with a site key and in open mode. Accepted frames
 * must re-serialize to canonical bytes that parse again, are a fixed point
 * of canonicalization, and (integer-only bodies) re-encode to a frame that
 * decodes to the same fields. Agreement with the Python reference is
 * checked separately over the corpus this target grows
 * (tests/native_ywp/test_ywp_fuzz.py). HOST ONLY. */
#include <stdlib.h>
#include <string.h>
#include "ywp_frame.h"

static const uint8_t KEY[] = "fuzz-site-key-0123456789abcdef!!";
static ywp_codec_t keyed, open_mode, scratch;
static ywp_json_t doc;
static uint8_t first[8 * YWP_MAX_FRAME], second[8 * YWP_MAX_FRAME];

#define ASSERT(x) do { if (!(x)) abort(); } while (0)

static size_t canonical(const ywp_json_t *d, unsigned i, uint8_t *out, size_t cap)
{
    ywp_out_t o;
    ywp_out_buffer(&o, out, cap);
    ywp_json_canonical(d, i, YWP_JSON_NONE, &o);
    ASSERT(ywp_out_finish(&o));
    return o.len;
}

static void check_frame(ywp_codec_t *codec, const ywp_frame_t *f)
{
    size_t n = canonical(&codec->json, f->body, first, sizeof(first));
    bool floats = false;
    for (unsigned i = f->body; i < codec->json.tok[f->body].end; ++i)
        floats |= codec->json.tok[i].type == YWP_JSON_FLOAT;
    ASSERT(ywp_json_parse(&doc, first, n < YWP_MAX_FRAME ? n : 0) || n >= YWP_MAX_FRAME);
    if (n < YWP_MAX_FRAME) ASSERT(canonical(&doc, 0, second, sizeof(second)) == n &&
                                  memcmp(first, second, n) == 0);
    uint8_t raw[YWP_MAX_FRAME];
    ywp_frame_t again;
    size_t len = ywp_encode(&scratch, f->type, f->origin, f->seq, f->ts, first,
                            n < YWP_MAX_FRAME ? n : 0, raw);
    if (!len) {                     /* refused only for floats or the budget */
        ASSERT(floats || n + ywp_frame_overhead(f->origin) > YWP_MAX_FRAME);
        return;
    }
    ASSERT(ywp_decode(&scratch, raw, len, &again) == YWP_OK);
    ASSERT(again.type == f->type && again.seq == f->seq && strcmp(again.origin, f->origin) == 0);
    ASSERT(memcmp(&again.ts, &f->ts, sizeof(double)) == 0 || (again.ts == 0 && f->ts == 0));
}

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size);

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)
{
    static bool ready;
    if (!ready) {
        ywp_codec_init(&keyed, &ywp_crypto_portable, KEY, sizeof(KEY) - 1);
        ywp_codec_init(&open_mode, &ywp_crypto_portable, NULL, 0);
        ywp_codec_init(&scratch, &ywp_crypto_portable, KEY, sizeof(KEY) - 1);
        ready = true;
    }
    ywp_frame_t f;
    if (ywp_decode(&keyed, data, size, &f) == YWP_OK) check_frame(&keyed, &f);
    if (ywp_decode(&open_mode, data, size, &f) == YWP_OK) check_frame(&open_mode, &f);
    if (size <= YWP_MAX_FRAME && ywp_json_parse(&doc, data, size)) {
        size_t n = canonical(&doc, 0, first, sizeof(first));
        if (n <= YWP_MAX_FRAME) {
            ASSERT(ywp_json_parse(&doc, first, n));
            ASSERT(canonical(&doc, 0, second, sizeof(second)) == n && memcmp(first, second, n) == 0);
        }
    }
    return 0;
}
