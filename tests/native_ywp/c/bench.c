/* Host cost of the native YWP/1 library: one JSON object on stdout with
 * nanoseconds per operation and the memory footprint of the default and a
 * minimal configuration. HOST NUMBERS (x86-64 or whatever builds this);
 * device numbers need the device. `make ywp-bench` builds it with -O2. */
#include <stdio.h>
#include <string.h>
#include <time.h>
#include "ywp.h"
#include "ywp_route.h"

static const uint8_t KEY[] = "bench-site-key-0123456789abcdef!";
static const char ANN_BODY[] =
    "{\"err\":null,\"kind\":\"usb\",\"media\":{\"busids\":[\"1-1\"],\"host\":\"10.0.0.1\","
    "\"port\":3240},\"name\":\"rack encoder\",\"pairing\":null,\"role\":\"producer\","
    "\"state\":\"active\"}";

static double now_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec * 1e9 + (double)ts.tv_nsec;
}

#define TIME(label, iters, body)                                         \
    do {                                                                 \
        double start = now_ns();                                         \
        for (long i_ = 0; i_ < (iters); ++i_) { body; }                  \
        printf("%s\"%s\": %.0f", first ? "" : ", ", label,               \
               (now_ns() - start) / (double)(iters));                    \
        first = false;                                                   \
    } while (0)

static ywp_codec_t codec, tx;

/* A forged (bad-signature) 1200 B frame whose body is an array of `elem`:
 * the costliest inputs a keyless host can send (cold review 2026-11-05). */
static size_t forged(uint8_t *out, const char *elem)
{
    static const char tail[] = "]},\"id\":\"a\",\"seq\":1,\"sig\":\""
                               "0000000000000000000000000000000000000000000000000000000000000000"
                               "\",\"t\":\"bye\",\"ts\":0,\"v\":1}";
    size_t n = (size_t)sprintf((char *)out, "{\"body\":{\"x\":[%s", elem), e = strlen(elem);
    while (n + 1 + e + sizeof(tail) - 1 <= YWP_MAX_FRAME) {
        out[n++] = ',';
        memcpy(out + n, elem, e);
        n += e;
    }
    memcpy(out + n, tail, sizeof(tail) - 1);
    return n + sizeof(tail) - 1;
}
static uint8_t frame[YWP_MAX_FRAME], legacy[YWP_MAX_FRAME], worst[YWP_MAX_FRAME];
static uint8_t attack[YWP_MAX_FRAME];

static ywp_node_t *consumer_with_peers(size_t peers)
{
    ywp_config_t cfg;
    ywp_saved_t fresh;
    ywp_err_t err;
    memset(&fresh, 0, sizeof(fresh));
    ywp_config_default(&cfg);
    cfg.node_id = "con-1";
    cfg.psk = KEY;
    cfg.psk_len = sizeof(KEY) - 1;
    ywp_node_t *n = ywp_node_create(&cfg, &ywp_crypto_portable, &fresh, 0.0, 1793000000.0, &err);
    const ywp_effect_t *fx;
    ywp_node_start(n, 0.0);
    size_t count = ywp_node_effects(n, &fx);
    ywp_node_persisted(n, fx[count - 1].persist.request_id, true, 0.0);
    for (size_t i = 0; i < peers; ++i) {
        char id[24];
        snprintf(id, sizeof(id), "prod-%zu", i);
        size_t len = ywp_encode(&tx, YWP_ANN, id, 1793000000000000001ull, 1793000000.0,
                                (const uint8_t *)ANN_BODY, sizeof(ANN_BODY) - 1, frame);
        ywp_node_datagram(n, frame, len, 0.0, 1793000000.0, NULL);
    }
    return n;
}

int main(void)
{
    bool first = true;
    ywp_frame_t f;
    ywp_codec_init(&codec, &ywp_crypto_portable, KEY, sizeof(KEY) - 1);
    ywp_codec_init(&tx, &ywp_crypto_portable, KEY, sizeof(KEY) - 1);
    size_t flen = ywp_encode(&tx, YWP_ANN, "rack-1", 1793000000000000001ull, 1793000000.25,
                             (const uint8_t *)ANN_BODY, sizeof(ANN_BODY) - 1, frame);
    /* Legacy path: the same frame pretty-printed, sig over canonical bytes. */
    size_t llen = 0;
    for (size_t i = 0; i < flen; ++i) {
        legacy[llen++] = frame[i];
        if (frame[i] == ',' && llen < sizeof(legacy) - 2) legacy[llen++] = ' ';
    }
    /* Parse worst case: 1200 bytes of tiny tokens, rejected by signature. */
    memcpy(worst, "{\"body\":{\"x\":[0", 15);
    size_t wlen = 15;
    while (wlen < 1070) { memcpy(worst + wlen, ",0", 2); wlen += 2; }
    wlen += (size_t)sprintf((char *)worst + wlen, "]},\"id\":\"a\",\"seq\":1,\"sig\":\"%064d\","
                                                  "\"t\":\"bye\",\"ts\":0,\"v\":1}", 0);
    ywp_hmac_t mac;
    ywp_hmac_init(&mac, &ywp_crypto_portable, KEY, sizeof(KEY) - 1);
    uint8_t digest[32];
    printf("{\"forged_ns\": {");
    static const char *const elems[] = {
        "0", "1e308", "1e-300", "5e-324", "2.2250738585072009e-308",
        "1.2345678901234567e308", "4.9406564584124654e-324", "17976931348623157e292",
        /* Midpoint straddles: the first 19 digits and the next round apart, so
         * the exact bignum comparison runs (20, 40 and 25 digits; normal and
         * subnormal; reviewer-verified against CPython). */
        "1.6688053938804012843e-308", "1.0000000000000001268e300",
        "1.668805393880401284350497458622575136448e-308",
        "1.000235900005603628685463e-320"};
    for (size_t k = 0; k < sizeof(elems) / sizeof(elems[0]); ++k) {
        size_t len = forged(attack, elems[k]);
        TIME(elems[k], 2000, ywp_decode(&codec, attack, len, &f));
    }
    printf("}, \"host_ns\": {");
    first = true;
    TIME("decode_canonical_ann", 20000, ywp_decode(&codec, frame, flen, &f));
    TIME("decode_legacy_path_ann", 20000, ywp_decode(&codec, legacy, llen, &f));
    TIME("decode_worst_parse_1200B", 5000, ywp_decode(&codec, worst, wlen, &f));
    TIME("hmac_1200B", 20000, (ywp_hmac_start(&mac), ywp_hmac_update(&mac, worst, 1200),
                               ywp_hmac_finish(&mac, digest)));
    TIME("encode_ann", 20000, ywp_encode(&tx, YWP_ANN, "rack-1", 1793000000000000002ull,
                                         1793000000.123456, (const uint8_t *)ANN_BODY,
                                         sizeof(ANN_BODY) - 1, frame));
    ywp_node_t *n = consumer_with_peers(32);
    double t = 1.0;
    TIME("node_poll_32_peers", 100000, ywp_node_poll(n, t += 1e-6, 1793000001.0));
    TIME("node_next_deadline_32_peers", 100000, (void)ywp_node_next_deadline(n));
    enum { BATCH = 4096 };
    static uint8_t anns[BATCH][YWP_MAX_FRAME];
    static size_t lens[BATCH];
    for (size_t i = 0; i < BATCH; ++i)              /* fresh seqs: each one is accepted */
        lens[i] = ywp_encode(&tx, YWP_ANN, "prod-31", 1793000000000000002ull + i, 1793000000.5,
                             (const uint8_t *)ANN_BODY, sizeof(ANN_BODY) - 1, anns[i]);
    TIME("node_datagram_ann_known_peer", BATCH,
         ywp_node_datagram(n, anns[i_], lens[i_], t, 1793000001.0, NULL));
    printf("}");
    ywp_limits_t limits;
    ywp_limits_default(&limits);
    size_t default_bytes = ywp_node_footprint(&limits);
    ywp_limits_t producer = limits;
    producer.media_bytes = 0;
    ywp_limits_t small = {8, 16, 8, 4, 2 * YWP_MEDIA_MAX, 16, 4096};
    printf(", \"bytes\": {\"node_default\": %zu, \"node_default_producer\": %zu, "
           "\"node_small_consumer\": %zu, \"codec\": %zu, \"json_tokens\": %zu, "
           "\"route\": %zu, \"effect_record\": %zu, \"limits_default\": [%u, %u, %u, %u, %u, %u, %u]}}\n",
           default_bytes, ywp_node_footprint(&producer), ywp_node_footprint(&small),
           sizeof(ywp_codec_t), sizeof(ywp_json_t), sizeof(ywp_route_t), sizeof(ywp_effect_t),
           limits.peers, limits.origins, limits.intents, limits.rejections, limits.media_bytes,
           limits.effects, limits.effect_bytes);
    ywp_node_destroy(n);
    return 0;
}
