/* ADR 0022 control-socket schema (see control.h; reference:
 * src/yoke/usb/control.py).
 *
 * Requests are parsed strictly and schema-first: the parser reads exactly
 * the two request shapes and refuses a line at the first byte that cannot
 * belong to one. There is no general JSON parser and no Unicode decoding,
 * so little untrusted-input code. A request accepted here is accepted by
 * control.parse_request(control.decode(line)) with the same route.
 * Python accepts more, never anything the agent sends (control.encode):
 *  - Only ASCII is read. Every field of an acceptable request is ASCII
 *    after validation, so Python refuses non-ASCII text too, except a
 *    UTF-8 BOM and the UTF-16/32 encodings json.loads also detects.
 *  - "schema" must be the integer literal 1; Python's `!= 1` also takes
 *    true, 1.0 and 1e0.
 *  - Duplicate keys are refused; json.loads keeps the last.
 *  - Scoped IPv6 hosts are refused (native/yusb/src/target.c).
 * tests/native_usb/test_native_control.py holds the decisions to Python's,
 * with exactly these exceptions. Refusals carry a category (control.h),
 * not control.py's message.
 *
 * Replies are control.encode's bytes exactly, except "updated" (six fixed
 * decimals, 0.0 when not finite, so a status always parses) and detail,
 * cut to 500 bytes on a character boundary where worker._set keeps 500
 * characters.
 * Pure: no I/O and no allocation.
 */
#include <stdio.h>
#include <string.h>
#include "control.h"

#define DETAIL_MAX 500u
#define ERROR_CHARS_MAX 200u

/* --- requests --------------------------------------------------------------- */

typedef struct {
    const uint8_t *s;
    size_t n;
    size_t i;
} cur_t;

static bool is_digit(uint8_t c)
{
    return c >= '0' && c <= '9';
}

static void skip_ws(cur_t *c)
{
    while (c->i < c->n && (c->s[c->i] == ' ' || c->s[c->i] == '\t' || c->s[c->i] == '\n' ||
                           c->s[c->i] == '\r'))
        ++c->i;
}

/* After whitespace, the byte `ch`: consumed. */
static bool eat(cur_t *c, char ch)
{
    skip_ws(c);
    if (c->i >= c->n || c->s[c->i] != (uint8_t)ch) return false;
    ++c->i;
    return true;
}

static bool eat_null(cur_t *c)
{
    skip_ws(c);
    if (c->n - c->i < 4 || memcmp(c->s + c->i, "null", 4)) return false;
    c->i += 4;
    return true;
}

static int hex_value(uint8_t c)
{
    if (is_digit(c)) return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

/* A JSON string of ASCII text without NUL, decoded into buf (NUL
 * terminated). false: not a string, malformed, a code point outside
 * 1..127, or longer than cap - 1 characters. */
static bool get_string(cur_t *c, char *buf, size_t cap, size_t *len)
{
    if (!eat(c, '"')) return false;
    size_t k = 0;
    while (c->i < c->n) {
        uint32_t cp = c->s[c->i++];
        if (cp == '"') {
            buf[k] = '\0';
            *len = k;
            return true;
        }
        if (cp < 0x20 || cp >= 0x80) return false;
        if (cp == '\\') {
            if (c->i >= c->n) return false;
            uint8_t e = c->s[c->i++];
            switch (e) {
            case '"': case '\\': case '/': cp = e; break;
            case 'b': cp = '\b'; break;
            case 'f': cp = '\f'; break;
            case 'n': cp = '\n'; break;
            case 'r': cp = '\r'; break;
            case 't': cp = '\t'; break;
            case 'u':
                if (c->n - c->i < 4) return false;
                cp = 0;
                for (int d = 0; d < 4; ++d) {
                    int v = hex_value(c->s[c->i++]);
                    if (v < 0) return false;
                    cp = cp << 4 | (uint32_t)v;
                }
                break;
            default: return false;
            }
        }
        if (!cp || cp >= 0x80 || k + 1 >= cap) return false;
        buf[k++] = (char)cp;
    }
    return false;
}

/* A JSON integer literal in [min, max] (min >= 1). false for a sign, a
 * fraction or an exponent (a float to Python), or out of range. */
static bool get_integer(cur_t *c, uint64_t min, uint64_t max, uint64_t *out)
{
    skip_ws(c);
    if (c->i >= c->n || c->s[c->i] < '1' || c->s[c->i] > '9') return false;
    uint64_t v = 0;
    while (c->i < c->n && is_digit(c->s[c->i])) {
        uint64_t d = (uint64_t)(c->s[c->i++] - '0');
        if (v > (UINT64_MAX - d) / 10) return false;
        v = v * 10 + d;
    }
    if (c->i < c->n && (c->s[c->i] == '.' || c->s[c->i] == 'e' || c->s[c->i] == 'E')) return false;
    *out = v;
    return v >= min && v <= max;
}

/* Index of a known key, or -1. Keys longer than the names are unknown. */
static int get_key(cur_t *c, const char *const *names, int count, yctl_error_t *why)
{
    char key[16];
    size_t len;
    if (!get_string(c, key, sizeof(key), &len)) {
        *why = YCTL_E_SYNTAX;
        return -1;
    }
    if (!eat(c, ':')) {
        *why = YCTL_E_SYNTAX;
        return -1;
    }
    for (int k = 0; k < count; ++k)
        if (!strcmp(key, names[k])) return k;
    *why = YCTL_E_REQUEST;
    return -1;
}

/* control.Target.from_json */
static yctl_error_t parse_target(cur_t *c, yusb_target_t *t)
{
    static const char *const names[] = {"host", "port", "busid", "source"};
    enum { HOST, PORT, BUSID, SOURCE };
    bool seen[4] = {false, false, false, false};
    char text[YUSB_HOST_MAX + 1];
    size_t len;
    uint64_t port;
    if (!eat(c, '{')) return YCTL_E_TARGET;
    do {
        yctl_error_t why;
        int k = get_key(c, names, 4, &why);
        if (k < 0) return why == YCTL_E_REQUEST ? YCTL_E_TARGET : why;
        if (seen[k]) return YCTL_E_TARGET;
        seen[k] = true;
        switch (k) {
        case HOST:
            if (!get_string(c, text, sizeof(text), &len) || yusb_validate_host(text, len, t->host))
                return YCTL_E_HOST;
            break;
        case PORT:
            if (!get_integer(c, 1, 65535, &port)) return YCTL_E_PORT;
            t->port = (uint16_t)port;
            break;
        case BUSID:
            if (!get_string(c, text, sizeof(t->busid), &len) || yusb_validate_busid(text, len))
                return YCTL_E_BUSID;
            memcpy(t->busid, text, len + 1);
            break;
        default:
            if (eat_null(c)) break;
            if (!get_string(c, text, sizeof(t->source), &len) || yusb_validate_ipv4(text, len, t->source))
                return YCTL_E_SOURCE;
        }
    } while (eat(c, ','));
    if (!eat(c, '}')) return YCTL_E_SYNTAX;
    return seen[HOST] && seen[PORT] && seen[BUSID] && seen[SOURCE] ? YCTL_E_NONE : YCTL_E_TARGET;
}

static yctl_error_t parse(cur_t *c, yctl_request_t *r)
{
    static const char *const names[] = {"schema", "op", "generation", "target"};
    enum { SCHEMA, OP, GENERATION, TARGET };
    enum { NO_OP, STATUS, ROUTE } op = NO_OP;
    bool seen[4] = {false, false, false, false};
    uint64_t value;
    char text[8];
    size_t len;
    if (!eat(c, '{')) return YCTL_E_SYNTAX;
    if (!eat(c, '}')) {
        do {
            yctl_error_t why;
            int k = get_key(c, names, 4, &why);
            if (k < 0) return why;
            if (seen[k]) return YCTL_E_REQUEST;
            seen[k] = true;
            switch (k) {
            case SCHEMA:
                if (!get_integer(c, YCTL_SCHEMA, YCTL_SCHEMA, &value)) return YCTL_E_SCHEMA;
                break;
            case OP:
                if (!get_string(c, text, sizeof(text), &len)) return YCTL_E_REQUEST;
                op = !strcmp(text, "status") ? STATUS : !strcmp(text, "route") ? ROUTE : NO_OP;
                if (op == NO_OP) return YCTL_E_REQUEST;
                break;
            case GENERATION:
                if (!get_integer(c, 1, INT64_MAX, &r->generation)) return YCTL_E_GENERATION;
                break;
            default:
                r->has_target = !eat_null(c);
                if (r->has_target) {
                    yctl_error_t err = parse_target(c, &r->target);
                    if (err) return err;
                }
            }
        } while (eat(c, ','));
        if (!eat(c, '}')) return YCTL_E_SYNTAX;
    }
    skip_ws(c);
    if (c->i != c->n) return YCTL_E_SYNTAX;
    if (!seen[SCHEMA]) return YCTL_E_SCHEMA;
    if (op == STATUS && !seen[GENERATION] && !seen[TARGET]) return YCTL_E_NONE;
    if (op != ROUTE || !seen[GENERATION] || !seen[TARGET]) return YCTL_E_REQUEST;
    r->route = true;
    return YCTL_E_NONE;
}

int yctl_parse_request(const char *line, size_t len, yctl_request_t *out, yctl_error_t *why)
{
    yctl_error_t ignored, err = YCTL_E_LINE;
    yctl_request_t r;
    if (!why) why = &ignored;
    memset(&r, 0, sizeof(r));
    /* control.decode */
    if (line && len && len <= YCTL_MAX_MESSAGE && line[len - 1] == '\n') {
        cur_t c = {(const uint8_t *)line, len, 0};
        err = parse(&c, &r);
    }
    *why = err;
    if (out) *out = err ? (yctl_request_t){0} : r;
    return err || !out ? YUSB_ERR_PROTOCOL : YUSB_OK;
}

const char *yctl_error_text(yctl_error_t why)
{
    switch (why) {
    case YCTL_E_NONE: return "";
    case YCTL_E_LINE: return "control message is not one bounded line";
    case YCTL_E_SYNTAX: return "control message is not a well-formed request";
    case YCTL_E_SCHEMA: return "unknown request schema";
    case YCTL_E_REQUEST: return "unknown control request";
    case YCTL_E_GENERATION: return "route generation must be a positive integer";
    case YCTL_E_TARGET: return "target needs exactly host, port, busid and source";
    case YCTL_E_HOST: return "bad host";
    case YCTL_E_PORT: return "bad port";
    case YCTL_E_BUSID: return "bad busid";
    case YCTL_E_SOURCE: return "bad source IPv4 address";
    }
    return "invalid control request";
}

/* --- bounded output ------------------------------------------------------- */

typedef struct {
    char *buf;
    size_t cap;
    size_t room;  /* usable bytes, excluding the NUL */
    size_t len;
    bool full;    /* a write did not fit; everything after it is dropped */
} out_t;

static void out_init(out_t *o, char *buf, size_t cap)
{
    o->buf = buf;
    o->cap = buf ? cap : 0;
    o->room = buf && cap ? cap - 1 : 0;
    o->len = 0;
    o->full = !buf || !cap;
    if (!o->full) buf[0] = '\0';
}

/* Append as much as fits, never splitting a UTF-8 character. */
static void put(out_t *o, const char *s, size_t n)
{
    if (o->full) return;
    if (n > o->room - o->len) {
        size_t keep = o->room - o->len;
        while (keep && ((unsigned char)s[keep] & 0xc0u) == 0x80u) --keep;
        n = keep;
        o->full = true;
    }
    memcpy(o->buf + o->len, s, n);
    o->len += n;
    o->buf[o->len] = '\0';
}

static void put_str(out_t *o, const char *s)
{
    put(o, s, strlen(s));
}

static void put_char(out_t *o, char c)
{
    put(o, &c, 1);
}

static void put_u64(out_t *o, uint64_t value)
{
    char tmp[20];
    size_t n = sizeof(tmp);
    do {
        tmp[--n] = (char)('0' + value % 10);
        value /= 10;
    } while (value);
    put(o, tmp + n, sizeof(tmp) - n);
}

static void put_hex(out_t *o, uint32_t value, int digits)
{
    static const char hex[] = "0123456789abcdef";
    char tmp[8];
    for (int i = 0; i < digits; ++i) tmp[i] = hex[(value >> (4 * (digits - 1 - i))) & 0xfu];
    put(o, tmp, (size_t)digits);
}

/* One character of s[0..n), n >= 1: true with its code point, or false for
 * an invalid sequence of *len bytes (the maximal subpart, which CPython's
 * errors="replace" turns into one U+FFFD). Surrogates are invalid. */
static bool utf8_char(const uint8_t *s, size_t n, uint32_t *cp, size_t *len)
{
    uint8_t c = s[0], lo = 0x80, hi = 0xbf;
    size_t need;
    *len = 1;
    *cp = c;
    if (c < 0x80) return true;
    if (c >= 0xc2 && c <= 0xdf) {
        need = 1;
    } else if (c >= 0xe0 && c <= 0xef) {
        need = 2;
        lo = c == 0xe0 ? 0xa0 : 0x80;
        hi = c == 0xed ? 0x9f : 0xbf;
    } else if (c >= 0xf0 && c <= 0xf4) {
        need = 3;
        lo = c == 0xf0 ? 0x90 : 0x80;
        hi = c == 0xf4 ? 0x8f : 0xbf;
    } else {
        return false;
    }
    uint32_t v = c & (need == 1 ? 0x1fu : need == 2 ? 0x0fu : 0x07u);
    for (size_t k = 1; k <= need; ++k) {
        if (k >= n) return false;
        uint8_t b = s[k];
        if (b < (k == 1 ? lo : 0x80) || b > (k == 1 ? hi : 0xbf)) return false;
        v = v << 6 | (b & 0x3fu);
        *len = k + 1;
    }
    *cp = v;
    return true;
}

/* --- replies (control.encode) --------------------------------------------- */

/* json.dumps(ensure_ascii=True) of UTF-8 text, invalid sequences decoded
 * as errors="replace" would; at most max_chars characters and max_bytes
 * input bytes, cut on a character boundary. */
static void put_json_text(out_t *o, const char *text, size_t len, size_t max_chars, size_t max_bytes)
{
    put_char(o, '"');
    for (size_t i = 0, chars = 0; i < len && chars < max_chars && !o->full; ++chars) {
        uint32_t cp;
        size_t n;
        if (!utf8_char((const uint8_t *)text + i, len - i, &cp, &n)) cp = 0xfffd;
        if (i + n > max_bytes) break;
        i += n;
        switch (cp) {
        case '"':
            put_str(o, "\\\"");
            break;
        case '\\':
            put_str(o, "\\\\");
            break;
        case '\b':
            put_str(o, "\\b");
            break;
        case '\f':
            put_str(o, "\\f");
            break;
        case '\n':
            put_str(o, "\\n");
            break;
        case '\r':
            put_str(o, "\\r");
            break;
        case '\t':
            put_str(o, "\\t");
            break;
        default:
            if (cp >= 0x20 && cp <= 0x7e) {
                put_char(o, (char)cp);
            } else if (cp < 0x10000) {
                put_str(o, "\\u");
                put_hex(o, cp, 4);
            } else {
                cp -= 0x10000;
                put_str(o, "\\u");
                put_hex(o, 0xd800 | cp >> 10, 4);
                put_str(o, "\\u");
                put_hex(o, 0xdc00 | (cp & 0x3ff), 4);
            }
        }
    }
    put_char(o, '"');
}

static void put_text(out_t *o, const char *text)
{
    text = text ? text : "";
    put_json_text(o, text, strlen(text), SIZE_MAX, SIZE_MAX);
}

/* A fixed-size field that should be NUL terminated. */
static void put_field(out_t *o, const char *field, size_t size)
{
    const char *nul = memchr(field, '\0', size);
    put_json_text(o, field, nul ? (size_t)(nul - field) : size, SIZE_MAX, SIZE_MAX);
}

/* Order of str keys as Python sorts them (code points; invalid UTF-8 as
 * the U+FFFD it is shown as). */
static int key_cmp(const char *a, const char *b)
{
    const uint8_t *pa = (const uint8_t *)(a ? a : ""), *pb = (const uint8_t *)(b ? b : "");
    size_t la = strlen((const char *)pa), lb = strlen((const char *)pb);
    while (la && lb) {
        uint32_t ca, cb;
        size_t na, nb;
        if (!utf8_char(pa, la, &ca, &na)) ca = 0xfffd;
        if (!utf8_char(pb, lb, &cb, &nb)) cb = 0xfffd;
        if (ca != cb) return ca < cb ? -1 : 1;
        pa += na;
        pb += nb;
        la -= na;
        lb -= nb;
    }
    return la ? 1 : lb ? -1 : 0;
}

/* json.dumps(sort_keys=True) of a dict built from (key, value) entries:
 * ascending keys, and the last of equal keys wins as in dict(). Selection
 * instead of a sort keeps it allocation-free; entry counts are tiny. */
static void put_sorted(out_t *o, const void *entries, size_t count, size_t stride, bool counters)
{
    const char *prev = NULL;
    bool first = true;
    put_char(o, '{');
    for (;;) {
        size_t best = SIZE_MAX;
        const char *best_key = NULL;
        for (size_t k = 0; k < count; ++k) {
            const char *key = *(const char *const *)((const char *)entries + k * stride);
            if (!first && key_cmp(key, prev) <= 0) continue;
            if (best == SIZE_MAX || key_cmp(key, best_key) <= 0) {
                best = k;
                best_key = key;
            }
        }
        if (best == SIZE_MAX || o->full) break;
        if (!first) put_char(o, ',');
        first = false;
        prev = best_key;
        put_text(o, best_key);
        put_char(o, ':');
        if (counters) {
            put_u64(o, ((const yctl_counter_t *)entries)[best].value);
        } else {
            put_text(o, ((const yctl_pair_t *)entries)[best].value);
        }
    }
    put_char(o, '}');
}

/* Seconds with six fixed decimals, by hand: printf would follow
 * LC_NUMERIC. Anything not finite or beyond 1e18 prints as 0.0, which
 * keeps the reply acceptable to WorkerStatus.from_json. */
static void put_seconds(out_t *o, double t)
{
    if (!(t > -1e18 && t < 1e18)) t = 0;
    bool negative = t < 0;
    if (negative) t = -t;
    uint64_t whole = (uint64_t)t;
    uint64_t micros = (uint64_t)((t - (double)whole) * 1e6 + 0.5);
    if (micros >= 1000000) {
        ++whole;
        micros -= 1000000;
    }
    if (negative && (whole || micros)) put_char(o, '-');
    put_u64(o, whole);
    char frac[7];
    for (int i = 5; i >= 0; --i) {
        frac[i] = (char)('0' + micros % 10);
        micros /= 10;
    }
    put_char(o, '.');
    put(o, frac, 6);
}

static size_t finish(out_t *o)
{
    put_char(o, '\n');
    if (o->full || o->len > YCTL_MAX_MESSAGE) {
        if (o->cap) o->buf[0] = '\0';
        return 0;
    }
    return o->len;
}

size_t yctl_format_status(const yctl_status_t *st, char *out, size_t cap)
{
    out_t o;
    out_init(&o, out, cap);
    if (!st) {
        o.full = true;
        return finish(&o);
    }
    const char *detail = st->detail ? st->detail : "";
    put_str(&o, "{\"ok\":true,\"status\":{\"counters\":");
    put_sorted(&o, st->counters, st->counters ? st->counter_count : 0, sizeof(yctl_counter_t), true);
    put_str(&o, ",\"detail\":");
    put_json_text(&o, detail, strlen(detail), SIZE_MAX, DETAIL_MAX);
    put_str(&o, ",\"device\":");
    put_sorted(&o, st->device, st->device ? st->device_count : 0, sizeof(yctl_pair_t), false);
    put_str(&o, ",\"generation\":");
    put_u64(&o, st->generation);
    put_str(&o, ",\"link_speed\":");
    put_text(&o, st->link_speed);
    put_str(&o, ",\"reconnects\":");
    put_u64(&o, st->reconnects);
    put_str(&o, ",\"schema\":1,\"state\":");
    put_text(&o, st->state);
    put_str(&o, ",\"target\":");
    if (st->target) {
        const yusb_target_t *t = st->target;
        put_str(&o, "{\"busid\":");
        put_field(&o, t->busid, sizeof(t->busid));
        put_str(&o, ",\"host\":");
        put_field(&o, t->host, sizeof(t->host));
        put_str(&o, ",\"port\":");
        put_u64(&o, t->port);
        put_str(&o, ",\"source\":");
        if (t->source[0]) {
            put_field(&o, t->source, sizeof(t->source));
        } else {
            put_str(&o, "null");
        }
        put_char(&o, '}');
    } else {
        put_str(&o, "null");
    }
    put_str(&o, ",\"udc\":");
    put_text(&o, st->udc);
    put_str(&o, ",\"updated\":");
    put_seconds(&o, st->updated);
    put_str(&o, "}}");
    return finish(&o);
}

size_t yctl_format_error(const char *error, char *out, size_t cap)
{
    out_t o;
    out_init(&o, out, cap);
    error = error ? error : "";
    put_str(&o, "{\"error\":");
    put_json_text(&o, error, strlen(error), ERROR_CHARS_MAX, SIZE_MAX);
    put_str(&o, ",\"ok\":false}");
    return finish(&o);
}
