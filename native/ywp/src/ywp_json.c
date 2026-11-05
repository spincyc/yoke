/* Strict YWP/1 JSON: parser, accessors and the canonical writer (see
 * ywp_json.h). The parser is iterative with an explicit container stack,
 * so hostile nesting costs no C stack; the writer recurses at most
 * YWP_JSON_MAX_DEPTH levels. */
#include <string.h>
#include "ywp_json.h"
#include "ywp_num.h"

#define NONE YWP_JSON_NONE

/* -- UTF-8 ---------------------------------------------------------------- */

bool ywp_utf8_valid(const uint8_t *s, size_t len)
{
    size_t i = 0;
    while (i < len) {
        uint8_t b = s[i];
        if (b < 0x80) { ++i; continue; }
        size_t need;
        uint8_t lo = 0x80, hi = 0xbf;
        if (b >= 0xc2 && b <= 0xdf) need = 1;
        else if (b >= 0xe0 && b <= 0xef) {
            need = 2;
            if (b == 0xe0) lo = 0xa0;
            if (b == 0xed) hi = 0x9f;             /* no UTF-16 surrogates */
        } else if (b >= 0xf0 && b <= 0xf4) {
            need = 3;
            if (b == 0xf0) lo = 0x90;
            if (b == 0xf4) hi = 0x8f;             /* <= U+10FFFF */
        } else return false;
        if (len - i <= need) return false;
        if (s[i + 1] < lo || s[i + 1] > hi) return false;
        for (size_t k = 2; k <= need; ++k)
            if ((s[i + k] & 0xc0) != 0x80) return false;
        i += need + 1;
    }
    return true;
}

size_t ywp_utf8_clip(const char *text, size_t len, size_t max)
{
    if (len <= max) return len;
    size_t n = max;
    while (n > 0 && ((uint8_t)text[n] & 0xc0) == 0x80) --n;
    return n;
}

/* -- unescaping iterator over a validated string token -------------------- */

typedef struct {
    const uint8_t *p, *end;
    uint8_t pend[4];
    unsigned np, ip;
} str_it_t;

static unsigned hexval(uint8_t c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10u;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10u;
    return 16u;
}

static bool hex4(const uint8_t *p, const uint8_t *end, uint32_t *out)
{
    if (end - p < 4) return false;
    uint32_t v = 0;
    for (unsigned k = 0; k < 4; ++k) {
        unsigned h = hexval(p[k]);
        if (h > 15) return false;
        v = v << 4 | h;
    }
    *out = v;
    return true;
}

static void it_init(str_it_t *it, const ywp_json_t *d, unsigned str)
{
    const ywp_json_tok_t *t = &d->tok[str];
    it->p = d->src + t->off + 1;
    it->end = d->src + t->off + t->len - 1;
    it->np = it->ip = 0;
}

static int it_next(str_it_t *it)
{
    if (it->ip < it->np) return it->pend[it->ip++];
    if (it->p >= it->end) return -1;
    uint8_t b = *it->p++;
    if (b != '\\') return b;
    uint8_t e = *it->p++;
    switch (e) {
    case 'b': return '\b';
    case 'f': return '\f';
    case 'n': return '\n';
    case 'r': return '\r';
    case 't': return '\t';
    case 'u': break;
    default: return e;                            /* " \ / */
    }
    uint32_t cp = 0, low = 0;
    (void)hex4(it->p, it->end, &cp);             /* validated by the parser */
    it->p += 4;
    if (cp >= 0xd800 && cp < 0xdc00) {
        (void)hex4(it->p + 2, it->end, &low);
        it->p += 6;
        cp = 0x10000u + ((cp - 0xd800u) << 10) + (low - 0xdc00u);
    }
    if (cp < 0x80) return (int)cp;
    if (cp < 0x800) {
        it->pend[0] = (uint8_t)(0xc0 | cp >> 6);
        it->pend[1] = (uint8_t)(0x80 | (cp & 0x3f));
        it->np = 2;
    } else if (cp < 0x10000) {
        it->pend[0] = (uint8_t)(0xe0 | cp >> 12);
        it->pend[1] = (uint8_t)(0x80 | ((cp >> 6) & 0x3f));
        it->pend[2] = (uint8_t)(0x80 | (cp & 0x3f));
        it->np = 3;
    } else {
        it->pend[0] = (uint8_t)(0xf0 | cp >> 18);
        it->pend[1] = (uint8_t)(0x80 | ((cp >> 12) & 0x3f));
        it->pend[2] = (uint8_t)(0x80 | ((cp >> 6) & 0x3f));
        it->pend[3] = (uint8_t)(0x80 | (cp & 0x3f));
        it->np = 4;
    }
    it->ip = 1;
    return it->pend[0];
}

/* Unescaped byte order is code point order, as Python sorts str keys. */
static int str_cmp(const ywp_json_t *d, unsigned a, unsigned b)
{
    const ywp_json_tok_t *ta = &d->tok[a], *tb = &d->tok[b];
    if (!ta->escaped && !tb->escaped) {
        size_t la = ta->len - 2u, lb = tb->len - 2u;
        int c = memcmp(d->src + ta->off + 1, d->src + tb->off + 1, la < lb ? la : lb);
        if (c) return c;
        return (la > lb) - (la < lb);
    }
    str_it_t x, y;
    it_init(&x, d, a);
    it_init(&y, d, b);
    for (;;) {
        int cx = it_next(&x), cy = it_next(&y);
        if (cx != cy || cx < 0) return (cx > cy) - (cx < cy);
    }
}

bool ywp_json_str_is(const ywp_json_t *d, unsigned str, const char *text)
{
    const ywp_json_tok_t *t = &d->tok[str];
    size_t n = strlen(text);
    if (!t->escaped)
        return t->len - 2u == n && memcmp(d->src + t->off + 1, text, n) == 0;
    str_it_t it;
    it_init(&it, d, str);
    for (size_t i = 0; i < n; ++i)
        if (it_next(&it) != (uint8_t)text[i]) return false;
    return it_next(&it) < 0;
}

size_t ywp_json_str_len(const ywp_json_t *d, unsigned str)
{
    const ywp_json_tok_t *t = &d->tok[str];
    if (!t->escaped) return t->len - 2u;
    str_it_t it;
    size_t n = 0;
    it_init(&it, d, str);
    while (it_next(&it) >= 0) ++n;
    return n;
}

size_t ywp_json_str_chars(const ywp_json_t *d, unsigned str)
{
    str_it_t it;
    size_t n = 0;
    int c;
    it_init(&it, d, str);
    while ((c = it_next(&it)) >= 0)
        if ((c & 0xc0) != 0x80) ++n;
    return n;
}

size_t ywp_json_str_copy(const ywp_json_t *d, unsigned str, char *out, size_t cap)
{
    size_t n = ywp_json_str_len(d, str);
    if (n >= cap) return SIZE_MAX;
    str_it_t it;
    int c;
    size_t i = 0;
    it_init(&it, d, str);
    while ((c = it_next(&it)) >= 0) out[i++] = (char)c;
    out[i] = 0;
    return n;
}

/* -- parser ---------------------------------------------------------------- */

typedef struct {
    uint16_t tok;
    uint16_t last_key;
} level_t;

static size_t skip_ws(const uint8_t *s, size_t len, size_t i)
{
    while (i < len && (s[i] == ' ' || s[i] == '\t' || s[i] == '\n' || s[i] == '\r')) ++i;
    return i;
}

static int new_tok(ywp_json_t *d, ywp_json_type_t type, size_t off)
{
    if (d->count >= YWP_JSON_MAX_TOKENS) return -1;   /* unreachable: see header */
    ywp_json_tok_t *t = &d->tok[d->count];
    t->off = (uint16_t)off;
    t->len = 0;
    t->type = (uint8_t)type;
    t->escaped = 0;
    t->link = NONE;
    t->end = (uint16_t)(d->count + 1u);
    return d->count++;
}

/* A string token at s[i] == '"'; returns the index past it, or 0. */
static size_t scan_string(ywp_json_t *d, size_t i)
{
    const uint8_t *s = d->src, *end = d->src + d->len;
    size_t j = i + 1;
    bool escaped = false;
    for (;;) {
        if (j >= d->len) return 0;
        uint8_t b = s[j];
        if (b == '"') break;
        if (b < 0x20) return 0;                         /* raw control */
        if (b != '\\') { ++j; continue; }
        escaped = true;
        if (++j >= d->len) return 0;
        uint8_t e = s[j];
        if (e == '"' || e == '\\' || e == '/' || e == 'b' || e == 'f' || e == 'n' ||
            e == 'r' || e == 't') { ++j; continue; }
        uint32_t cp, low;
        if (e != 'u' || !hex4(s + j + 1, end, &cp)) return 0;
        j += 5;
        if (cp >= 0xdc00 && cp < 0xe000) return 0;      /* lone low surrogate */
        if (cp >= 0xd800 && cp < 0xdc00) {              /* needs its low half */
            if (j + 1 >= d->len || s[j] != '\\' || s[j + 1] != 'u' ||
                !hex4(s + j + 2, end, &low) || low < 0xdc00 || low >= 0xe000) return 0;
            j += 6;
        }
    }
    int t = new_tok(d, YWP_JSON_STRING, i);
    if (t < 0) return 0;
    d->tok[t].len = (uint16_t)(j + 1 - i);
    d->tok[t].escaped = escaped;
    return j + 1;
}

static bool digit(uint8_t c)
{
    return c >= '0' && c <= '9';
}

/* A number token at s[i]; returns the index past it, or 0. A '.' or 'e'
 * without digits ends the number and then fails as an unexpected byte,
 * exactly as Python's scanner reports "Extra data". */
static size_t scan_number(ywp_json_t *d, size_t i)
{
    const uint8_t *s = d->src;
    size_t len = d->len, j = i;
    bool is_float = false;
    if (s[j] == '-') ++j;
    if (j >= len) return 0;
    if (s[j] == '0') ++j;
    else if (s[j] >= '1' && s[j] <= '9') while (j < len && digit(s[j])) ++j;
    else return 0;
    if (j + 1 < len && s[j] == '.' && digit(s[j + 1])) {
        j += 2;
        while (j < len && digit(s[j])) ++j;
        is_float = true;
    }
    if (j < len && (s[j] == 'e' || s[j] == 'E')) {
        size_t k = j + 1;
        if (k < len && (s[k] == '+' || s[k] == '-')) ++k;
        if (k < len && digit(s[k])) {
            j = k;
            while (j < len && digit(s[j])) ++j;
            is_float = true;
        }
    }
    /* Finiteness from the digits alone: no conversion at scan (a float is
     * converted at most once, when the canonical writer needs it). */
    if (is_float && !ywp_num_finite((const char *)s + i, j - i)) return 0;
    int t = new_tok(d, is_float ? YWP_JSON_FLOAT : YWP_JSON_INT, i);
    if (t < 0) return 0;
    d->tok[t].len = (uint16_t)(j - i);
    return j;
}

static size_t scan_literal(ywp_json_t *d, size_t i)
{
    static const struct { const char *text; ywp_json_type_t type; } lits[] = {
        {"null", YWP_JSON_NULL}, {"true", YWP_JSON_TRUE}, {"false", YWP_JSON_FALSE}};
    for (unsigned k = 0; k < 3; ++k) {
        size_t n = strlen(lits[k].text);
        if (d->len - i >= n && memcmp(d->src + i, lits[k].text, n) == 0) {
            int t = new_tok(d, lits[k].type, i);
            if (t < 0) return 0;
            d->tok[t].len = (uint16_t)n;
            return i + n;
        }
    }
    return 0;
}

static uint16_t merge(ywp_json_t *d, uint16_t a, uint16_t b)
{
    uint16_t head = NONE, *tail = &head;
    while (a != NONE && b != NONE) {
        uint16_t *take = str_cmp(d, a, b) <= 0 ? &a : &b;
        *tail = *take;
        tail = &d->tok[*take].link;
        *take = d->tok[*take].link;
    }
    *tail = a != NONE ? a : b;
    return head;
}

/* Stable merge sort of a key list: O(k log k) even for hostile objects. */
static uint16_t sort_keys(ywp_json_t *d, uint16_t head)
{
    if (head == NONE || d->tok[head].link == NONE) return head;
    uint16_t slow = head, fast = d->tok[head].link;
    while (fast != NONE && d->tok[fast].link != NONE) {
        slow = d->tok[slow].link;
        fast = d->tok[d->tok[fast].link].link;
    }
    uint16_t second = d->tok[slow].link;
    d->tok[slow].link = NONE;
    return merge(d, sort_keys(d, head), sort_keys(d, second));
}

static bool close_object(ywp_json_t *d, uint16_t obj)
{
    uint16_t head = sort_keys(d, d->tok[obj].link);
    d->tok[obj].link = head;
    for (uint16_t k = head; k != NONE && d->tok[k].link != NONE; k = d->tok[k].link)
        if (str_cmp(d, k, d->tok[k].link) == 0) return false;   /* duplicate key */
    return true;
}

/* A member key and its ':' at s[i]; returns the index of the value. */
static size_t scan_key(ywp_json_t *d, level_t *lv, size_t i)
{
    i = skip_ws(d->src, d->len, i);
    if (i >= d->len || d->src[i] != '"') return 0;
    uint16_t key = d->count;
    i = scan_string(d, i);
    if (!i) return 0;
    if (lv->last_key == NONE) d->tok[lv->tok].link = key;
    else d->tok[lv->last_key].link = key;
    lv->last_key = key;
    i = skip_ws(d->src, d->len, i);
    if (i >= d->len || d->src[i] != ':') return 0;
    return i + 1;
}

bool ywp_json_parse(ywp_json_t *d, const uint8_t *s, size_t len)
{
    level_t stack[YWP_JSON_MAX_DEPTH];
    unsigned depth = 0;
    d->src = s;
    d->len = len;
    d->count = 0;
    d->depth = 0;
    if (len > YWP_MAX_FRAME || !ywp_utf8_valid(s, len)) return false;
    size_t i = 0;
    for (;;) {
        /* A value starts at i. */
        i = skip_ws(s, len, i);
        if (i >= len) return false;
        uint8_t c = s[i];
        if (c == '{' || c == '[') {
            if (depth == YWP_JSON_MAX_DEPTH) return false;
            int t = new_tok(d, c == '{' ? YWP_JSON_OBJECT : YWP_JSON_ARRAY, i);
            if (t < 0) return false;
            stack[depth].tok = (uint16_t)t;
            stack[depth].last_key = NONE;
            if (++depth > d->depth) d->depth = (uint8_t)depth;
            size_t j = skip_ws(s, len, i + 1);
            if (j < len && s[j] == (c == '{' ? '}' : ']')) {
                i = j;                                  /* empty: close below */
            } else {
                i = c == '{' ? scan_key(d, &stack[depth - 1], i + 1) : i + 1;
                if (!i) return false;
                continue;
            }
        } else {
            if (c == '"') i = scan_string(d, i);
            else if (c == '-' || digit(c)) i = scan_number(d, i);
            else i = scan_literal(d, i);
            if (!i) return false;
            i = skip_ws(s, len, i);
        }
        /* After a value: separators and closes until the next value. */
        for (;;) {
            if (depth == 0) {
                if (skip_ws(s, len, i) != len) return false;
                return true;
            }
            level_t *lv = &stack[depth - 1];
            ywp_json_tok_t *top = &d->tok[lv->tok];
            uint8_t closer = top->type == YWP_JSON_OBJECT ? '}' : ']';
            i = skip_ws(s, len, i);
            if (i >= len) return false;
            if (s[i] == ',') {
                i = top->type == YWP_JSON_OBJECT ? scan_key(d, lv, i + 1) : i + 1;
                if (!i) return false;
                break;
            }
            if (s[i] != closer) return false;
            top->len = (uint16_t)(i + 1 - top->off);
            top->end = d->count;
            if (top->type == YWP_JSON_OBJECT && !close_object(d, lv->tok)) return false;
            --depth;
            ++i;
        }
    }
}

/* -- accessors --------------------------------------------------------------- */

unsigned ywp_json_get(const ywp_json_t *d, unsigned obj, const char *key)
{
    if (d->tok[obj].type != YWP_JSON_OBJECT) return NONE;
    for (unsigned k = obj + 1; k < d->tok[obj].end; k = d->tok[k + 1].end)
        if (ywp_json_str_is(d, k, key)) return k + 1;
    return NONE;
}

unsigned ywp_json_count(const ywp_json_t *d, unsigned c)
{
    unsigned n = 0, step = d->tok[c].type == YWP_JSON_OBJECT ? 1u : 0u;
    if (d->tok[c].type != YWP_JSON_OBJECT && d->tok[c].type != YWP_JSON_ARRAY) return 0;
    for (unsigned k = c + 1; k < d->tok[c].end; k = d->tok[k + step].end) ++n;
    return n;
}

unsigned ywp_json_at(const ywp_json_t *d, unsigned arr, unsigned index)
{
    if (d->tok[arr].type != YWP_JSON_ARRAY) return NONE;
    for (unsigned k = arr + 1; k < d->tok[arr].end; k = d->tok[k].end)
        if (index-- == 0) return k;
    return NONE;
}

bool ywp_json_uint(const ywp_json_t *d, unsigned num, uint64_t max, uint64_t *out)
{
    const ywp_json_tok_t *t = &d->tok[num];
    if (t->type != YWP_JSON_INT) return false;
    const uint8_t *p = d->src + t->off, *end = p + t->len;
    if (*p == '-') return t->len == 2 && p[1] == '0' ? (*out = 0, true) : false;
    uint64_t v = 0;
    for (; p < end; ++p) {
        unsigned dgt = (unsigned)(*p - '0');
        if (dgt > max || v > (max - dgt) / 10u) return false;
        v = v * 10u + dgt;
    }
    *out = v;
    return true;
}

bool ywp_json_number(const ywp_json_t *d, unsigned num, double *out)
{
    const ywp_json_tok_t *t = &d->tok[num];
    if (t->type != YWP_JSON_INT && t->type != YWP_JSON_FLOAT) return false;
    if (!ywp_num_parse((const char *)d->src + t->off, t->len, out)) return false;
    if (t->type == YWP_JSON_INT && *out == 0) *out = 0.0;   /* float(int("-0")) is +0.0 */
    return true;
}

/* -- output -------------------------------------------------------------------- */

void ywp_out_buffer(ywp_out_t *o, uint8_t *buf, size_t cap)
{
    o->buf = buf;
    o->cap = cap;
    o->len = 0;
    o->flush = NULL;
    o->ctx = NULL;
    o->total = 0;
    o->overflow = false;
}

void ywp_out_stream(ywp_out_t *o, uint8_t *buf, size_t cap,
                    void (*flush)(void *ctx, const uint8_t *data, size_t len), void *ctx)
{
    ywp_out_buffer(o, buf, cap);
    o->flush = flush;
    o->ctx = ctx;
}

void ywp_out_raw(ywp_out_t *o, const void *data, size_t n)
{
    const uint8_t *p = data;
    o->total += n;
    while (n) {
        if (o->len == o->cap) {
            if (!o->flush || !o->cap) { o->overflow = true; return; }
            o->flush(o->ctx, o->buf, o->len);
            o->len = 0;
        }
        size_t take = o->cap - o->len < n ? o->cap - o->len : n;
        memcpy(o->buf + o->len, p, take);
        o->len += take;
        p += take;
        n -= take;
    }
}

void ywp_out_text(ywp_out_t *o, const char *text)
{
    ywp_out_raw(o, text, strlen(text));
}

bool ywp_out_finish(ywp_out_t *o)
{
    if (o->flush && o->len) o->flush(o->ctx, o->buf, o->len);
    if (o->flush) o->len = 0;
    return !o->overflow;
}

/* Python's json escaping of one unescaped byte (ensure_ascii=False). */
static void out_escaped(ywp_out_t *o, uint8_t b)
{
    static const char hex[] = "0123456789abcdef";
    char esc[6] = {'\\', 0, '0', '0', 0, 0};
    size_t n = 2;
    switch (b) {
    case '"': esc[1] = '"'; break;
    case '\\': esc[1] = '\\'; break;
    case '\b': esc[1] = 'b'; break;
    case '\f': esc[1] = 'f'; break;
    case '\n': esc[1] = 'n'; break;
    case '\r': esc[1] = 'r'; break;
    case '\t': esc[1] = 't'; break;
    default:
        if (b >= 0x20) { ywp_out_raw(o, &b, 1); return; }
        esc[1] = 'u';
        esc[4] = hex[b >> 4];
        esc[5] = hex[b & 15];
        n = 6;
    }
    ywp_out_raw(o, esc, n);
}

void ywp_out_str(ywp_out_t *o, const char *utf8, size_t len)
{
    const uint8_t *p = (const uint8_t *)utf8;
    size_t run = 0;
    ywp_out_raw(o, "\"", 1);
    for (size_t i = 0; i < len; ++i) {
        if (p[i] >= 0x20 && p[i] != '"' && p[i] != '\\') continue;
        ywp_out_raw(o, p + run, i - run);
        out_escaped(o, p[i]);
        run = i + 1;
    }
    ywp_out_raw(o, p + run, len - run);
    ywp_out_raw(o, "\"", 1);
}

void ywp_out_uint(ywp_out_t *o, uint64_t v)
{
    char digits[20];
    size_t n = 0;
    do { digits[sizeof(digits) - 1 - n++] = (char)('0' + v % 10u); v /= 10u; } while (v);
    ywp_out_raw(o, digits + sizeof(digits) - n, n);
}

void ywp_out_float(ywp_out_t *o, double v)
{
    char text[YWP_REPR_MAX];
    ywp_out_raw(o, text, ywp_num_repr(v, text));
}

/* -- canonical writer ------------------------------------------------------------ */

static void canon_str(const ywp_json_t *d, unsigned str, ywp_out_t *o)
{
    const ywp_json_tok_t *t = &d->tok[str];
    if (!t->escaped) {                  /* raw content never needs escaping */
        ywp_out_raw(o, d->src + t->off, t->len);
        return;
    }
    str_it_t it;
    int c;
    it_init(&it, d, str);
    ywp_out_raw(o, "\"", 1);
    while ((c = it_next(&it)) >= 0) out_escaped(o, (uint8_t)c);
    ywp_out_raw(o, "\"", 1);
}

static void canon_scalar(const ywp_json_t *d, unsigned i, ywp_out_t *o)
{
    const ywp_json_tok_t *t = &d->tok[i];
    const uint8_t *src = d->src + t->off;
    char text[YWP_REPR_MAX];
    switch ((ywp_json_type_t)t->type) {
    case YWP_JSON_NULL: ywp_out_raw(o, "null", 4); return;
    case YWP_JSON_TRUE: ywp_out_raw(o, "true", 4); return;
    case YWP_JSON_FALSE: ywp_out_raw(o, "false", 5); return;
    case YWP_JSON_INT:                          /* int("-0") == 0 */
        if (t->len == 2 && src[0] == '-' && src[1] == '0') ywp_out_raw(o, "0", 1);
        else ywp_out_raw(o, src, t->len);
        return;
    case YWP_JSON_FLOAT:                        /* repr(float(literal)) */
        ywp_out_raw(o, text, ywp_num_canonical((const char *)src, t->len, text));
        return;
    case YWP_JSON_STRING: canon_str(d, i, o); return;
    default: return;                            /* containers: the caller */
    }
}

/* Iterative, so hostile nesting costs a fixed few bytes of stack per level
 * instead of a C frame; containers emit their members in key order. */
void ywp_json_canonical(const ywp_json_t *d, unsigned root, unsigned skip, ywp_out_t *o)
{
    struct {
        uint16_t container, next;               /* next element or key, NONE */
        bool first;
    } stack[YWP_JSON_MAX_DEPTH];
    unsigned depth = 0, i = root;
    for (;;) {
        const ywp_json_tok_t *t = &d->tok[i];
        if (t->type == YWP_JSON_ARRAY || t->type == YWP_JSON_OBJECT) {
            bool array = t->type == YWP_JSON_ARRAY;
            ywp_out_raw(o, array ? "[" : "{", 1);
            stack[depth].container = (uint16_t)i;
            stack[depth].next = array ? (i + 1u < t->end ? (uint16_t)(i + 1u) : NONE) : t->link;
            stack[depth].first = true;
            ++depth;
        } else {
            canon_scalar(d, i, o);
        }
        for (;;) {                               /* the next value to emit */
            if (depth == 0) return;
            unsigned c = stack[depth - 1].container, k = stack[depth - 1].next;
            const ywp_json_tok_t *ct = &d->tok[c];
            bool array = ct->type == YWP_JSON_ARRAY;
            if (!array && c == root)             /* the omitted member (legacy sig) */
                while (k != NONE && k == skip) k = d->tok[k].link;
            if (k == NONE) {
                ywp_out_raw(o, array ? "]" : "}", 1);
                --depth;
                continue;
            }
            if (!stack[depth - 1].first) ywp_out_raw(o, ",", 1);
            stack[depth - 1].first = false;
            if (array) {
                stack[depth - 1].next = d->tok[k].end < ct->end ? d->tok[k].end : NONE;
                i = k;
            } else {
                stack[depth - 1].next = d->tok[k].link;
                canon_str(d, k, o);
                ywp_out_raw(o, ":", 1);
                i = k + 1;
            }
            break;
        }
    }
}
