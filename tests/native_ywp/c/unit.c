/* Unit tests of the native YWP/1 library: crypto known answers, the state
 * blob, JSON accessors, the jitter generator, the driver route queue and
 * node API contracts. Behavioural equivalence with the Python reference is
 * tested from Python (lockstep, simnet); this file covers what is only
 * visible from C. Built with ASan and UBSan. HOST ONLY. */
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "ywp.h"
#include "ywp_json.h"
#include "ywp_route.h"
#include "ywp_state.h"
#include "../../../native/ywp/src/ywp_internal.h"

static int failures, checks;

#define CHECK(cond)                                                          \
    do {                                                                     \
        ++checks;                                                            \
        if (!(cond)) {                                                       \
            fprintf(stderr, "%s:%d: CHECK(%s)\n", __FILE__, __LINE__, #cond);  \
            ++failures;                                                      \
        }                                                                    \
    } while (0)

static bool contains(const uint8_t *hay, size_t len, const char *needle)
{
    size_t n = strlen(needle);
    for (size_t i = 0; i + n <= len; ++i)
        if (memcmp(hay + i, needle, n) == 0) return true;
    return false;
}

static void hex(const uint8_t *p, size_t n, char *out)
{
    for (size_t i = 0; i < n; ++i) sprintf(out + 2 * i, "%02x", p[i]);
}

static void mac_hex(const uint8_t *key, size_t klen, const char *msg, char out[65])
{
    ywp_hmac_t mac;
    uint8_t digest[32];
    ywp_hmac_init(&mac, &ywp_crypto_portable, key, klen);
    ywp_hmac_start(&mac);
    for (size_t i = 0; msg[i]; ++i) ywp_hmac_update(&mac, msg + i, 1);   /* byte stream */
    ywp_hmac_finish(&mac, digest);
    hex(digest, 32, out);
}

static void test_crypto(void)
{
    const ywp_crypto_ops_t *ops = &ywp_crypto_portable;
    unsigned char state[YWP_SHA256_STATE_MAX];
    uint8_t digest[32];
    char text[65];
    static uint8_t thousand[1000];
    memset(thousand, 'a', sizeof(thousand));
    ops->sha256_start(NULL, state);
    ops->sha256_update(NULL, state, thousand, 3);
    ops->sha256_update(NULL, state, thousand + 3, 61);
    ops->sha256_update(NULL, state, thousand + 64, 936);
    ops->sha256_finish(NULL, state, digest);
    hex(digest, 32, text);
    CHECK(strcmp(text, "41edece42d63e8d9bf515a9ba6932e1c20cbc9f5a5d134645adb5db1b9737ea3") == 0);
    ops->sha256_start(NULL, state);
    ops->sha256_finish(NULL, state, digest);
    hex(digest, 32, text);
    CHECK(strcmp(text, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855") == 0);

    uint8_t big[131];
    memset(big, 0xaa, sizeof(big));
    mac_hex(big, sizeof(big), "Test Using Larger Than Block-Size Key - Hash Key First", text);
    CHECK(strcmp(text, "60e431591ee0b67f0d8a26aacbf5b77f8e0bc6213728c5140546040f0ee37f54") == 0);
    mac_hex((const uint8_t *)"Jefe", 4, "what do ya want for nothing?", text);
    CHECK(strcmp(text, "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843") == 0);
    mac_hex(NULL, 0, "", text);
    CHECK(strcmp(text, "b613679a0814d9ec772f95d778c35fc5ff1697c493715653c6c712144292c5ad") == 0);

    CHECK(ywp_ct_equal((const uint8_t *)"abc", (const uint8_t *)"abc", 3));
    CHECK(!ywp_ct_equal((const uint8_t *)"abc", (const uint8_t *)"abd", 3));
}

static ywp_saved_t saved(uint64_t seq, const char *producer, uint64_t n, const char *origin)
{
    ywp_saved_t s;
    memset(&s, 0, sizeof(s));
    s.seq = seq;
    s.pairing.paired = producer != NULL;
    if (producer) strcpy(s.pairing.producer, producer);
    s.pairing.version.n = n;
    strcpy(s.pairing.version.origin, origin);
    return s;
}

static void test_state(void)
{
    uint8_t blob[YWP_SAVED_MAX];
    ywp_saved_t in = saved(1793000000000004096ull, "prod-1", 7, "cli-7"), out;
    size_t len = ywp_saved_encode(&in, blob);
    CHECK(len == 4 + 8 + 8 + 1 + 6 + 1 + 5 + 4);
    CHECK(ywp_saved_decode(blob, len, &out) && memcmp(&in, &out, sizeof(in)) == 0);
    for (size_t i = 0; i < len; ++i) {             /* every single-bit flip is caught */
        blob[i] ^= 4;
        CHECK(!ywp_saved_decode(blob, len, &out));
        blob[i] ^= 4;
    }
    CHECK(!ywp_saved_decode(blob, len - 1, &out));
    ywp_saved_t fresh = saved(0, NULL, 0, "");
    len = ywp_saved_encode(&fresh, blob);
    CHECK(ywp_saved_decode(blob, len, &out) && !out.pairing.paired && out.pairing.version.n == 0);
    ywp_saved_t longest = saved(YWP_MAX_SEQUENCE, "pppppppppppppppppppppppppppppppp", YWP_MAX_SEQUENCE,
                                "oooooooooooooooooooooooooooooooo");
    CHECK(ywp_saved_encode(&longest, blob) == YWP_SAVED_MAX);
    ywp_saved_t bad[] = {
        saved(YWP_MAX_SEQUENCE + 1, NULL, 0, ""), saved(0, "prod-1", 0, ""),
        saved(0, NULL, 0, "cli"), saved(0, NULL, 1, ""), saved(0, "Bad", 1, "cli"),
        saved(0, NULL, YWP_MAX_SEQUENCE + 1, "cli"),
    };
    for (size_t i = 0; i < sizeof(bad) / sizeof(bad[0]); ++i)
        CHECK(!ywp_saved_valid(&bad[i]) && ywp_saved_encode(&bad[i], blob) == 0);

    /* ADR 0023 floors, as node.restart_floor and _admission_bound. */
    const uint64_t s = 1793000000000000000ull, ns = 1000000000ull;
    CHECK(ywp_restart_floor(0, 1793000123.9) == 1793000123ull * ns);
    CHECK(ywp_restart_floor(s, 1793000010.0) == 1793000010ull * ns);
    CHECK(ywp_restart_floor(s, 1893000000.0) == s + 60 * ns);         /* capped jump */
    CHECK(ywp_restart_floor(s, 1000.0) == s);                         /* clock behind */
    CHECK(ywp_admission_bound(1793000000.7) == 1793003600ull * ns);
    CHECK(ywp_admission_bound(1e300) == UINT64_MAX);
    CHECK(ywp_restart_floor(1, 1e300) == 1 + 60 * ns);
}

static void test_random(void)
{
    ywp_mt_t rng;
    ywp_mt_seed(&rng, 0);
    CHECK(ywp_mt_random(&rng) == 0.8444218515250481);
    CHECK(ywp_mt_uniform(&rng, -0.25, 0.25) == 0.12897720147015124);
    ywp_mt_seed(&rng, (1ull << 40) + 7);
    CHECK(ywp_mt_random(&rng) == 0.6137037779936511);
    ywp_mt_seed(&rng, 12345678901234567890ull);
    CHECK(ywp_mt_random(&rng) == 0.5120180983871961);
}

static bool parse(ywp_json_t *doc, const char *text)
{
    return ywp_json_parse(doc, (const uint8_t *)text, strlen(text));
}

static void test_json(void)
{
    static ywp_json_t doc;
    char buf[16];
    uint64_t v;
    double d;
    CHECK(parse(&doc, "{\"b\":[1,2,3],\"\\u0061\":\"x\\u00e9\",\"n\":-0,\"f\":1.5}"));
    unsigned a = ywp_json_get(&doc, 0, "a"), b = ywp_json_get(&doc, 0, "b");
    CHECK(a != YWP_JSON_NONE && ywp_json_str_is(&doc, a, "x\xc3\xa9"));
    CHECK(ywp_json_str_len(&doc, a) == 3 && ywp_json_str_chars(&doc, a) == 2);
    CHECK(ywp_json_str_copy(&doc, a, buf, 4) == 3 && strcmp(buf, "x\xc3\xa9") == 0);
    CHECK(ywp_json_str_copy(&doc, a, buf, 3) == SIZE_MAX);
    CHECK(ywp_json_count(&doc, 0) == 4 && ywp_json_count(&doc, b) == 3);
    CHECK(ywp_json_uint(&doc, ywp_json_at(&doc, b, 2), 3, &v) && v == 3);
    CHECK(!ywp_json_uint(&doc, ywp_json_at(&doc, b, 2), 2, &v));
    CHECK(ywp_json_at(&doc, b, 3) == YWP_JSON_NONE);
    CHECK(ywp_json_uint(&doc, ywp_json_get(&doc, 0, "n"), 0, &v) && v == 0);
    CHECK(ywp_json_number(&doc, ywp_json_get(&doc, 0, "n"), &d) && d == 0 && !signbit(d));
    CHECK(!ywp_json_uint(&doc, ywp_json_get(&doc, 0, "f"), 9, &v));
    CHECK(ywp_json_get(&doc, 0, "z") == YWP_JSON_NONE && ywp_json_get(&doc, b, "a") == YWP_JSON_NONE);
    CHECK(doc.depth == 2);

    uint8_t out[8];
    ywp_out_t o;
    ywp_out_buffer(&o, out, sizeof(out));
    ywp_out_str(&o, "a\"\n\x01", 4);
    CHECK(o.overflow && o.total == 2 + 1 + 2 + 2 + 6 && !ywp_out_finish(&o));
    ywp_out_buffer(&o, NULL, 0);                    /* counting sink */
    ywp_out_float(&o, 1e16);
    CHECK(o.total == 5);

    CHECK(ywp_utf8_clip("a\xc3\xa9", 3, 2) == 1 && ywp_utf8_clip("abc", 3, 5) == 3);
    CHECK(!ywp_utf8_valid((const uint8_t *)"\xed\xa0\x80", 3));
    CHECK(ywp_utf8_valid((const uint8_t *)"\xf4\x8f\xbf\xbf", 4));
    CHECK(!ywp_utf8_valid((const uint8_t *)"\xf4\x90\x80\x80", 4));
}

static ywp_effect_t fx_attach(const char *producer, const char *media)
{
    ywp_effect_t fx;
    memset(&fx, 0, sizeof(fx));
    fx.kind = YWP_FX_ATTACH;
    fx.attach.producer = producer;
    fx.attach.media = media;
    fx.attach.media_len = strlen(media);
    return fx;
}

static bool done(ywp_route_t *r, bool completed, ywp_state_t state, const char **detail)
{
    size_t len = *detail ? strlen(*detail) : 0;
    ywp_state_t st = state;
    return ywp_route_done(r, completed, &st, detail, &len);
}

static void test_route(void)
{
    ywp_route_t r;
    ywp_route_cmd_t cmd;
    const char *detail = NULL;
    ywp_effect_t detach = {.kind = YWP_FX_DETACH};
    ywp_effect_t a = fx_attach("prod-a", "{\"a\":1}"), b = fx_attach("prod-b", "{\"b\":2}");

    /* Pending attaches coalesce to the latest desired route. */
    ywp_route_init(&r);
    ywp_route_submit(&r, &a);
    ywp_route_submit(&r, &b);
    CHECK(ywp_route_next(&r, &cmd) && !cmd.detach && strcmp(cmd.producer, "prod-b") == 0);
    CHECK(!ywp_route_next(&r, &cmd));                 /* one command at a time */
    CHECK(done(&r, true, YWP_ACTIVE, &detail));
    CHECK(ywp_route_poll_current(&r, ywp_route_poll_tag(&r)));

    /* A detach barrier precedes a later attach; its stale result is withheld. */
    ywp_route_submit(&r, &detach);
    ywp_route_submit(&r, &a);
    CHECK(ywp_route_next(&r, &cmd) && cmd.detach);
    CHECK(!done(&r, true, YWP_IDLE, &detail));
    CHECK(ywp_route_next(&r, &cmd) && strcmp(cmd.producer, "prod-a") == 0 &&
          memcmp(cmd.media, "{\"a\":1}", cmd.media_len) == 0);
    uint32_t tag = ywp_route_poll_tag(&r);            /* poll queued behind it */
    CHECK(done(&r, true, YWP_ACTIVE, &detail));
    CHECK(!ywp_route_poll_current(&r, tag));          /* old route's health */

    /* A failed command reports against its generation, never promoting it. */
    ywp_route_submit(&r, &b);
    CHECK(ywp_route_next(&r, &cmd));
    detail = "boom";
    CHECK(done(&r, false, YWP_ERROR, &detail));
    CHECK(ywp_route_poll_tag(&r) != r.generation);

    /* Unconfirmed teardown is an error and a barrier that waits for work. */
    ywp_route_submit(&r, &detach);
    CHECK(ywp_route_next(&r, &cmd) && cmd.detach);
    size_t len = 0;
    ywp_state_t st = YWP_ACTIVE;
    detail = NULL;
    CHECK(ywp_route_done(&r, true, &st, &detail, &len) && st == YWP_ERROR && len > 0);
    CHECK(!ywp_route_next(&r, &cmd));                 /* no spin */
    ywp_route_submit(&r, &a);
    CHECK(ywp_route_next(&r, &cmd) && cmd.detach);    /* barrier first */
    detail = NULL;
    CHECK(!done(&r, true, YWP_IDLE, &detail));
    CHECK(ywp_route_next(&r, &cmd) && !cmd.detach);
    CHECK(done(&r, true, YWP_ACTIVE, &detail));

    /* Review repro: a route submitted while a teardown runs and then fails
     * wakes the worker, so the barrier reruns at once (runner.py's event). */
    ywp_route_init(&r);
    ywp_route_submit(&r, &detach);
    CHECK(ywp_route_next(&r, &cmd) && cmd.detach);
    ywp_route_submit(&r, &a);                         /* arrives mid-command */
    st = YWP_ACTIVE;
    detail = NULL;
    len = 0;
    CHECK(ywp_route_done(&r, true, &st, &detail, &len) && st == YWP_ERROR);
    CHECK(ywp_route_next(&r, &cmd) && cmd.detach);    /* no wait for a retry */
    detail = NULL;
    CHECK(!done(&r, true, YWP_IDLE, &detail));
    CHECK(ywp_route_next(&r, &cmd) && !cmd.detach && strcmp(cmd.producer, "prod-a") == 0);
    CHECK(done(&r, true, YWP_ACTIVE, &detail));

    /* Shutdown: pending routes dropped; final teardown runs, never published. */
    ywp_route_submit(&r, &b);
    ywp_route_close(&r);
    CHECK(!ywp_route_next(&r, &cmd));
    ywp_route_submit(&r, &detach);
    CHECK(ywp_route_next(&r, &cmd) && cmd.detach);
    CHECK(!done(&r, true, YWP_ACTIVE, &detail) && !ywp_route_next(&r, &cmd));
}

/* -- node API --------------------------------------------------------------- */

static const uint8_t PSK[] = "unit-test-site-key-0123456789abcdef";

static ywp_node_t *make(ywp_role_t role, const ywp_limits_t *limits, ywp_err_t *err)
{
    ywp_config_t cfg;
    ywp_config_default(&cfg);
    cfg.node_id = role == YWP_CONSUMER ? "con-1" : "prod-1";
    cfg.role = role;
    cfg.psk = PSK;
    cfg.psk_len = sizeof(PSK) - 1;
    if (role == YWP_PRODUCER) {
        cfg.media = "{\"host\":\"10.0.0.1\",\"port\":3240,\"busids\":[\"1-1\"]}";
        cfg.media_len = strlen(cfg.media);
    }
    if (limits) cfg.limits = *limits;
    ywp_saved_t s = saved(0, NULL, 0, "");
    return ywp_node_create(&cfg, &ywp_crypto_portable, &s, 10.0, 1793000000.0, err);
}

/* Perform effects like a shell: saves succeed. Returns sends seen. */
static size_t drain(ywp_node_t *n, double now, size_t *events)
{
    size_t sends = 0;
    for (;;) {
        const ywp_effect_t *fx;
        size_t count = ywp_node_effects(n, &fx);
        bool persisted = false;
        for (size_t i = 0; i < count; ++i) {
            if (fx[i].kind == YWP_FX_PERSIST) {
                CHECK(i == count - 1);                      /* always last */
                ywp_node_persisted(n, fx[i].persist.request_id, true, now);
                persisted = true;
            } else if (fx[i].kind == YWP_FX_SEND) {
                uint8_t raw[YWP_MAX_FRAME];
                CHECK(ywp_node_encode(n, &fx[i], NAN, raw) > 0);
                ++sends;
                if (events && fx[i].send.type == YWP_EVT) ++*events;
            }
        }
        if (!persisted) return sends;
    }
}

static void announce(ywp_codec_t *codec, ywp_node_t *n, const char *id, uint64_t seq, double now)
{
    static const char body[] =
        "{\"err\":null,\"kind\":\"usb\",\"media\":{\"host\":\"10.0.0.2\",\"port\":1,"
        "\"busids\":[\"1-1\"]},\"name\":\"\",\"pairing\":null,\"role\":\"producer\","
        "\"state\":\"active\"}";
    uint8_t raw[YWP_MAX_FRAME];
    size_t len = ywp_encode(codec, YWP_ANN, id, seq, 1793000000.0 + now, (const uint8_t *)body,
                            sizeof(body) - 1, raw);
    CHECK(len > 0);
    ywp_node_datagram(n, raw, len, now, 1793000000.0 + now, NULL);
}

static void test_node_contracts(void)
{
    ywp_err_t err;
    ywp_limits_t limits;
    ywp_config_t cfg;
    ywp_saved_t fresh = saved(0, NULL, 0, "");
    ywp_limits_default(&limits);
    CHECK(ywp_node_footprint(&limits) > 0 && ywp_node_footprint(&limits) < 64 * 1024);
    ywp_limits_t tiny = limits;
    tiny.effect_bytes = 100;
    CHECK(ywp_node_footprint(&tiny) == 0);
    CHECK(!make(YWP_CONSUMER, &tiny, &err) && err == YWP_E_CONFIG);

    ywp_config_default(&cfg);
    cfg.node_id = "Bad";
    CHECK(!ywp_node_create(&cfg, &ywp_crypto_portable, &fresh, 0, 0, &err) && err == YWP_E_ID);
    cfg.node_id = "ok";
    cfg.name = "\xff";
    CHECK(!ywp_node_create(&cfg, &ywp_crypto_portable, &fresh, 0, 0, &err) && err == YWP_E_NAME);
    cfg.name = NULL;
    CHECK(!ywp_node_create(&cfg, &ywp_crypto_portable, &fresh, 0, -1, &err) && err == YWP_E_CLOCK);
    CHECK(!ywp_node_create(&cfg, &ywp_crypto_portable, &fresh, NAN, 0, &err) && err == YWP_E_CLOCK);
    ywp_saved_t bad = saved(0, "prod-1", 0, "");
    CHECK(!ywp_node_create(&cfg, &ywp_crypto_portable, &bad, 0, 0, &err) && err == YWP_E_STATE);
    ywp_saved_t late = saved(YWP_MAX_SEQUENCE - 1, NULL, 0, "");
    CHECK(!ywp_node_create(&cfg, &ywp_crypto_portable, &late, 0, 1e18, &err) &&
          err == YWP_E_SEQUENCE);
    cfg.role = YWP_PRODUCER;
    const char *medias[] = {"[1]", "{\"x\":1.5}", "{", "{\"x\":\"", "7"};
    for (size_t i = 0; i < 5; ++i) {
        cfg.media = medias[i];
        cfg.media_len = strlen(medias[i]);
        CHECK(!ywp_node_create(&cfg, &ywp_crypto_portable, &fresh, 0, 0, &err) && err == YWP_E_MEDIA);
    }

    /* Start: the durable barrier precedes every frame. */
    ywp_node_t *p = make(YWP_PRODUCER, NULL, &err);
    CHECK(p != NULL);
    const ywp_effect_t *fx;
    ywp_node_start(p, 10.0);
    size_t count = ywp_node_effects(p, &fx);
    CHECK(count >= 1 && fx[count - 1].kind == YWP_FX_PERSIST);
    for (size_t i = 0; i < count; ++i) CHECK(fx[i].kind != YWP_FX_SEND);
    uint32_t request = fx[count - 1].persist.request_id;
    ywp_node_persisted(p, request + 1, true, 10.0);             /* unknown id: ignored */
    CHECK(ywp_node_effects(p, &fx) == 0);
    ywp_node_start(p, 10.0);                                    /* overlapping: refused */
    CHECK(ywp_node_effects(p, &fx) == 0);
    ywp_status_t st;
    ywp_node_status(p, &st);
    CHECK(st.save_pending && !st.started);
    ywp_node_persisted(p, request, true, 10.0);
    drain(p, 10.0, NULL);
    ywp_node_status(p, &st);
    CHECK(st.started && st.reserved_until == st.last_seq - 2 + YWP_SEQUENCE_RESERVATION);

    /* Media: a rejected description leaves the announced one unchanged. */
    char pad[1400];
    memset(pad, 'x', sizeof(pad));
    memcpy(pad, "{\"p\":\"", 6);
    memcpy(pad + sizeof(pad) - 2, "\"}", 2);
    CHECK(ywp_node_set_media(p, pad, sizeof(pad), 11.0) == YWP_E_MEDIA);
    CHECK(ywp_node_effects(p, &fx) == 0);
    CHECK(ywp_node_set_media(p, "{\"host\":\"10.0.0.3\",\"port\":1,\"busids\":[\"2-1\"]}", 45,
                             11.0) == YWP_E_OK);
    CHECK(ywp_node_effects(p, &fx) == 1 && fx[0].kind == YWP_FX_SEND && fx[0].send.type == YWP_ANN);
    CHECK(contains(fx[0].send.body, fx[0].send.body_len, "10.0.0.3"));
    ywp_node_shutdown(p, 12.0);
    count = ywp_node_effects(p, &fx);
    CHECK(count == 2 && fx[1].kind == YWP_FX_SEND && fx[1].send.type == YWP_BYE);
    ywp_node_destroy(p);
}

/* Many peers time out at once with the minimum effect space: the poll
 * yields in steps and asks to be polled again at once, losing nothing. */
static void test_poll_yields_without_loss(void)
{
    ywp_err_t err;
    ywp_limits_t limits;
    ywp_limits_default(&limits);
    limits.effects = 16;
    limits.effect_bytes = 4096;
    ywp_node_t *n = make(YWP_CONSUMER, &limits, &err);
    CHECK(n != NULL);
    ywp_codec_t codec;
    ywp_codec_init(&codec, &ywp_crypto_portable, PSK, sizeof(PSK) - 1);
    ywp_node_start(n, 10.0);
    drain(n, 10.0, NULL);
    char id[24];
    for (int i = 0; i < 32; ++i) {
        snprintf(id, sizeof(id), "prod-%d", i);
        announce(&codec, n, id, 1793000000000000000ull + 1, 10.0 + i * 0.001);
        drain(n, 10.0, NULL);
    }
    ywp_status_t st;
    ywp_node_status(n, &st);
    CHECK(st.peers == 32);
    double now = 30.0;
    size_t events = 0, polls = 0;
    do {
        ywp_node_poll(n, now, NAN);
        drain(n, now, &events);
        ++polls;
    } while (ywp_node_next_deadline(n) <= now && polls < 100);
    ywp_node_status(n, &st);
    CHECK(polls > 1 && events >= 32 && st.overruns == 0);
    size_t lost = 0;
    for (size_t i = 0; i < ywp_node_peer_count(n); ++i) {
        ywp_peer_t peer;
        CHECK(ywp_node_peer(n, i, &peer));
        lost += peer.lost;
    }
    CHECK(lost == 32);
    ywp_node_destroy(n);
}

/* Minimum effect space (16 records, 4 KiB) with every field at its worst:
 * a 64-character name of 4-byte UTF-8, the largest media that fits and a
 * 128-byte all-control driver detail (escaped sixfold). Nothing may be
 * dropped, and every frame must encode within the budget. */
static void stress_node(ywp_node_t *n, double now, size_t *sends)
{
    const ywp_effect_t *fx;
    for (size_t round = 0; round < 8; ++round) {
        size_t count = ywp_node_effects(n, &fx);
        bool persisted = false;
        for (size_t i = 0; i < count; ++i) {
            uint8_t raw[YWP_MAX_FRAME];
            if (fx[i].kind == YWP_FX_SEND) {
                CHECK(ywp_node_encode(n, &fx[i], NAN, raw) > 0);
                ++*sends;
            } else if (fx[i].kind == YWP_FX_PERSIST) {
                ywp_node_persisted(n, fx[i].persist.request_id, true, now);
                persisted = true;
            }
        }
        if (!persisted) break;
    }
    ywp_status_t st;
    ywp_node_status(n, &st);
    CHECK(st.overruns == 0);
}

static void test_minimum_limits_worst_fields(void)
{
    static char name[4 * 64 + 1], media[1300], control[128], body[YWP_MAX_FRAME];
    for (int i = 0; i < 64; ++i) memcpy(name + 4 * i, "\xf0\x9f\x98\x80", 4);
    for (int i = 0; i < 128; ++i) control[i] = (char)(1 + i % 31);
    ywp_limits_t limits;
    ywp_limits_default(&limits);
    limits.effects = 16;
    limits.effect_bytes = 4096;
    ywp_config_t cfg;
    ywp_config_default(&cfg);
    cfg.limits = limits;
    cfg.name = name;
    cfg.psk = PSK;
    cfg.psk_len = sizeof(PSK) - 1;
    ywp_saved_t fresh = saved(0, NULL, 0, "");
    ywp_err_t err;
    size_t sends = 0;

    /* Producer: the largest media its worst announce can carry. */
    cfg.node_id = "prod-zzzzzzzzzzzzzzzzzzzzzzzzzzz";
    cfg.role = YWP_PRODUCER;
    ywp_node_t *p = NULL;
    for (size_t pad = 1100; pad > 0 && !p; --pad) {
        int len = snprintf(media, sizeof(media), "{\"p\":\"%0*d\"}", (int)pad, 0);
        cfg.media = media;
        cfg.media_len = (size_t)len;
        p = ywp_node_create(&cfg, &ywp_crypto_portable, &fresh, 1.0, 1793000000.0, &err);
    }
    CHECK(p != NULL && cfg.media_len > 600);
    if (!p) return;
    ywp_node_start(p, 1.0);
    stress_node(p, 1.0, &sends);
    ywp_node_driver_status(p, YWP_ERROR, control, sizeof(control), 2.0);
    stress_node(p, 2.0, &sends);
    ywp_node_driver_status(p, YWP_ACTIVE, control, sizeof(control), 3.0);
    stress_node(p, 3.0, &sends);
    ywp_node_shutdown(p, 4.0);
    stress_node(p, 4.0, &sends);
    ywp_node_destroy(p);

    /* Consumer: paired to a producer announcing near-frame-sized media. */
    cfg.node_id = "con-zzzzzzzzzzzzzzzzzzzzzzzzzzzz";
    cfg.role = YWP_CONSUMER;
    cfg.media = NULL;
    cfg.media_len = 0;
    ywp_node_t *c = ywp_node_create(&cfg, &ywp_crypto_portable, &fresh, 1.0, 1793000000.0, &err);
    CHECK(c != NULL);
    if (!c) return;
    ywp_codec_t codec;
    ywp_codec_init(&codec, &ywp_crypto_portable, PSK, sizeof(PSK) - 1);
    ywp_node_start(c, 1.0);
    stress_node(c, 1.0, &sends);
    uint8_t raw[YWP_MAX_FRAME];
    size_t len = 0;
    for (size_t pad = 1100; pad > 0 && !len; --pad) {
        int n = snprintf(body, sizeof(body),
                         "{\"err\":null,\"kind\":\"usb\",\"media\":{\"p\":\"%0*d\"},\"name\":\"\","
                         "\"pairing\":null,\"role\":\"producer\",\"state\":\"active\"}",
                         (int)pad, 0);
        len = n > 0 && (size_t)n < sizeof(body)
                  ? ywp_encode(&codec, YWP_ANN, "prod-1", 1793000000000000001ull, 1793000000.0,
                               (const uint8_t *)body, (size_t)n, raw) : 0;
    }
    CHECK(len > 1100);
    ywp_node_datagram(c, raw, len, 2.0, 1793000002.0, NULL);
    stress_node(c, 2.0, &sends);
    static const char intent[] =
        "{\"consumer\":\"con-zzzzzzzzzzzzzzzzzzzzzzzzzzzz\",\"op\":\"pair\","
        "\"producer\":\"prod-1\",\"version\":[9223372036854775807,"
        "\"cli-zzzzzzzzzzzzzzzzzzzzzzzzzzzz\"]}";
    len = ywp_encode(&codec, YWP_INT, "cli-x", 1793000000000000002ull, 1793000000.0,
                     (const uint8_t *)intent, sizeof(intent) - 1, raw);
    ywp_node_datagram(c, raw, len, 3.0, 1793000003.0, NULL);
    stress_node(c, 3.0, &sends);
    ywp_status_t st;
    ywp_node_status(c, &st);
    CHECK(st.pairing.paired && strcmp(st.pairing.producer, "prod-1") == 0);
    ywp_node_driver_status(c, YWP_ERROR, control, sizeof(control), 4.0);
    stress_node(c, 4.0, &sends);
    ywp_node_poll(c, 5.0, NAN);                               /* retry: attach */
    stress_node(c, 5.0, &sends);
    ywp_node_shutdown(c, 6.0);
    stress_node(c, 6.0, &sends);
    CHECK(sends > 15);
    ywp_node_destroy(c);
}

int main(void)
{
    test_crypto();
    test_state();
    test_random();
    test_json();
    test_route();
    test_node_contracts();
    test_poll_yields_without_loss();
    test_minimum_limits_worst_fields();
    printf("%d checks, %d failures\n", checks, failures);
    return failures != 0;
}
