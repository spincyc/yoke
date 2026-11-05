/* Deterministic unit tests of the native USB core (run by
 * test_native_core.py under ASan/UBSan). Virtual time, scripted peers. */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
#include "rawgadget.h"
#include "yusb_fence.h"
#include "yusb_proxy.h"
#ifdef __has_include
#if __has_include(<linux/usb/raw_gadget.h>)
#include <linux/usb/raw_gadget.h>
#define HAVE_RAW_GADGET_H 1
#endif
#endif

static int failures;
#define CHECK(cond)                                                              \
    do {                                                                         \
        if (!(cond)) {                                                           \
            fprintf(stderr, "%s:%d: CHECK(%s) failed\n", __FILE__, __LINE__, #cond); \
            failures++;                                                          \
        }                                                                        \
    } while (0)

/* --- fence ---------------------------------------------------------------- */

static void test_fence(void)
{
    yusb_fence_cfg_t cfg = {250, 2000, 30000};
    yusb_fence_t f;
    yusb_fence_init(&f, &cfg);
    yusb_target_t t = {.port = 3240};
    strcpy(t.host, "10.0.0.2");
    strcpy(t.busid, "1-1");
    CHECK(f.state == YUSB_W_IDLE && !yusb_fence_due(&f, false, 0));
    CHECK(yusb_fence_newer(&f, 1) && !yusb_fence_newer(&f, 0));
    yusb_fence_adopt(&f, 3, &t);
    CHECK(f.state == YUSB_W_CONNECTING && !yusb_fence_newer(&f, 3) && yusb_fence_due(&f, false, 0));
    /* Backoff doubles from retry_min to retry_max. */
    uint32_t expect[] = {250, 500, 1000, 2000, 2000};
    for (unsigned i = 0; i < 5; ++i) {
        yusb_fence_build_failed(&f, YUSB_BUILD_RETRY, "refused", 1000);
        CHECK(f.next_attempt_ms == 1000 + expect[i]);
        CHECK(!yusb_fence_due(&f, false, 1000) && yusb_fence_due(&f, false, 1000 + expect[i]));
    }
    yusb_fence_build_failed(&f, YUSB_BUILD_REJECTED, "unsupported device: hub", 5000);
    CHECK(f.state == YUSB_W_REJECTED && f.next_attempt_ms == 35000);
    yusb_fence_built(&f);
    CHECK(f.state == YUSB_W_PRESENTING && f.backoff_ms == 250);
    yusb_fence_observe(&f, YUSB_W_CONFIGURED, "", "");
    CHECK(f.state == YUSB_W_CONFIGURED && !f.detail[0]);
    yusb_fence_observe(&f, YUSB_W_CONFIGURED, "computer link is full-speed", "");
    CHECK(f.state == YUSB_W_REJECTED);
    yusb_fence_ended(&f, NULL, "remote lost: closed", 9000);
    CHECK(f.state == YUSB_W_CONNECTING && f.reconnects == 1 && f.next_attempt_ms == 9250);
    CHECK(!strcmp(f.detail, "session ended: remote lost: closed"));
    yusb_fence_hold(&f, "teardown unconfirmed");
    CHECK(f.state == YUSB_W_ERROR && f.generation == 3);   /* the fence holds */
    yusb_fence_adopt(&f, 4, NULL);
    CHECK(f.state == YUSB_W_IDLE && !f.has_target && !yusb_fence_due(&f, false, 99999));
    CHECK(yusb_wstate_from_name("bogus") == YUSB_W_PRESENTING);
    CHECK(!strcmp(yusb_wstate_name(YUSB_W_SUSPENDED), "suspended"));
}

/* --- a scripted exporter for the proxy ------------------------------------ */

typedef struct {
    uint32_t seq, length, victim;
    bool in, unlink;
    uint8_t head[4];          /* first OUT payload bytes */
    unsigned ep;
} cmd_t;

static struct {
    cmd_t cmds[512];
    unsigned n;
    uint8_t stream[1 << 18];
    size_t have;
    bool ep0_busy, ep_busy[32];
    int ep0_last;
    unsigned halts, enables, ep0_ops;
    uint8_t *ep0_data, *rd[32];
    size_t ep0_len, rdlen[32];
} sx;

static size_t want_align;
static unsigned misaligned, checked;

static void note_ptr(const void *p)
{
    checked++;
    if (want_align > 1 && (uintptr_t)p % want_align) misaligned++;
}

/* Backend contract: one ep0 operation at a time. */
static void ep0_start(int kind)
{
    CHECK(!sx.ep0_busy);
    sx.ep0_busy = true;
    sx.ep0_last = kind;
    sx.ep0_ops++;
}
static void s_ep0_write(void *c, uint8_t *d, size_t l, bool z) { (void)c; (void)z; note_ptr(d); sx.ep0_data = d; sx.ep0_len = l; ep0_start(1); }
static void s_ep0_read(void *c, uint8_t *d, size_t l) { (void)c; note_ptr(d); sx.ep0_data = d; sx.ep0_len = l; ep0_start(2); }
static void s_ep0_stall(void *c) { (void)c; ep0_start(3); }
static int s_enable(void *c, const yusb_endpoint_t *e) { (void)c; sx.enables++; return yusb_ep_number(e) + (yusb_ep_is_in(e) ? 16 : 0); }
static int s_disable(void *c, int h) { (void)c; (void)h; return 0; }
static int s_halt(void *c, int h) { (void)c; (void)h; sx.halts++; return 0; }
static int s_none(void *c) { (void)c; return 0; }
static int s_draw(void *c, unsigned m) { (void)c; (void)m; return 0; }
static yusb_speed_t s_speed(void *c) { (void)c; return YUSB_SPEED_HIGH; }
static void s_write(void *c, int h, uint8_t *d, size_t l, bool z) { (void)c; (void)l; (void)z; note_ptr(d); sx.ep_busy[h] = true; }
static void s_read(void *c, int h, uint8_t *d, size_t l) { (void)c; note_ptr(d); sx.rd[h] = d; sx.rdlen[h] = l; sx.ep_busy[h] = true; }
static void s_cancel(void *c, int h) { (void)c; (void)h; }

static void *aligned_hook(void *ctx, size_t size, size_t align)
{
    (void)ctx;
    void *p = aligned_alloc(align, size);
    if (p) memset(p, 0, size);
    return p;
}

static void free_hook(void *ctx, void *p) { (void)ctx; free(p); }

static yusb_gadget_ops_t scripted_ops(const yusb_buffers_t *buffers)
{
    yusb_gadget_ops_t ops = {
        .ep0_write = s_ep0_write, .ep0_read = s_ep0_read, .ep0_stall = s_ep0_stall,
        .ep_enable = s_enable, .ep_disable = s_disable, .ep_set_halt = s_halt,
        .configure = s_none, .vbus_draw = s_draw, .link_speed = s_speed, .ep_write = s_write,
        .ep_read = s_read, .ep_cancel = s_cancel, .buffers = buffers,
    };
    return ops;
}

static uint32_t be32(const uint8_t *p) { return (uint32_t)p[0] << 24 | (uint32_t)p[1] << 16 | (uint32_t)p[2] << 8 | p[3]; }

static void collect(yusb_proxy_t *p)
{
    yusb_iov_t iov[64];
    size_t n;
    while ((n = yusb_proxy_tx_iov(p, iov, 64))) {
        size_t total = 0;
        for (size_t i = 0; i < n; ++i) {
            memcpy(sx.stream + sx.have, iov[i].base, iov[i].len);
            sx.have += iov[i].len;
            total += iov[i].len;
        }
        yusb_proxy_tx_done(p, total);
    }
    size_t pos = 0;
    if (sx.have >= 40 && be32(sx.stream) == (0x0111u << 16 | 0x8003u)) pos = 40;
    while (sx.have - pos >= 48) {
        yusb_usbip_hdr_t h;
        if (yusb_usbip_decode(sx.stream + pos, &h)) { failures++; return; }
        size_t need = 48 + (h.command == YUSB_CMD_SUBMIT && !h.direction ? h.length : 0);
        if (sx.have - pos < need) break;
        cmd_t c = {h.seqnum, h.length, h.unlink_seqnum, h.direction == 1,
                   h.command == YUSB_CMD_UNLINK, {0}, h.ep};
        if (need > 48) memcpy(c.head, sx.stream + pos + 48, need - 48 < 4 ? need - 48 : 4);
        sx.cmds[sx.n++] = c;
        pos += need;
    }
    memmove(sx.stream, sx.stream + pos, sx.have - pos);
    sx.have -= pos;
}

static void feed(yusb_proxy_t *p, const uint8_t *data, size_t len, uint64_t now)
{
    while (len) {
        uint8_t *buf;
        size_t space = yusb_proxy_rx_space(p, &buf);
        if (!space) { failures++; return; }
        size_t n = len < space ? len : space;
        memcpy(buf, data, n);
        CHECK(yusb_proxy_rx_done(p, n, now) == YUSB_OK);
        data += n;
        len -= n;
    }
}

static void reply(yusb_proxy_t *p, uint32_t seq, int32_t status, const uint8_t *data, uint32_t len,
                  uint64_t now)
{
    uint8_t raw[48];
    yusb_usbip_hdr_t h = yusb_ret_submit(seq, status, len);
    yusb_usbip_encode(&h, raw);
    feed(p, raw, 48, now);
    if (data && len) feed(p, data, len, now);
}

static void unlink_reply(yusb_proxy_t *p, uint32_t seq, int32_t status, uint64_t now)
{
    uint8_t raw[48];
    yusb_usbip_hdr_t h = yusb_ret_unlink(seq, status);
    yusb_usbip_encode(&h, raw);
    feed(p, raw, 48, now);
}

static cmd_t take_cmd(void)
{
    cmd_t c = sx.cmds[0];
    memmove(sx.cmds, sx.cmds + 1, (--sx.n) * sizeof(sx.cmds[0]));
    return c;
}

/* A high-speed vendor device: one interface, bulk 0x81 and 0x02. */
static const uint8_t dev_desc[18] = {18, 1, 0, 2, 0, 0, 0, 64, 0x09, 0x12, 0x99, 0, 0, 1, 0, 0, 0, 1};
static const uint8_t cfg_desc[32] = {9, 2, 32, 0, 1, 1, 0, 0x80, 50, 9, 4, 0, 0, 2, 0xff, 0, 0, 0,
                                     7, 5, 0x81, 2, 0, 2, 0, 7, 5, 0x02, 2, 0, 2, 0};

static const uint8_t bot_cfg[32] = {9, 2, 32, 0, 1, 1, 0, 0x80, 50, 9, 4, 0, 0, 2, 0x08, 0x06, 0x50, 0,
                                    7, 5, 0x81, 2, 0, 2, 0, 7, 5, 0x02, 2, 0, 2, 0};

/* Import and answer the descriptor fetch (configuration `cfg`), then bind. */
static yusb_proxy_t *ready(const yusb_policy_t *policy, const yusb_gadget_ops_t *ops,
                           const uint8_t *cfg, uint64_t now)
{
    memset(&sx, 0, sizeof(sx));
    yusb_proxy_t *p = yusb_proxy_create(policy, ops, "1-1", now);
    collect(p);
    uint8_t rep[YUSB_REP_IMPORT_SIZE] = {0};
    yusb_op_encode(YUSB_OP_REP_IMPORT, 0, rep);
    yusb_device_info_t info = {.busnum = 1, .devnum = 2, .speed = YUSB_SPEED_HIGH};
    strcpy(info.busid, "1-1");
    yusb_device_info_encode(&info, rep + 8);
    feed(p, rep, sizeof(rep), now);
    for (int guard = 0; guard < 64 && yusb_proxy_phase(p) == YUSB_PHASE_FETCH; ++guard) {
        collect(p);
        if (!sx.n) break;
        cmd_t c = take_cmd();
        /* Answer from the setup the core put in its header: re-derive it by
         * length (device 18, config 9 then 32, qualifier 10: stall). */
        if (c.length == 18) reply(p, c.seq, 0, dev_desc, 18, now);
        else if (c.length == 9) reply(p, c.seq, 0, cfg, 9, now);
        else if (c.length == 32) reply(p, c.seq, 0, cfg, 32, now);
        else reply(p, c.seq, -YUSB_EPIPE, NULL, 0, now);
    }
    CHECK(yusb_proxy_phase(p) == YUSB_PHASE_READY);
    yusb_proxy_gadget_started(p, NULL, 0, now);
    return p;
}

/* Import, answer the descriptor fetch, bind, configure. */
static yusb_proxy_t *presenting_cfg(const yusb_policy_t *policy, const yusb_gadget_ops_t *ops,
                                    const uint8_t *cfg, uint64_t now)
{
    yusb_proxy_t *p = ready(policy, ops, cfg, now);
    uint8_t setconf[8] = {0, 9, 1, 0, 0, 0, 0, 0};
    yusb_proxy_event(p, YUSB_EV_CONTROL, setconf, now);
    collect(p);
    CHECK(sx.n == 1);           /* SET_CONFIGURATION forwarded */
    reply(p, take_cmd().seq, 0, NULL, 0, now);
    collect(p);
    CHECK(sx.ep0_busy && sx.ep0_last == 2);
    sx.ep0_busy = false;
    yusb_proxy_ep0_done(p, 0, now);
    collect(p);
    return p;
}

static yusb_proxy_t *presenting(const yusb_policy_t *policy, const yusb_gadget_ops_t *ops, uint64_t now)
{
    return presenting_cfg(policy, ops, cfg_desc, now);
}

static void test_unlink_grace_and_tombstones(void)
{
    yusb_policy_t policy;
    yusb_policy_defaults(&policy);
    policy.in_depth_bulk = 2;
    yusb_gadget_ops_t ops = scripted_ops(NULL);
    uint64_t now = 1000;
    yusb_proxy_t *p = presenting(&policy, &ops, now);
    CHECK(sx.n == 2 && sx.cmds[0].in && sx.cmds[1].in);   /* two IN URBs on 0x81 */
    cmd_t a = sx.cmds[0], b = sx.cmds[1];
    /* Bus reset: both IN URBs are unlinked. */
    sx.n = 0;
    yusb_proxy_event(p, YUSB_EV_RESET, NULL, now);
    CHECK(sx.ep_busy[2]);                 /* the OUT read was cancelled: complete it */
    sx.ep_busy[2] = false;
    yusb_proxy_ep_done(p, 2, YUSB_GADGET_INTERRUPTED, now);
    collect(p);
    CHECK(sx.n == 2 && sx.cmds[0].unlink && sx.cmds[0].victim == a.seq && sx.cmds[1].victim == b.seq);
    cmd_t ua = take_cmd(), ub = take_cmd();
    /* stub_tx order: RET_UNLINK(0) for a, then its late RET_SUBMIT. */
    unlink_reply(p, ua.seq, 0, now);
    uint8_t data[4] = {1, 2, 3, 4};
    reply(p, a.seq, 0, data, 4, now);
    /* b: cancelled; a straggling RET_SUBMIT is consumed via the tombstone. */
    unlink_reply(p, ub.seq, -YUSB_ECONNRESET, now);
    reply(p, b.seq, 0, data, 4, now);
    const yusb_proxy_counters_t *c = yusb_proxy_counters(p);
    CHECK(c->late_ret_submits == 2 && c->unlinks == 2);
    CHECK(yusb_proxy_phase(p) == YUSB_PHASE_PRESENTING);
    /* An unknown RET_SUBMIT is still a protocol error. */
    uint8_t raw[48], *buf;
    yusb_usbip_hdr_t unknown = yusb_ret_submit(0xdead, 0, 0);
    yusb_usbip_encode(&unknown, raw);
    CHECK(yusb_proxy_rx_space(p, &buf) >= 48);
    memcpy(buf, raw, 48);
    CHECK(yusb_proxy_rx_done(p, 48, now) == YUSB_ERR_PROTOCOL);
    yusb_proxy_status_t st;
    yusb_proxy_status(p, &st);
    CHECK(st.end == YUSB_END_REMOTE_LOST && strstr(st.end_reason, "unknown seqnum"));
    yusb_proxy_stop(p, now);
    CHECK(yusb_proxy_phase(p) == YUSB_PHASE_STOPPED);
    yusb_proxy_destroy(p);
}

static void test_in_stall_parks_with_backoff(void)
{
    yusb_policy_t policy;
    yusb_policy_defaults(&policy);
    policy.in_depth_bulk = 1;
    yusb_gadget_ops_t ops = scripted_ops(NULL);
    uint64_t now = 1000;
    yusb_proxy_t *p = presenting(&policy, &ops, now);
    unsigned clears = 0;
    uint64_t parked_for[3] = {0};
    for (int round = 0; round < 4; ++round) {
        collect(p);
        CHECK(sx.n == 1 && sx.cmds[0].in);
        reply(p, take_cmd().seq, -YUSB_EPIPE, NULL, 0, now);   /* the device stalls */
        collect(p);
        if (sx.n == 1 && !sx.cmds[0].in && sx.cmds[0].length == 0) {
            clears++;                                          /* clear-halt control */
            reply(p, take_cmd().seq, 0, NULL, 0, now);
            continue;
        }
        /* Stalled right after a clear: parked; nothing is submitted until
         * the backoff expires (50, 100, 200 ms). */
        CHECK(sx.n == 0);
        uint64_t next = yusb_proxy_poll(p, now);
        if (round - 1 >= 0 && round - 1 < 3) parked_for[round - 1] = next - now;
        now = next;
        yusb_proxy_poll(p, now);
        collect(p);
        CHECK(sx.n == 1 && sx.cmds[0].length == 0);            /* unpark: clear again */
        reply(p, take_cmd().seq, 0, NULL, 0, now);
    }
    const yusb_proxy_counters_t *c = yusb_proxy_counters(p);
    CHECK(clears == 1 && c->parked == 3 && sx.halts == 4 && c->remote_stalls == 4);
    CHECK(parked_for[0] == 50 && parked_for[1] == 100 && parked_for[2] == 200);
    yusb_proxy_stop(p, now);
    yusb_proxy_destroy(p);
}

static void test_dma_layout_aligns_every_backend_buffer(void)
{
    yusb_policy_t policy;
    yusb_policy_defaults(&policy);
    yusb_buffers_t hook = {64, aligned_hook, free_hook, NULL};
    yusb_gadget_ops_t ops = scripted_ops(&hook);
    want_align = 64;
    misaligned = checked = 0;
    yusb_proxy_t *p = presenting(&policy, &ops, 1000);   /* ep0 ack, OUT read */
    uint8_t get[8] = {0x80, 6, 0, 1, 0, 0, 18, 0};
    yusb_proxy_event(p, YUSB_EV_CONTROL, get, 1000);   /* cached reply: ep0 buffer */
    CHECK(sx.ep0_busy);
    /* One IN completion is written from its slot; two coalesce via scratch. */
    uint8_t packet[512] = {7};
    reply(p, sx.cmds[0].seq, 0, packet, 100, 1000);
    CHECK(sx.ep_busy[17]);
    sx.ep_busy[17] = false;
    reply(p, sx.cmds[1].seq, 0, packet, 512, 1000);
    reply(p, sx.cmds[2].seq, 0, packet, 100, 1000);
    yusb_proxy_ep_done(p, 17, 100, 1000);
    CHECK(sx.ep_busy[17]);
    CHECK(checked >= 5 && misaligned == 0);
    want_align = 0;
    yusb_proxy_stop(p, 1000);
    yusb_proxy_destroy(p);
}


/* --- endpoint zero: requests that must finish (review A, Linux H1/H2) --- */

/* Control commands only (pumps submit too); unlinks count as control. */
static unsigned count_ep0(void)
{
    unsigned k = 0;
    for (unsigned i = 0; i < sx.n; ++i) k += sx.cmds[i].unlink || sx.cmds[i].ep == 0;
    return k;
}

static cmd_t take_ep0(void)
{
    for (unsigned i = 0; i < sx.n; ++i) {
        if (!sx.cmds[i].unlink && sx.cmds[i].ep) continue;
        cmd_t c = sx.cmds[i];
        memmove(sx.cmds + i, sx.cmds + i + 1, (--sx.n - i) * sizeof(sx.cmds[0]));
        return c;
    }
    failures++;
    return (cmd_t){0};
}

static void ep0_finish(yusb_proxy_t *p, int result, uint64_t now)
{
    CHECK(sx.ep0_busy);
    sx.ep0_busy = false;
    yusb_proxy_ep0_done(p, result, now);
    collect(p);
}

/* A UDC that acked SET_CONFIGURATION itself lets the host's next SETUP in
 * while the core still forwards it: the configuration still happens, the
 * next request waits, and every ep0 operation answers the newest SETUP
 * (none for SET_CONFIGURATION), with the explicit flag and without. */
static void test_set_configuration_survives_the_next_setup(void)
{
    for (int acked = 0; acked < 2; ++acked) {
        yusb_policy_t policy;
        yusb_policy_defaults(&policy);
        yusb_gadget_ops_t ops = scripted_ops(NULL);
        uint64_t now = 1000;
        yusb_proxy_t *p = ready(&policy, &ops, cfg_desc, now);
        uint8_t setconf[8] = {0, 9, 1, 0, 0, 0, 0, 0};
        yusb_proxy_event(p, acked ? YUSB_EV_CONTROL_ACKED : YUSB_EV_CONTROL, setconf, now);
        collect(p);
        CHECK(sx.n == 1);
        cmd_t sc = take_cmd();
        uint8_t next[8] = {0x40, 0x0a, 0, 0, 0, 0, 0, 0};   /* vendor OUT, no data */
        yusb_proxy_event(p, YUSB_EV_CONTROL, next, now);
        collect(p);
        CHECK(sx.n == 0 && sx.ep0_ops == 0);                 /* waits; nothing unlinked */
        reply(p, sc.seq, 0, NULL, 0, now);
        collect(p);
        yusb_proxy_status_t st;
        yusb_proxy_status(p, &st);
        CHECK(sx.enables == 2 && st.configuration == 1);
        CHECK(!sx.ep0_busy && count_ep0() == 1);           /* no answer for it */
        cmd_t v = take_ep0();
        CHECK(!v.unlink && !v.in);
        reply(p, v.seq, 0, NULL, 0, now);
        collect(p);
        ep0_finish(p, 0, now);
        CHECK(sx.ep0_ops == 1);
        yusb_proxy_stop(p, now);
        yusb_proxy_destroy(p);
    }
}

/* SET_CONFIGURATION delivered as acked gets no ep0 operation at all. */
static void test_acked_set_configuration_gets_no_ep0_answer(void)
{
    yusb_policy_t policy;
    yusb_policy_defaults(&policy);
    yusb_gadget_ops_t ops = scripted_ops(NULL);
    uint64_t now = 1000;
    yusb_proxy_t *p = ready(&policy, &ops, cfg_desc, now);
    uint8_t setconf[8] = {0, 9, 1, 0, 0, 0, 0, 0};
    yusb_proxy_event(p, YUSB_EV_CONTROL_ACKED, setconf, now);
    collect(p);
    reply(p, take_ep0().seq, 0, NULL, 0, now);
    collect(p);
    yusb_proxy_status_t st;
    yusb_proxy_status(p, &st);
    CHECK(sx.enables == 2 && st.configuration == 1 && sx.ep0_ops == 0);
    yusb_proxy_stop(p, now);
    yusb_proxy_destroy(p);
    /* Refused by the device: only counted, still no ep0 operation. */
    p = ready(&policy, &ops, cfg_desc, now);
    yusb_proxy_event(p, YUSB_EV_CONTROL_ACKED, setconf, now);
    collect(p);
    reply(p, take_ep0().seq, -YUSB_EPIPE, NULL, 0, now);
    collect(p);
    CHECK(sx.ep0_ops == 0 && yusb_proxy_counters(p)->late_out_stalls == 1);
    yusb_proxy_stop(p, now);
    yusb_proxy_destroy(p);
}

/* A request the host abandoned while still owed its answer (a UDC that
 * delivers the next SETUP) is unlinked, so orphans cannot fill the table. */
static void test_abandoned_request_is_unlinked(void)
{
    yusb_policy_t policy;
    yusb_policy_defaults(&policy);
    yusb_gadget_ops_t ops = scripted_ops(NULL);
    uint64_t now = 1000;
    yusb_proxy_t *p = presenting(&policy, &ops, now);
    sx.n = 0;
    sx.ep0_ops = 0;
    uint8_t vendor[8] = {0xc0, 0x01, 0, 0, 0, 0, 64, 0};
    yusb_proxy_event(p, YUSB_EV_CONTROL, vendor, now);
    collect(p);
    CHECK(sx.n == 1);
    cmd_t first = take_cmd();
    yusb_proxy_event(p, YUSB_EV_CONTROL, vendor, now);
    collect(p);
    CHECK(sx.n == 2 && sx.cmds[0].unlink && sx.cmds[0].victim == first.seq && !sx.cmds[1].unlink);
    cmd_t unlink = take_cmd(), second = take_cmd();
    unlink_reply(p, unlink.seq, -YUSB_ECONNRESET, now);
    uint8_t answer[64];
    memset(answer, 0x5a, sizeof(answer));
    reply(p, second.seq, 0, answer, 64, now);
    collect(p);
    CHECK(sx.ep0_busy && sx.ep0_last == 1 && sx.ep0_len == 64);
    ep0_finish(p, 64, now);
    yusb_proxy_stop(p, now);
    yusb_proxy_destroy(p);
}

static const uint8_t out_data[4] = {1, 2, 3, 4};

/* Linux H1: the UDC acked an OUT-with-data request's status stage when the
 * data arrived, so the host's next SETUP can be seen before the read's
 * completion. The data must still reach the device. */
static void test_out_data_survives_an_early_setup(void)
{
    yusb_policy_t policy;
    yusb_policy_defaults(&policy);
    yusb_gadget_ops_t ops = scripted_ops(NULL);
    uint64_t now = 1000;
    yusb_proxy_t *p = presenting(&policy, &ops, now);
    sx.n = 0;
    sx.ep0_ops = 0;
    uint8_t out[8] = {0x40, 0x01, 0, 0, 0, 0, 4, 0};
    uint8_t in[8] = {0xc0, 0x02, 0, 0, 0, 0, 4, 0};
    yusb_proxy_event(p, YUSB_EV_CONTROL, out, now);
    CHECK(sx.ep0_busy && sx.ep0_last == 2 && sx.ep0_len == 4);
    memcpy(sx.ep0_data, out_data, 4);
    yusb_proxy_event(p, YUSB_EV_CONTROL, in, now);    /* before EP0_READ returns */
    collect(p);
    CHECK(sx.n == 0);
    ep0_finish(p, 4, now);
    CHECK(sx.n == 1 && !sx.cmds[0].in && sx.cmds[0].length == 4 && !memcmp(sx.cmds[0].head, out_data, 4));
    reply(p, take_cmd().seq, 0, NULL, 0, now);
    collect(p);
    CHECK(!sx.ep0_busy);                               /* nothing more owed on ep0 */
    CHECK(sx.n == 1 && sx.cmds[0].in && sx.cmds[0].length == 4);
    reply(p, take_cmd().seq, 0, out_data, 4, now);
    collect(p);
    CHECK(sx.ep0_busy && sx.ep0_last == 1);
    ep0_finish(p, 4, now);
    CHECK(sx.ep0_ops == 2);
    yusb_proxy_stop(p, now);
    yusb_proxy_destroy(p);
}

/* Linux H1, gate: the same while the request waits for a remote reset. */
static void test_out_data_survives_a_setup_during_a_remote_reset(void)
{
    yusb_policy_t policy;
    yusb_policy_defaults(&policy);
    yusb_gadget_ops_t ops = scripted_ops(NULL);
    uint64_t now = 1000;
    yusb_proxy_t *p = ready(&policy, &ops, cfg_desc, now);
    uint8_t in[8] = {0xc0, 0x02, 0, 0, 0, 0, 4, 0};
    uint8_t out[8] = {0x40, 0x01, 0, 0, 0, 0, 4, 0};
    yusb_proxy_event(p, YUSB_EV_CONTROL, in, now);    /* the remote is now dirty */
    collect(p);
    reply(p, take_cmd().seq, 0, out_data, 4, now);
    collect(p);
    ep0_finish(p, 4, now);
    yusb_proxy_event(p, YUSB_EV_RESET, NULL, now);
    collect(p);
    CHECK(sx.n == 1);                                  /* SET_FEATURE(PORT_RESET) */
    cmd_t reset = take_cmd();
    yusb_proxy_event(p, YUSB_EV_CONTROL, out, now);
    memcpy(sx.ep0_data, out_data, 4);
    ep0_finish(p, 4, now);
    CHECK(sx.n == 0);                                  /* gated behind the reset */
    yusb_proxy_event(p, YUSB_EV_CONTROL, in, now);
    collect(p);
    CHECK(sx.n == 0 && !sx.ep0_busy);
    reply(p, reset.seq, 0, NULL, 0, now);
    collect(p);
    CHECK(sx.n == 1 && !sx.cmds[0].in && !memcmp(sx.cmds[0].head, out_data, 4));
    reply(p, take_cmd().seq, 0, NULL, 0, now);
    collect(p);
    CHECK(sx.n == 1 && sx.cmds[0].in);
    reply(p, take_cmd().seq, 0, out_data, 4, now);
    collect(p);
    ep0_finish(p, 4, now);
    yusb_proxy_stop(p, now);
    yusb_proxy_destroy(p);
}

/* Linux H2: Raw Gadget keeps a SETUP pending across a bus reset or
 * disconnect, refusing every later one: the core releases it with a STALL,
 * after the outstanding ep0 operation if there is one. */
static void test_bus_reset_releases_an_owed_setup(void)
{
    for (int disconnect = 0; disconnect < 2; ++disconnect) {
        yusb_event_t ev = disconnect ? YUSB_EV_DISCONNECT : YUSB_EV_RESET;
        yusb_policy_t policy;
        yusb_policy_defaults(&policy);
        yusb_gadget_ops_t ops = scripted_ops(NULL);
        uint64_t now = 1000;
        yusb_proxy_t *p = presenting(&policy, &ops, now);
        uint8_t vendor[8] = {0xc0, 0x01, 0, 0, 0, 0, 64, 0};
        yusb_proxy_event(p, YUSB_EV_CONTROL, vendor, now);   /* forwarded, owed */
        collect(p);
        CHECK(!sx.ep0_busy);
        yusb_proxy_event(p, ev, NULL, now);
        collect(p);
        CHECK(sx.ep0_busy && sx.ep0_last == 3);
        ep0_finish(p, 0, now);
        /* Owed while an OUT data stage is still being read: STALL after it. */
        uint8_t out[8] = {0x40, 0x01, 0, 0, 0, 0, 4, 0};
        yusb_proxy_event(p, YUSB_EV_CONTROL, out, now);
        yusb_proxy_event(p, YUSB_EV_CONTROL, vendor, now);   /* waits behind it */
        yusb_proxy_event(p, ev, NULL, now);
        CHECK(sx.ep0_busy && sx.ep0_last == 2);
        ep0_finish(p, -5, now);                             /* flushed by the reset */
        CHECK(sx.ep0_busy && sx.ep0_last == 3);
        ep0_finish(p, 0, now);
        /* Nothing owed: no STALL. */
        unsigned ops_before = sx.ep0_ops;
        yusb_proxy_event(p, ev, NULL, now);
        collect(p);
        CHECK(sx.ep0_ops == ops_before);
        yusb_proxy_stop(p, now);
        yusb_proxy_destroy(p);
    }
}

/* Review B: a forwarded control IN times out; its late RET_SUBMIT must not
 * land in ep0buf while the next request is answered from it. */
static void test_late_control_reply_does_not_overwrite_ep0(void)
{
    yusb_policy_t policy;
    yusb_policy_defaults(&policy);
    yusb_gadget_ops_t ops = scripted_ops(NULL);
    uint64_t now = 1000;
    yusb_proxy_t *p = presenting(&policy, &ops, now);
    sx.n = 0;
    sx.ep0_ops = 0;
    uint8_t vendor[8] = {0xc0, 0x01, 0, 0, 0, 0, 64, 0};
    yusb_proxy_event(p, YUSB_EV_CONTROL, vendor, now);
    collect(p);
    cmd_t c1 = sx.cmds[sx.n - 1];
    sx.n = 0;
    now += policy.control_timeout_ms + 1;
    yusb_proxy_poll(p, now);                        /* times out: STALL, CMD_UNLINK */
    collect(p);
    ep0_finish(p, 0, now);
    uint8_t get[8] = {0x80, 6, 0, 1, 0, 0, 18, 0};
    yusb_proxy_event(p, YUSB_EV_CONTROL, get, now); /* cached: ep0_write from ep0buf */
    CHECK(sx.ep0_busy && sx.ep0_last == 1 && sx.ep0_len == 18);
    uint8_t *held = sx.ep0_data;
    uint8_t late[64];
    memset(late, 0xee, sizeof(late));
    reply(p, c1.seq, 0, late, 64, now);             /* the completion won the unlink race */
    CHECK(!memcmp(held, dev_desc, 18));
    ep0_finish(p, 18, now);
    yusb_proxy_stop(p, now);
    yusb_proxy_destroy(p);
}

/* Review M4, OUT: an abandoned request's payload may still be queued for
 * transmission from ep0buf; the next request waits until it is sent. */
static void test_ep0_buffer_waits_for_an_unsent_out_payload(void)
{
    yusb_policy_t policy;
    yusb_policy_defaults(&policy);
    yusb_gadget_ops_t ops = scripted_ops(NULL);
    uint64_t now = 1000;
    yusb_proxy_t *p = presenting(&policy, &ops, now);
    sx.n = 0;
    sx.ep0_ops = 0;
    uint8_t out[8] = {0x40, 0x01, 0, 0, 0, 0, 4, 0};
    yusb_proxy_event(p, YUSB_EV_CONTROL, out, now);
    memcpy(sx.ep0_data, out_data, 4);
    sx.ep0_busy = false;
    yusb_proxy_ep0_done(p, 4, now);                 /* forwarded; not yet sent */
    now += policy.control_timeout_ms + 1;
    yusb_proxy_poll(p, now);                        /* abandoned */
    uint8_t get[8] = {0x80, 6, 0, 1, 0, 0, 18, 0};
    yusb_proxy_event(p, YUSB_EV_CONTROL, get, now);
    CHECK(!sx.ep0_busy);                            /* ep0buf still being sent */
    collect(p);
    CHECK(sx.n >= 1 && !sx.cmds[0].in && !memcmp(sx.cmds[0].head, out_data, 4));
    CHECK(sx.ep0_busy && sx.ep0_last == 1 && sx.ep0_len == 18);
    ep0_finish(p, 18, now);
    yusb_proxy_stop(p, now);
    yusb_proxy_destroy(p);
}


/* GET_STATUS(endpoint) reports a halt the proxy set locally after a remote
 * STALL, until the host clears it. */
static void test_get_status_reports_a_local_halt(void)
{
    yusb_policy_t policy;
    yusb_policy_defaults(&policy);
    yusb_gadget_ops_t ops = scripted_ops(NULL);
    uint64_t now = 1000;
    yusb_proxy_t *p = presenting(&policy, &ops, now);
    sx.n = 0;
    uint8_t status[8] = {0x82, 0, 0, 0, 0x02, 0, 2, 0};
    yusb_proxy_event(p, YUSB_EV_CONTROL, status, now);
    CHECK(sx.ep0_busy && sx.ep0_last == 1 && sx.ep0_len == 2 && sx.ep0_data[0] == 0);
    ep0_finish(p, 2, now);
    CHECK(sx.ep_busy[2]);
    sx.ep_busy[2] = false;
    yusb_proxy_ep_done(p, 2, 512, now);
    collect(p);
    cmd_t d = take_cmd();
    reply(p, d.seq, -YUSB_EPIPE, NULL, 0, now);     /* the device stalls bulk-out */
    collect(p);
    CHECK(sx.halts == 1);
    reply(p, take_ep0().seq, 0, NULL, 0, now);       /* remote clear */
    collect(p);
    yusb_proxy_event(p, YUSB_EV_CONTROL, status, now);
    CHECK(sx.ep0_busy && sx.ep0_len == 2 && sx.ep0_data[0] == 1);
    ep0_finish(p, 2, now);
    uint8_t clear[8] = {0x02, 1, 0, 0, 0x02, 0, 0, 0};
    yusb_proxy_event(p, YUSB_EV_CONTROL, clear, now);
    collect(p);
    reply(p, take_ep0().seq, 0, NULL, 0, now);
    collect(p);
    ep0_finish(p, 0, now);
    yusb_proxy_event(p, YUSB_EV_CONTROL, status, now);
    CHECK(sx.ep0_busy && sx.ep0_len == 2 && sx.ep0_data[0] == 0);
    ep0_finish(p, 2, now);
    yusb_proxy_stop(p, now);
    yusb_proxy_destroy(p);
}

/* --- BOT after a data-out STALL (review C) ------------------------------- */

static void cbw(uint8_t out[31], uint32_t length, bool in)
{
    memset(out, 0, 31);
    memcpy(out, "USBC", 4);
    out[4] = 0x11;
    out[8] = (uint8_t)length;
    out[9] = (uint8_t)(length >> 8);
    out[10] = (uint8_t)(length >> 16);
    out[11] = (uint8_t)(length >> 24);
    out[12] = in ? 0x80 : 0;
    out[14] = 10;
}

static unsigned count_in(void)
{
    unsigned k = 0;
    for (unsigned i = 0; i < sx.n; ++i) k += sx.cmds[i].in && !sx.cmds[i].unlink;
    return k;
}

/* A WRITE whose data-out the device STALLs; after recovery (or a BOT
 * reset) the next CBW must be read as a CBW, not as planned data. */
static void test_bot_plan_ends_with_the_data_out_phase(void)
{
    for (int bot_reset = 0; bot_reset < 2; ++bot_reset) {
        yusb_policy_t policy;
        yusb_policy_defaults(&policy);
        yusb_gadget_ops_t ops = scripted_ops(NULL);
        uint64_t now = 1000;
        yusb_proxy_t *p = presenting_cfg(&policy, &ops, bot_cfg, now);
        sx.n = 0;
        CHECK(sx.ep_busy[2]);
        cbw(sx.rd[2] + 0, 131072, false);           /* WRITE, 128 KiB */
        sx.ep_busy[2] = false;
        yusb_proxy_ep_done(p, 2, 31, now);
        collect(p);
        cmd_t cbw_urb = take_cmd();
        CHECK(sx.n == 1 && sx.cmds[0].in);          /* the CSW */
        cmd_t csw_urb = take_cmd();
        reply(p, cbw_urb.seq, 0, NULL, 0, now);
        collect(p);
        CHECK(sx.ep_busy[2] && sx.rdlen[2] == 65536);   /* a planned data read */
        if (!bot_reset) {
            sx.ep_busy[2] = false;
            yusb_proxy_ep_done(p, 2, 65536, now);
            collect(p);
            cmd_t d1 = take_cmd();
            reply(p, d1.seq, -YUSB_EPIPE, NULL, 0, now);   /* device stalls bulk-out */
            collect(p);
            CHECK(sx.halts == 1);
            cmd_t clear = take_cmd();
            reply(p, clear.seq, 0, NULL, 0, now);
            collect(p);
        } else {
            uint8_t reset[8] = {0x21, 0xff, 0, 0, 0, 0, 0, 0};
            yusb_proxy_event(p, YUSB_EV_CONTROL, reset, now);
            collect(p);
            reply(p, take_cmd().seq, 0, NULL, 0, now);
            collect(p);
            ep0_finish(p, 0, now);
        }
        uint8_t csw[13] = {'U', 'S', 'B', 'S', 0x11, 0, 0, 0, 0, 0, 1, 0, 1};
        reply(p, csw_urb.seq, 0, csw, 13, now);
        collect(p);
        if (sx.ep_busy[17]) {
            sx.ep_busy[17] = false;
            yusb_proxy_ep_done(p, 17, 13, now);
            collect(p);
        }
        sx.n = 0;
        CHECK(sx.ep_busy[2]);
        cbw(sx.rd[2], 4096, true);                  /* READ, 4 KiB */
        sx.ep_busy[2] = false;
        yusb_proxy_ep_done(p, 2, 31, now);
        collect(p);
        /* Without a host clear-halt seen by the UDC, the core re-clears the
         * remote halt before submitting the CBW (W_PRECLEAR). */
        while (count_ep0()) {
            reply(p, take_ep0().seq, 0, NULL, 0, now);
            collect(p);
        }
        CHECK(count_in() == 2);                     /* data-in and CSW */
        yusb_proxy_stop(p, now);
        yusb_proxy_destroy(p);
    }
}

/* --- Raw Gadget ABI ------------------------------------------------------ */

static void test_raw_gadget_abi(void)
{
#ifdef HAVE_RAW_GADGET_H
    CHECK(RG_IOCTL_INIT == USB_RAW_IOCTL_INIT);
    CHECK(RG_IOCTL_RUN == USB_RAW_IOCTL_RUN);
    CHECK(RG_IOCTL_EVENT_FETCH == USB_RAW_IOCTL_EVENT_FETCH);
    CHECK(RG_IOCTL_EP0_WRITE == USB_RAW_IOCTL_EP0_WRITE);
    CHECK(RG_IOCTL_EP0_READ == USB_RAW_IOCTL_EP0_READ);
    CHECK(RG_IOCTL_EP_ENABLE == USB_RAW_IOCTL_EP_ENABLE);
    CHECK(RG_IOCTL_EP_DISABLE == USB_RAW_IOCTL_EP_DISABLE);
    CHECK(RG_IOCTL_EP_WRITE == USB_RAW_IOCTL_EP_WRITE);
    CHECK(RG_IOCTL_EP_READ == USB_RAW_IOCTL_EP_READ);
    CHECK(RG_IOCTL_CONFIGURE == USB_RAW_IOCTL_CONFIGURE);
    CHECK(RG_IOCTL_VBUS_DRAW == USB_RAW_IOCTL_VBUS_DRAW);
    CHECK(RG_IOCTL_EPS_INFO == USB_RAW_IOCTL_EPS_INFO);
    CHECK(RG_IOCTL_EP0_STALL == USB_RAW_IOCTL_EP0_STALL);
    CHECK(RG_IOCTL_EP_SET_HALT == USB_RAW_IOCTL_EP_SET_HALT);
    CHECK(sizeof(struct usb_raw_init) == RG_INIT_SIZE);
    CHECK(sizeof(struct usb_raw_ep_io) == YUSB_HEADROOM);
    CHECK(sizeof(struct usb_raw_ep_info) == RG_EP_INFO_SIZE);
    CHECK((int)YUSB_EV_CONTROL == (int)USB_RAW_EVENT_CONTROL && (int)YUSB_EV_DISCONNECT == (int)USB_RAW_EVENT_DISCONNECT);
    struct usb_raw_ep_info info;
    memset(&info, 0, sizeof(info));
    info.addr = 3;
    info.caps.type_bulk = 1;
    info.caps.dir_in = 1;
    info.limits.maxpacket_limit = 1024;
    yusb_udc_ep_t ep;
    CHECK(rg_unpack_eps((const uint8_t *)&info, 1, &ep, 1) == 1);
    CHECK(ep.number == 3 && ep.bulk && ep.dir_in && !ep.dir_out && ep.maxpacket_limit == 1024);
    puts("raw_gadget.h checked");
#endif
    uint8_t init[RG_INIT_SIZE];
    CHECK(rg_pack_init(init, "dwc3-gadget", "fcc00000.usb", 3) == 0 && init[256] == 3);
    CHECK(rg_pack_init(init, "", "x", 3) < 0);
    CHECK(rg_result(-4) == YUSB_GADGET_INTERRUPTED && rg_result(-32) == YUSB_GADGET_HALT_CLEARED);
    CHECK(rg_result(-108) == YUSB_GADGET_SHUTDOWN && rg_result(-19) == YUSB_GADGET_SHUTDOWN);
    CHECK(rg_result(-5) == YUSB_GADGET_ERROR && rg_result(512) == 512);
    /* Gadget name inference from the UDC's platform driver (sysfs symlink). */
    char dir[] = "/tmp/yusb-sysfs-XXXXXX";
    CHECK(mkdtemp(dir) != NULL);
    char path[256], target[256], name[64];
    snprintf(path, sizeof(path), "%s/fcc00000.usb", dir);
    mkdir(path, 0700);
    snprintf(path, sizeof(path), "%s/drivers/dwc3", dir);
    snprintf(target, sizeof(target), "%s/drivers", dir);
    mkdir(target, 0700);
    mkdir(path, 0700);
    snprintf(target, sizeof(target), "%s/fcc00000.usb/device", dir);
    mkdir(target, 0700);
    snprintf(target, sizeof(target), "%s/fcc00000.usb/device/driver", dir);
    CHECK(symlink(path, target) == 0);
    rg_gadget_name(dir, "fcc00000.usb", name, sizeof(name));
    CHECK(!strcmp(name, "dwc3-gadget"));
    rg_gadget_name(dir, "missing.usb", name, sizeof(name));
    CHECK(!strcmp(name, "missing.usb"));
    CHECK(!strcmp(rg_gadget_name_for("dummy_udc", "dummy_udc.0"), "dummy_udc"));
    unlink(target);
    char cmd[300];
    snprintf(cmd, sizeof(cmd), "rm -rf %s", dir);
    CHECK(system(cmd) == 0);
}

/* --- footprints and MCU-sized buffers ----------------------------------- */

static void test_footprints(void)
{
    yusb_policy_t policy;
    yusb_policy_defaults(&policy);
    yusb_footprint_t f = yusb_proxy_footprint(&policy, NULL);
#ifdef MCU_SIZED
    /* The ESP32-S3 computer-side box (lane C): firmware bounds above. */
    policy.ep0_max = 4096;
    policy.max_transfer = 4096;
    policy.in_depth_bulk = policy.in_depth_interrupt = 2;
    policy.bot_in_slots = 2;
    policy.budget = (yusb_budget_t){4, 4, 6};
    f = yusb_proxy_footprint(&policy, NULL);
    CHECK(f.create < 60 * 1024);
    /* A forwarded request longer than ep0_max is STALLed locally. */
    yusb_gadget_ops_t ops = scripted_ops(NULL);
    yusb_proxy_t *p = presenting(&policy, &ops, 1000);
    unsigned before = sx.n;
    uint8_t vendor[8] = {0xc0, 0x01, 0, 0, 0, 0, 0x00, 0x20};   /* wLength 8192 */
    yusb_proxy_event(p, YUSB_EV_CONTROL, vendor, 1000);
    collect(p);
    CHECK(sx.ep0_busy && sx.ep0_last == 3 && sx.n == before);
    sx.ep0_busy = false;
    yusb_proxy_ep0_done(p, 0, 1000);
    uint8_t small[8] = {0xc0, 0x01, 0, 0, 0, 0, 0x00, 0x10};    /* 4096: forwarded */
    yusb_proxy_event(p, YUSB_EV_CONTROL, small, 1000);
    collect(p);
    CHECK(sx.n == before + 1);
    yusb_proxy_stop(p, 1000);
    yusb_proxy_destroy(p);
#endif
    printf("footprint proxy create=%zu pumps_max=%zu\n", f.create, f.pumps_max);
    CHECK(f.create > 0 && f.pumps_max > 0);
}

int main(void)
{
    test_fence();
    test_unlink_grace_and_tombstones();
    test_in_stall_parks_with_backoff();
    test_dma_layout_aligns_every_backend_buffer();
    test_set_configuration_survives_the_next_setup();
    test_acked_set_configuration_gets_no_ep0_answer();
    test_abandoned_request_is_unlinked();
    test_out_data_survives_an_early_setup();
    test_out_data_survives_a_setup_during_a_remote_reset();
    test_bus_reset_releases_an_owed_setup();
    test_late_control_reply_does_not_overwrite_ep0();
    test_ep0_buffer_waits_for_an_unsent_out_payload();
    test_bot_plan_ends_with_the_data_out_phase();
    test_get_status_reports_a_local_halt();
    test_raw_gadget_abi();
    test_footprints();
    if (failures) {
        fprintf(stderr, "%d failures\n", failures);
        return 1;
    }
    puts("all core tests passed");
    return 0;
}
