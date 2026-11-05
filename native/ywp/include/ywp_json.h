#pragma once
/* Strict JSON for YWP/1 (PROTOCOL.md Encoding) with the acceptance rules of
 * the Python reference (`json.loads` of strict UTF-8 plus frames.py checks):
 * RFC 8259 grammar, valid UTF-8, no BOM, unique object keys after
 * unescaping, no NaN/Infinity, no number that rounds to infinity, no lone
 * surrogate escapes, at most 32 nested containers. Inputs are at most
 * YWP_MAX_FRAME bytes, so a parse never runs out of tokens.
 *
 * The parse is a flat token array over the caller's bytes (no copy, no
 * allocation). Objects carry their members in key order as a linked list,
 * which serves duplicate detection and the canonical writer, which emits
 * exactly Python's json.dumps(sort_keys=True, separators=(",", ":"),
 * ensure_ascii=False, allow_nan=False). */
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "ywp_types.h"

#define YWP_JSON_MAX_DEPTH 32
/* Every token but the last of a container is followed by a separator. */
#define YWP_JSON_MAX_TOKENS (YWP_MAX_FRAME / 2 + 1)
#define YWP_JSON_NONE 0xffffu

typedef enum {
    YWP_JSON_NULL = 0,
    YWP_JSON_FALSE,
    YWP_JSON_TRUE,
    YWP_JSON_INT,       /* no fraction or exponent: Python int */
    YWP_JSON_FLOAT,     /* finite Python float */
    YWP_JSON_STRING,
    YWP_JSON_ARRAY,
    YWP_JSON_OBJECT
} ywp_json_type_t;

typedef struct {
    uint16_t off, len;  /* source span; strings include their quotes */
    uint16_t end;       /* index one past this value's subtree */
    uint16_t link;      /* object: first key in key order; key: next key */
    uint8_t type;       /* ywp_json_type_t */
    uint8_t escaped;    /* string contains a backslash escape */
} ywp_json_tok_t;

typedef struct {
    const uint8_t *src;
    size_t len;
    uint16_t count;
    uint8_t depth;      /* deepest container nesting (top level = 1) */
    ywp_json_tok_t tok[YWP_JSON_MAX_TOKENS];
} ywp_json_t;

/* Parse len (<= YWP_MAX_FRAME) bytes. Token 0 is the top-level value.
 * Returns false for any input the reference rejects. */
bool ywp_json_parse(ywp_json_t *doc, const uint8_t *src, size_t len);

static inline ywp_json_type_t ywp_json_type(const ywp_json_t *doc, unsigned i)
{
    return (ywp_json_type_t)doc->tok[i].type;
}

/* Value of the member whose unescaped key equals `key`, or YWP_JSON_NONE. */
unsigned ywp_json_get(const ywp_json_t *doc, unsigned object, const char *key);
/* Members of an object, elements of an array. */
unsigned ywp_json_count(const ywp_json_t *doc, unsigned container);
/* Element `index` of an array, or YWP_JSON_NONE. */
unsigned ywp_json_at(const ywp_json_t *doc, unsigned array, unsigned index);

/* Strings (tokens of type YWP_JSON_STRING, keys included). */
bool ywp_json_str_is(const ywp_json_t *doc, unsigned str, const char *text);
/* Unescaped UTF-8 length in bytes, and in code points. */
size_t ywp_json_str_len(const ywp_json_t *doc, unsigned str);
size_t ywp_json_str_chars(const ywp_json_t *doc, unsigned str);
/* Copy the unescaped value plus NUL when it fits in cap; returns its
 * length, or SIZE_MAX (nothing written) when it does not fit. */
size_t ywp_json_str_copy(const ywp_json_t *doc, unsigned str, char *out, size_t cap);

/* An integer token (not bool, not float) with 0 <= value <= max. */
bool ywp_json_uint(const ywp_json_t *doc, unsigned num, uint64_t max, uint64_t *out);
/* Python float() of a number token (int or float, so an integer -0 is
 * +0.0); false on overflow. */
bool ywp_json_number(const ywp_json_t *doc, unsigned num, double *out);

/* Buffered byte sink. With flush == NULL, buf is the destination and an
 * overrun sets `overflow`; otherwise full buffers go to flush(ctx, ...). */
typedef struct {
    uint8_t *buf;
    size_t cap, len;
    void (*flush)(void *ctx, const uint8_t *data, size_t len);
    void *ctx;
    size_t total;
    bool overflow;
} ywp_out_t;

void ywp_out_buffer(ywp_out_t *out, uint8_t *buf, size_t cap);
void ywp_out_stream(ywp_out_t *out, uint8_t *buf, size_t cap,
                    void (*flush)(void *ctx, const uint8_t *data, size_t len), void *ctx);
void ywp_out_raw(ywp_out_t *out, const void *data, size_t len);
void ywp_out_text(ywp_out_t *out, const char *text);
/* A JSON string with the reference escaping: `"` `\` and C0 controls
 * (\b \f \n \r \t, else \u00xx); everything else verbatim UTF-8. */
void ywp_out_str(ywp_out_t *out, const char *utf8, size_t len);
void ywp_out_uint(ywp_out_t *out, uint64_t value);
void ywp_out_float(ywp_out_t *out, double value);   /* Python repr() */
/* Pass buffered bytes to flush; returns false if the sink overflowed. */
bool ywp_out_finish(ywp_out_t *out);

/* Canonical bytes of value `i`. When `skip` names a member key of that
 * object, the member is omitted (the legacy signed message). */
void ywp_json_canonical(const ywp_json_t *doc, unsigned i, unsigned skip, ywp_out_t *out);

/* Strict UTF-8 (Unicode Table 3-7): no overlongs, surrogates or values
 * above U+10FFFF. */
bool ywp_utf8_valid(const uint8_t *data, size_t len);
/* Longest prefix of valid UTF-8 `text` of at most `max` bytes that ends on
 * a character boundary. */
size_t ywp_utf8_clip(const char *text, size_t len, size_t max);
