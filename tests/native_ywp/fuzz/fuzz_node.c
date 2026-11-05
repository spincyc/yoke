/* libFuzzer target: the node state machine under arbitrary input
 * sequences. The input is a program of one-byte opcodes driving a consumer
 * and a producer that share a site key: frames from fuzz-chosen origins
 * with fuzz-chosen bodies of up to ~1100 bytes (signed, so they reach the
 * handlers), raw datagrams, timer polls with clock jumps and wall steps,
 * driver statuses, storage completions that may fail, media changes and
 * shutdown; every frame either node sends is delivered to the other. The
 * nodes must never crash, overrun their effect space, emit an unencodable
 * frame or report a deadline in the past (except an overdue reservation
 * retry, which the reference reports as is). HOST ONLY. */
#include <math.h>
#include <stdlib.h>
#include <string.h>
#include "ywp.h"

static const uint8_t KEY[] = "fuzz-site-key-0123456789abcdef!!";
static const char *const ORIGINS[] = {"prod-1", "con-1", "con-2", "prod-2", "cli-a", "x"};

#define ASSERT(x) do { if (!(x)) abort(); } while (0)

/* Right after an input at `now`, a node's deadline is not in the past,
 * except an overdue reservation retry (reported as is, like the reference). */
static void check_deadline(const ywp_node_t *n, double now)
{
    ywp_status_t st;
    ywp_node_status(n, &st);
    ASSERT(ywp_node_next_deadline(n) >= now || (st.held && !st.save_pending));
}

typedef struct {
    const uint8_t *p;
    size_t left;
} reader_t;

/* Frames in flight between the two nodes (bounded; overflow is loss). */
static struct {
    int dest;
    size_t len;
    uint8_t raw[YWP_MAX_FRAME];
} wire[16];
static size_t wire_len;

static unsigned byte(reader_t *r)
{
    if (!r->left) return 0;
    --r->left;
    return *r->p++;
}

/* Perform effects; PERSIST completes per `ok` (the persist is last) and
 * sent frames go on the wire to the other node. */
static void perform(ywp_node_t *n, int self, double now, double wall, reader_t *r)
{
    for (unsigned rounds = 0; rounds < 8; ++rounds) {
        const ywp_effect_t *fx;
        size_t count = ywp_node_effects(n, &fx);
        int persist = -1;
        for (size_t i = 0; i < count; ++i) {
            if (fx[i].kind == YWP_FX_SEND) {
                uint8_t raw[YWP_MAX_FRAME];
                size_t len = ywp_node_encode(n, &fx[i], wall, raw);
                ASSERT(len > 0);
                if (wire_len < sizeof(wire) / sizeof(wire[0])) {
                    wire[wire_len].dest = 1 - self;
                    wire[wire_len].len = len;
                    memcpy(wire[wire_len++].raw, raw, len);
                }
            } else if (fx[i].kind == YWP_FX_PERSIST) {
                ASSERT(i == count - 1);
                persist = (int)i;
            }
        }
        ywp_status_t st;
        ywp_node_status(n, &st);
        ASSERT(st.overruns == 0);
        if (persist < 0) return;
        bool ok = (byte(r) & 7) != 0;
        ywp_node_persisted(n, fx[persist].persist.request_id, ok, now);
    }
}

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size);

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)
{
    ywp_config_t cfg;
    ywp_saved_t fresh;
    ywp_err_t err;
    ywp_node_t *nodes[2];
    memset(&fresh, 0, sizeof(fresh));
    for (int i = 0; i < 2; ++i) {
        ywp_config_default(&cfg);
        cfg.node_id = i ? "prod-1" : "con-1";
        cfg.role = i ? YWP_PRODUCER : YWP_CONSUMER;
        cfg.psk = KEY;
        cfg.psk_len = sizeof(KEY) - 1;
        cfg.admission = i == 1;
        cfg.seed = (uint64_t)i;
        cfg.media = i ? "{\"host\":\"10.0.0.1\",\"port\":3240,\"busids\":[\"1-1\"]}" : NULL;
        cfg.media_len = cfg.media ? strlen(cfg.media) : 0;
        cfg.limits.peers = cfg.limits.origins = cfg.limits.intents = cfg.limits.rejections = 4;
        cfg.limits.effects = 16;
        nodes[i] = ywp_node_create(&cfg, &ywp_crypto_portable, &fresh, 0.0, 1793000000.0, &err);
        ASSERT(nodes[i]);
    }
    ywp_codec_t codec;
    ywp_codec_init(&codec, &ywp_crypto_portable, KEY, sizeof(KEY) - 1);
    reader_t r = {data, size};
    wire_len = 0;
    double now = 0.0, skew = 1793000000.0;
    uint64_t seq = 1793000000000000000ull;
    for (int i = 0; i < 2; ++i) {
        ywp_node_start(nodes[i], now);
        perform(nodes[i], i, now, now + skew, &r);
    }
    while (r.left) {
        unsigned op = byte(&r), target = op & 1;
        ywp_node_t *n = nodes[target];
        double wall = now + skew;
        bool input = true;                       /* the node saw `now` */
        switch ((op >> 1) % 8) {
        case 0: case 1: {                          /* signed frame, fuzz body */
            unsigned len = (byte(&r) << 8 | byte(&r)) % 1100, type = byte(&r) % 5;
            const char *origin = ORIGINS[byte(&r) % 6];
            seq += 1 + byte(&r) * 1000000000ull;
            uint8_t raw[YWP_MAX_FRAME];
            size_t body = len < r.left ? len : r.left;
            size_t n_raw = ywp_encode(&codec, (ywp_frame_type_t)type, origin, seq, wall, r.p,
                                      body, raw);
            r.p += body;
            r.left -= body;
            if (n_raw) ywp_node_datagram(n, raw, n_raw, now, wall, NULL);
            input = n_raw > 0;
            break;
        }
        case 2: {                                  /* raw datagram */
            uint32_t source = 0x0a000000u | byte(&r);
            size_t len = byte(&r) % 128;
            len = len < r.left ? len : r.left;
            ywp_node_datagram(n, r.p, len, now, wall, &source);
            r.p += len;
            r.left -= len;
            break;
        }
        case 3: now += byte(&r) * 0.05; /* fall through */
        case 4: ywp_node_poll(n, now, (op & 0x80) ? NAN : wall); break;
        case 5: {
            const char *detail = (op & 0x40) ? "\xff\xfe broken \x01" : "fine";
            ywp_node_driver_status(n, (ywp_state_t)(byte(&r) % 4), detail, strlen(detail), now);
            break;
        }
        case 6:
            if (op & 0x80) now += 400.0;
            else skew += (op & 0x40) ? 7200.0 : -7200.0;
            input = false;
            break;
        default:
            if (op & 0x80) ywp_node_shutdown(n, now);
            else (void)ywp_node_set_media(n, "{\"host\":\"10.0.0.2\",\"port\":1,\"busids\":[]}",
                                          40, now);
        }
        perform(n, (int)target, now, wall, &r);
        if (input) check_deadline(n, now);
        for (size_t sent = 0; wire_len && sent < 16; ++sent) {   /* deliver */
            int dest = wire[0].dest;
            uint8_t raw[YWP_MAX_FRAME];
            size_t len = wire[0].len;
            memcpy(raw, wire[0].raw, len);
            memmove(wire, wire + 1, --wire_len * sizeof(wire[0]));
            uint32_t source = 0x0a000001u + (uint32_t)(1 - dest);
            ywp_node_datagram(nodes[dest], raw, len, now, wall, &source);
            perform(nodes[dest], dest, now, wall, &r);
            check_deadline(nodes[dest], now);
        }
    }
    for (int i = 0; i < 2; ++i) ywp_node_destroy(nodes[i]);
    return 0;
}
