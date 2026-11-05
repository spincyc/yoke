/* YWP/1 frame decode and encode (ywp_frame.h; reference: frames.py). */
#include <math.h>
#include <string.h>
#include "ywp_frame.h"

static const char *const ENVELOPE[7] = {"v", "t", "id", "seq", "ts", "body", "sig"};
enum { V, T, ID, SEQ, TS, BODY, SIG };

void ywp_codec_init(ywp_codec_t *c, const ywp_crypto_ops_t *ops,
                    const uint8_t *psk, size_t psk_len)
{
    ywp_hmac_init(&c->mac, ops, psk, psk_len);
    c->keyed = psk_len > 0;
    c->json.count = 0;
}

static void mac_sink(void *ctx, const uint8_t *data, size_t len)
{
    ywp_hmac_update(ctx, data, len);
}

/* HMAC of the canonical bytes of token `value`, member key `skip` omitted. */
static void mac_canonical(ywp_codec_t *c, unsigned value, unsigned skip,
                          uint8_t out[YWP_SHA256_LEN])
{
    uint8_t buf[128];
    ywp_out_t o;
    ywp_hmac_start(&c->mac);
    ywp_out_stream(&o, buf, sizeof(buf), mac_sink, &c->mac);
    ywp_json_canonical(&c->json, value, skip, &o);
    (void)ywp_out_finish(&o);
    ywp_hmac_finish(&c->mac, out);
}

void ywp_frame_fingerprint(ywp_codec_t *c, ywp_frame_t *f)
{
    mac_canonical(c, 0, YWP_JSON_NONE, f->fingerprint);
}

void ywp_codec_value_mac(ywp_codec_t *c, unsigned value, uint8_t out[YWP_SHA256_LEN])
{
    mac_canonical(c, value, YWP_JSON_NONE, out);
}

static bool is_ws(uint8_t b)
{
    return b == ' ' || b == '\t' || b == '\n' || b == '\r';
}

/* HMAC of the transmitted datagram with the top-level sig member excised
 * (PROTOCOL.md Authentication, steps 1 and 2): remove [K, C] when another
 * member follows, C being its terminating ',', else [P, E). */
static void mac_transmitted(ywp_codec_t *c, unsigned key, uint8_t out[YWP_SHA256_LEN])
{
    const ywp_json_t *d = &c->json;
    const uint8_t *s = d->src;
    size_t k = d->tok[key].off;
    size_t e = d->tok[key + 1].off + d->tok[key + 1].len;
    size_t head, tail;
    if (d->tok[key + 1].end < d->tok[0].end) {
        size_t comma = e;
        while (is_ws(s[comma])) ++comma;
        head = k;
        tail = comma + 1;
    } else {
        size_t comma = k;
        while (is_ws(s[comma - 1])) --comma;
        head = comma - 1;
        tail = e;
    }
    ywp_hmac_start(&c->mac);
    ywp_hmac_update(&c->mac, s, head);
    ywp_hmac_update(&c->mac, s + tail, d->len - tail);
    ywp_hmac_finish(&c->mac, out);
}

/* The top-level member key token whose value is `value`. */
static unsigned key_of(unsigned value)
{
    return value - 1;
}

static bool sig_bytes(const char *hex, uint8_t out[YWP_SHA256_LEN])
{
    for (unsigned i = 0; i < 2 * YWP_SHA256_LEN; ++i) {
        char ch = hex[i];
        unsigned v;
        if (ch >= '0' && ch <= '9') v = (unsigned)(ch - '0');
        else if (ch >= 'a' && ch <= 'f') v = (unsigned)(ch - 'a') + 10u;
        else return false;
        if (i & 1u) out[i / 2] = (uint8_t)(out[i / 2] | v);
        else out[i / 2] = (uint8_t)(v << 4);
    }
    return true;
}

ywp_drop_t ywp_decode(ywp_codec_t *c, const uint8_t *raw, size_t len, ywp_frame_t *f)
{
    ywp_json_t *d = &c->json;
    unsigned m[7];
    if (len > YWP_MAX_FRAME) return YWP_DROP_OVERSIZE;
    if (!ywp_json_parse(d, raw, len) || d->tok[0].type != YWP_JSON_OBJECT ||
        ywp_json_count(d, 0) != 7) return YWP_DROP_MALFORMED;
    for (unsigned i = 0; i < 7; ++i)
        if ((m[i] = ywp_json_get(d, 0, ENVELOPE[i])) == YWP_JSON_NONE) return YWP_DROP_MALFORMED;

    uint64_t version, seq;
    if (!ywp_json_uint(d, m[V], UINT64_MAX, &version) || version != YWP_PROTOCOL_VERSION)
        return YWP_DROP_VERSION;
    if (d->tok[m[T]].type != YWP_JSON_STRING) return YWP_DROP_MALFORMED;
    unsigned type = 0;
    while (type < 5 && !ywp_json_str_is(d, m[T], ywp_frame_type_name((ywp_frame_type_t)type)))
        ++type;
    if (type == 5) return YWP_DROP_MALFORMED;
    if (d->tok[m[ID]].type != YWP_JSON_STRING) return YWP_DROP_MALFORMED;
    size_t id_len = ywp_json_str_copy(d, m[ID], f->origin, sizeof(f->origin));
    if (id_len == SIZE_MAX || !ywp_id_valid(f->origin, id_len)) return YWP_DROP_MALFORMED;
    if (!ywp_json_uint(d, m[SEQ], YWP_MAX_SEQUENCE, &seq) || seq < 1) return YWP_DROP_MALFORMED;
    if (!ywp_json_number(d, m[TS], &f->ts)) return YWP_DROP_MALFORMED;
    if (d->tok[m[BODY]].type != YWP_JSON_OBJECT) return YWP_DROP_MALFORMED;
    if (d->tok[m[SIG]].type != YWP_JSON_STRING) return YWP_DROP_MALFORMED;

    /* sig must be 64 lowercase hex digits or "" before any comparison
     * (ADR 0010); any other string is an auth drop. */
    char hex[2 * YWP_SHA256_LEN + 1];
    uint8_t sig[YWP_SHA256_LEN], mac[YWP_SHA256_LEN];
    size_t sig_len = ywp_json_str_copy(d, m[SIG], hex, sizeof(hex));
    if (sig_len == SIZE_MAX || (sig_len != 0 && sig_len != sizeof(hex) - 1)) return YWP_DROP_AUTH;
    if (sig_len && !sig_bytes(hex, sig)) return YWP_DROP_AUTH;
    if (!c->keyed) {
        if (sig_len) return YWP_DROP_AUTH;           /* open mode: sig == "" */
    } else {
        if (!sig_len) return YWP_DROP_AUTH;          /* unsigned on a keyed receiver */
        /* Either message authenticates (ADR 0023); the order does not change
         * the accepted set. Transmitted bytes first: reference senders are
         * canonical, so it costs no re-serialization. */
        mac_transmitted(c, key_of(m[SIG]), mac);
        bool ok = ywp_hmac_equal(&c->mac, mac, sig, sizeof(sig));
        if (!ok) {
            mac_canonical(c, 0, key_of(m[SIG]), mac);
            ok = ywp_hmac_equal(&c->mac, mac, sig, sizeof(sig));
        }
        if (!ok) return YWP_DROP_AUTH;
    }
    memset(f->fingerprint, 0, sizeof(f->fingerprint));
    f->type = (ywp_frame_type_t)type;
    f->seq = seq;
    f->body = m[BODY];
    return YWP_OK;
}

static bool has_float(const ywp_json_t *d)
{
    for (unsigned i = 0; i < d->count; ++i)
        if (d->tok[i].type == YWP_JSON_FLOAT) return true;
    return false;
}

/* The envelope after the body: ,"id":..,"seq":N,  [sig goes here]  "t":..,"ts":..,"v":1} */
static size_t write_envelope(ywp_out_t *o, ywp_frame_type_t type, const char *origin,
                             uint64_t seq, double ts)
{
    ywp_out_text(o, ",\"id\":");
    ywp_out_str(o, origin, strlen(origin));
    ywp_out_text(o, ",\"seq\":");
    ywp_out_uint(o, seq);
    ywp_out_raw(o, ",", 1);
    size_t mark = o->total;
    ywp_out_text(o, "\"t\":\"");
    ywp_out_text(o, ywp_frame_type_name(type));
    ywp_out_text(o, "\",\"ts\":");
    ywp_out_float(o, ts);
    ywp_out_text(o, ",\"v\":1}");
    return mark;
}

size_t ywp_encode(ywp_codec_t *c, ywp_frame_type_t type, const char *origin,
                  uint64_t seq, double ts, const uint8_t *body, size_t body_len,
                  uint8_t out[YWP_MAX_FRAME])
{
    ywp_json_t *d = &c->json;
    if ((unsigned)type > (unsigned)YWP_BYE || !origin || !ywp_id_valid(origin, strlen(origin)) ||
        seq < 1 || seq > YWP_MAX_SEQUENCE || !isfinite(ts)) return 0;
    /* The body sits at depth 2 of the frame; integers only (frames.encode). */
    if (!ywp_json_parse(d, body, body_len) || d->tok[0].type != YWP_JSON_OBJECT ||
        d->depth > YWP_JSON_MAX_DEPTH - 1 || has_float(d)) return 0;
    ywp_out_t o;
    ywp_out_buffer(&o, out, YWP_MAX_FRAME);
    ywp_out_text(&o, "{\"body\":");
    ywp_json_canonical(d, 0, YWP_JSON_NONE, &o);
    size_t mark = write_envelope(&o, type, origin, seq, ts);
    if (!ywp_out_finish(&o)) return 0;

    /* Sign the unsigned canonical bytes, then insert sig in sorted place. */
    char field[8 + 2 * YWP_SHA256_LEN + 2] = "\"sig\":\"";
    size_t flen = 7;
    if (c->keyed) {
        static const char hex[] = "0123456789abcdef";
        uint8_t mac[YWP_SHA256_LEN];
        ywp_hmac_start(&c->mac);
        ywp_hmac_update(&c->mac, out, o.len);
        ywp_hmac_finish(&c->mac, mac);
        for (unsigned i = 0; i < YWP_SHA256_LEN; ++i) {
            field[flen++] = hex[mac[i] >> 4];
            field[flen++] = hex[mac[i] & 15];
        }
    }
    field[flen++] = '"';
    field[flen++] = ',';
    if (o.len + flen > YWP_MAX_FRAME) return 0;
    memmove(out + mark + flen, out + mark, o.len - mark);
    memcpy(out + mark, field, flen);
    return o.len + flen;
}

size_t ywp_frame_overhead(const char *origin)
{
    static const char zeros[] =
        "\"sig\":\"0000000000000000000000000000000000000000000000000000000000000000\",";
    ywp_out_t o;
    ywp_out_buffer(&o, NULL, 0);                   /* count only */
    ywp_out_text(&o, "{\"body\":");
    (void)write_envelope(&o, YWP_ANN, origin, YWP_MAX_SEQUENCE, -1.2345678901234567e308);
    return o.total + sizeof(zeros) - 1;
}
