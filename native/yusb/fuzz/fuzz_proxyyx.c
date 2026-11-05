/* libFuzzer: the computer-side core against a scripted exporter and UDC.
 *
 * The harness plays both peers. It reads the core's own transmit stream to
 * learn outstanding sequence numbers, then the input chooses, step by step:
 * answer a URB (any status, length and bytes, descriptor answers included),
 * answer or race an unlink, raw stream bytes, gadget events and SETUPs,
 * ep0 and endpoint completions (including EINTR/EPIPE/ESHUTDOWN), and time.
 * Invariants are the sanitizers' plus the core's own consistency, and the
 * backend contract: one ep0 operation at a time, one per handle.
 *
 * The first byte also picks a fixed device (a BOT mass-storage interface
 * with bulk 0x81/0x02) answered by an oracle during the descriptor fetch,
 * then configured, so BOT CBW planning, stalls and resets are reachable;
 * endpoint reads then sometimes carry a well-formed CBW.
 *
 * Built twice: fuzz_proxy (plain USB/IP) and fuzz_proxyyx (FUZZ_YX: the
 * exporter agrees YX; answers are plain, YX-encoded or garbage block
 * streams, including for timed-out and unlinked URBs, whose late payloads
 * must still be decoded to keep the cache in step). */
#ifndef FUZZ_PLAIN
#define FUZZ_YX 1
#endif
#include <stdlib.h>
#include <string.h>
#include "yusb_proxy.h"
#include "yusb_yx.h"
#ifdef FUZZ_YX
static yusb_yx_enc_t henc;
static yusb_yx_dec_t hdec;
static const yusb_yx_params_t AG = {1, 0x07, 8};
static uint8_t wirebuf[(1 << 20) + 4096];
static uint8_t sink[1 << 20];
#endif

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size);

#define MAX_OUT 256
#define HANDLES 32

typedef struct {
    uint32_t seq, length, victim;
    bool in, unlink, live;
} out_t;

static struct {
    const uint8_t *in;
    size_t size, pos;
    out_t outs[MAX_OUT];
    unsigned nouts;
    bool ep0_busy;
    bool ep_busy[HANDLES];
    int next_handle;
    uint8_t stream[1 << 20];
    size_t have;
} h;

static unsigned take(void) { return h.pos < h.size ? h.in[h.pos++] : 0; }

/* The fixed device's descriptors, answered by wLength during the fetch. */
static const uint8_t dev_desc[18] = {18, 1, 0, 2, 0, 0, 0, 64, 0x09, 0x12, 0x99, 0, 0, 1, 0, 0, 0, 1};
static const uint8_t bot_cfg[32] = {9, 2, 32, 0, 1, 1, 0, 0x80, 50, 9, 4, 0, 0, 2, 0x08, 0x06, 0x50, 0,
                                    7, 5, 0x81, 2, 0, 2, 0, 7, 5, 0x02, 2, 0, 2, 0};

/* Backend contract checks: one ep0 operation at a time, one per handle, and
 * no halt or disable while an operation is outstanding (Raw Gadget). */
static void ep0_start(void) { if (h.ep0_busy) abort(); h.ep0_busy = true; }
static void ep_start(int hd) { if (hd < 0 || hd >= HANDLES || h.ep_busy[hd]) abort(); h.ep_busy[hd] = true; }
static void g_ep0_write(void *c, uint8_t *d, size_t l, bool z) { (void)c; (void)z; if (l) d[l - 1] = d[0]; ep0_start(); }
static void g_ep0_read(void *c, uint8_t *d, size_t l) { (void)c; if (l) memset(d, 0x5a, l); ep0_start(); }
static void g_ep0_stall(void *c) { (void)c; ep0_start(); }
static int g_enable(void *c, const yusb_endpoint_t *e) { (void)c; (void)e; return h.next_handle < HANDLES ? h.next_handle++ : -16; }
static int g_idle(void *c, int hd) { (void)c; if (hd >= 0 && hd < HANDLES && h.ep_busy[hd]) abort(); return 0; }
static int g_none(void *c) { (void)c; return 0; }
static int g_draw(void *c, unsigned ma) { (void)c; (void)ma; return 0; }
static yusb_speed_t g_speed(void *c) { (void)c; return YUSB_SPEED_HIGH; }
static void g_write(void *c, int hd, uint8_t *d, size_t l, bool z) { (void)c; (void)z; if (l) d[l - 1] = d[0]; ep_start(hd); }
static unsigned take(void);
static void g_read(void *c, int hd, uint8_t *d, size_t l)
{
    (void)c;
    if (l) memset(d, 0xa5, l);
    if (l >= 31 && (take() & 3) == 0) {     /* a CBW: WRITE or READ of a fuzzed length */
        static const uint32_t lengths[] = {0, 13, 512, 4096, 65536, 131072, 1u << 20};
        uint32_t n = lengths[take() % 7];
        memset(d, 0, 31);
        memcpy(d, "USBC", 4);
        d[8] = (uint8_t)n; d[9] = (uint8_t)(n >> 8); d[10] = (uint8_t)(n >> 16); d[11] = (uint8_t)(n >> 24);
        d[12] = take() & 1 ? 0x80 : 0;
        d[14] = 10;
    }
    ep_start(hd);
}
static void g_cancel(void *c, int hd) { (void)c; (void)hd; }

static uint32_t be32(const uint8_t *p) { return (uint32_t)p[0] << 24 | (uint32_t)p[1] << 16 | (uint32_t)p[2] << 8 | p[3]; }

/* Collect the core's transmit stream and index the commands in it. */
static void drain_tx(yusb_proxy_t *p)
{
    yusb_iov_t iov[64];
    size_t n;
    while ((n = yusb_proxy_tx_iov(p, iov, 64))) {
        size_t total = 0;
        for (size_t i = 0; i < n; ++i) {
            size_t k = iov[i].len < sizeof(h.stream) - h.have ? iov[i].len : sizeof(h.stream) - h.have;
            memcpy(h.stream + h.have, iov[i].base, k);
            h.have += k;
            total += iov[i].len;
        }
        yusb_proxy_tx_done(p, total);
    }
    size_t pos = 0;
    if (h.have >= YUSB_REQ_IMPORT_SIZE && be32(h.stream) == (YUSB_USBIP_VERSION << 16 | YUSB_OP_REQ_IMPORT))
        pos = YUSB_REQ_IMPORT_SIZE;
    while (h.have - pos >= YUSB_USBIP_HEADER_SIZE) {
        yusb_usbip_hdr_t m;
        uint8_t raw[YUSB_USBIP_HEADER_SIZE];
        memcpy(raw, h.stream + pos, sizeof(raw));
        bool yx = be32(raw) == YUSB_YX_CMD_SUBMIT;
        if (yx) { raw[0] = raw[1] = raw[2] = 0; raw[3] = 1; }
        if (yusb_usbip_decode(raw, &m)) abort();          /* the core only emits valid headers */
        size_t need = sizeof(raw) + (m.command == YUSB_CMD_SUBMIT && !m.direction ? m.length : 0);
#ifdef FUZZ_YX
        if (yx) {
            size_t used = 0;
            if (yusb_yx_decode(&hdec, h.stream + pos + 48, h.have - pos - 48, sink, m.length, &used)) abort();
            need = 48 + used;
        }
#else
        if (yx) abort();                                  /* never without agreement */
#endif
        if (h.have - pos < need) break;
        if (h.nouts < MAX_OUT) {
            out_t *o = &h.outs[h.nouts++];
            *o = (out_t){m.seqnum, m.length, m.unlink_seqnum, m.direction == 1, m.command == YUSB_CMD_UNLINK, true};
        }
        pos += need;
    }
    memmove(h.stream, h.stream + pos, h.have - pos);
    h.have -= pos;
}

static void feed(yusb_proxy_t *p, const uint8_t *bytes, size_t len, uint64_t now)
{
    while (len) {
        uint8_t *buf;
        size_t space = yusb_proxy_rx_space(p, &buf);
        if (!space) return;
        size_t n = len < space ? len : space;
        memcpy(buf, bytes, n);
        if (yusb_proxy_rx_done(p, n, now) < 0) return;
        bytes += n;
        len -= n;
    }
}

static const int32_t statuses[] = {0, 0, 0, -YUSB_EPIPE, -YUSB_ECONNRESET, -YUSB_ENOENT,
                                   -YUSB_EPROTO, -YUSB_ESHUTDOWN, -YUSB_EOVERFLOW};

static void answer(yusb_proxy_t *p, uint64_t now)
{
    if (!h.nouts) return;
    unsigned i = take() % h.nouts;
    out_t o = h.outs[i];
    h.outs[i] = h.outs[--h.nouts];
    uint8_t hdr[YUSB_USBIP_HEADER_SIZE];
    if (o.unlink) {
        int32_t status = statuses[take() % (sizeof(statuses) / sizeof(statuses[0]))];
        yusb_usbip_hdr_t r = yusb_ret_unlink(o.seq, status);
        yusb_usbip_encode(&r, hdr);
        feed(p, hdr, sizeof(hdr), now);
        return;
    }
    int32_t status = statuses[take() % (sizeof(statuses) / sizeof(statuses[0]))];
    uint32_t actual = o.length ? (uint32_t)((take() << 8 | take()) % (o.length + 1)) : 0;
    if (take() & 1) actual = o.length;
    yusb_usbip_hdr_t r = yusb_ret_submit(o.seq, status, actual);
    yusb_usbip_encode(&r, hdr);
#ifdef FUZZ_YX
    unsigned mode = (o.in && actual) ? take() % 4 : 0;
#else
    unsigned mode = 0;
#endif
    if (mode) { hdr[0] = 0x59; hdr[1] = 0x58; hdr[2] = 0; hdr[3] = 3; }
    feed(p, hdr, sizeof(hdr), now);
    if (o.in && actual) {
        static uint8_t payload[1 << 20];
        size_t have = h.size - h.pos;
        size_t n = actual < have ? actual : have;
        memcpy(payload, h.in + h.pos, n);
        memset(payload + n, 0, actual - n);
        h.pos += n;
        if (mode == 0 || mode == 3) feed(p, payload, actual, now);   /* 3: garbage block stream */
#ifdef FUZZ_YX
        else {
            size_t w = yusb_yx_encode(&henc, payload, actual, wirebuf);
            feed(p, wirebuf, w, now);
        }
#endif
    }
}

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)
{
    memset(&h, 0, sizeof(h));
    h.in = data;
    h.size = size;
    yusb_policy_t policy;
    yusb_policy_defaults(&policy);
    unsigned knobs = take();
    policy.out_window = 1 + (knobs & 3);
    policy.bot_out_window = 1 + ((knobs >> 2) & 3);
    policy.coalesce_in = knobs & 16;
    policy.in_depth_bulk = 1 + ((knobs >> 5) & 7);
    bool oracle = knobs & 0x80;
#ifdef FUZZ_YX
    policy.yx_levers = 0x07;
    policy.yx_cache_blocks = 8;
    yusb_yx_enc_free(&henc); yusb_yx_dec_free(&hdec);
    yusb_yx_enc_init(&henc, &AG); yusb_yx_dec_init(&hdec, &AG);
#endif
    yusb_gadget_ops_t ops = {
        .ep0_write = g_ep0_write, .ep0_read = g_ep0_read, .ep0_stall = g_ep0_stall,
        .ep_enable = g_enable, .ep_disable = g_idle, .ep_set_halt = g_idle, .configure = g_none,
        .vbus_draw = g_draw, .link_speed = g_speed, .ep_write = g_write, .ep_read = g_read,
        .ep_cancel = g_cancel,
    };
    uint64_t now = 1000;
    yusb_proxy_t *p = yusb_proxy_create(&policy, &ops, "1-1", now);
    if (!p) return 0;
    uint8_t rep[YUSB_REP_IMPORT_SIZE];
    yusb_device_info_t info;
    memset(&info, 0, sizeof(info));
    strcpy(info.busid, "1-1");
    strcpy(info.path, "/sys/fuzz");
    info.busnum = 1;
    info.devnum = 2;
    info.speed = 1 + take() % 3;
    yusb_op_encode(YUSB_OP_REP_IMPORT, 0, rep);
    yusb_device_info_encode(&info, rep + YUSB_OP_HEADER_SIZE);
#ifdef FUZZ_YX
    yusb_yx_accept_put(rep + YUSB_OP_HEADER_SIZE, &AG);
#endif
    drain_tx(p);
    feed(p, rep, sizeof(rep), now);
    if (oracle) {
        info.speed = YUSB_SPEED_HIGH;
        for (int guard = 0; guard < 16 && yusb_proxy_phase(p) == YUSB_PHASE_FETCH; ++guard) {
            drain_tx(p);
            if (!h.nouts) break;
            out_t o = h.outs[0];
            h.outs[0] = h.outs[--h.nouts];
            uint8_t hdr[YUSB_USBIP_HEADER_SIZE];
            const uint8_t *d = o.length == 18 ? dev_desc : o.length == 9 || o.length == 32 ? bot_cfg : NULL;
            yusb_usbip_hdr_t r = yusb_ret_submit(o.seq, d ? 0 : -YUSB_EPIPE, d ? o.length : 0);
            yusb_usbip_encode(&r, hdr);
            feed(p, hdr, sizeof(hdr), now);
            if (d) feed(p, d, o.length, now);
        }
        if (yusb_proxy_phase(p) == YUSB_PHASE_READY) {
            yusb_proxy_gadget_started(p, NULL, 0, now);
            static const uint8_t setconf[8] = {0, 9, 1, 0, 0, 0, 0, 0};
            yusb_proxy_event(p, take() & 1 ? YUSB_EV_CONTROL_ACKED : YUSB_EV_CONTROL, setconf, now);
        }
    }
    for (unsigned steps = 0; steps < 4000 && h.pos < h.size; ++steps) {
        drain_tx(p);
        if (yusb_proxy_phase(p) == YUSB_PHASE_READY) yusb_proxy_gadget_started(p, NULL, 0, now);
        switch (take() % 8) {
        case 0: case 1: answer(p, now); break;
        case 2: {
            uint8_t setup[8];
            for (int i = 0; i < 8; ++i) setup[i] = (uint8_t)take();
            if (take() & 1) {                       /* often a BOT reset or clear-halt */
                static const uint8_t common[3][8] = {{0x21, 0xff, 0, 0, 0, 0, 0, 0},
                                                     {0x02, 0x01, 0, 0, 0x02, 0, 0, 0},
                                                     {0x02, 0x01, 0, 0, 0x81, 0, 0, 0}};
                memcpy(setup, common[take() % 3], 8);
            }
            unsigned ev = take() % 9;
            yusb_event_t type = ev < 2 ? YUSB_EV_CONTROL : ev == 2 ? YUSB_EV_CONTROL_ACKED : (yusb_event_t)(ev - 2);
            yusb_proxy_event(p, type, setup, now);
            break;
        }
        case 3:
            if (h.ep0_busy) {
                h.ep0_busy = false;
                yusb_proxy_ep0_done(p, take() & 1 ? (int)take() : YUSB_GADGET_ERROR, now);
            }
            break;
        case 4: {
            int hd = (int)(take() % HANDLES);
            if (!h.ep_busy[hd]) break;
            h.ep_busy[hd] = false;
            static const int results[] = {0, 13, 31, 512, 4096, YUSB_GADGET_INTERRUPTED,
                                          YUSB_GADGET_HALT_CLEARED, YUSB_GADGET_SHUTDOWN, YUSB_GADGET_ERROR};
            yusb_proxy_ep_done(p, hd, results[take() % 9], now);
            break;
        }
        case 5: now += take() * 20u; yusb_proxy_poll(p, now); break;
        case 6: {
            uint8_t junk[16];
            size_t n = take() % sizeof(junk);
            for (size_t i = 0; i < n; ++i) junk[i] = (uint8_t)take();
            feed(p, junk, n, now);
            break;
        }
        default: yusb_proxy_poll(p, now); break;
        }
        yusb_proxy_status_t st;
        yusb_proxy_status(p, &st);
        if (!st.state) abort();
    }
    yusb_proxy_stop(p, now);
    for (int round = 0; round < 4 && yusb_proxy_phase(p) != YUSB_PHASE_STOPPED; ++round) {
        if (h.ep0_busy) { h.ep0_busy = false; yusb_proxy_ep0_done(p, YUSB_GADGET_INTERRUPTED, now); }
        for (int hd = 0; hd < HANDLES; ++hd)
            if (h.ep_busy[hd]) { h.ep_busy[hd] = false; yusb_proxy_ep_done(p, hd, YUSB_GADGET_INTERRUPTED, now); }
        yusb_proxy_poll(p, now);
    }
    if (yusb_proxy_phase(p) != YUSB_PHASE_STOPPED) abort();     /* teardown must confirm */
    yusb_proxy_destroy(p);
    return 0;
}
