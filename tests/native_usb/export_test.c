/* Deterministic unit tests of the export core (run by
 * test_native_export_core.py under ASan/UBSan at Linux and MCU sizing).
 * Scripted host controller and client.
 *
 * Built a second time with -DCOUNT_ALLOCS, no sanitizer and
 * -Wl,--wrap=malloc,--wrap=calloc,--wrap=free,--wrap=aligned_alloc,--wrap=realloc:
 * live heap bytes are then counted, checked against the footprint
 * functions, and allocation failures are injected one by one. */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "yusb_export.h"

static int failures;
#define CHECK(cond)                                                              \
    do {                                                                         \
        if (!(cond)) {                                                           \
            fprintf(stderr, "%s:%d: CHECK(%s) failed\n", __FILE__, __LINE__, #cond); \
            failures++;                                                          \
        }                                                                        \
    } while (0)

/* --- counted allocations (COUNT_ALLOCS builds) ---------------------------- */

#ifdef COUNT_ALLOCS
void *__real_malloc(size_t n);
void *__real_calloc(size_t a, size_t b);
void *__real_aligned_alloc(size_t a, size_t n);
void *__real_realloc(void *p, size_t n);
void __real_free(void *p);
void *__wrap_malloc(size_t n);
void *__wrap_calloc(size_t a, size_t b);
void *__wrap_aligned_alloc(size_t a, size_t n);
void *__wrap_realloc(void *p, size_t n);
void __wrap_free(void *p);

static struct {
    void *p;
    size_t n;
} table[4096];
static size_t live, peak;
static bool tracking;
static unsigned allocs, fail_at;   /* fail_at: 1-based allocation to refuse */

static bool refuse(void) { return tracking && ++allocs == fail_at; }

static void track(void *p, size_t n)
{
    if (!p || !tracking) return;
    for (size_t i = 0; i < sizeof(table) / sizeof(table[0]); ++i) {
        if (table[i].p) continue;
        table[i].p = p;
        table[i].n = n;
        live += n;
        if (live > peak) peak = live;
        return;
    }
    abort();
}

static void untrack(void *p)
{
    for (size_t i = 0; p && i < sizeof(table) / sizeof(table[0]); ++i) {
        if (table[i].p != p) continue;
        live -= table[i].n;
        table[i].p = NULL;
        return;
    }
}

void *__wrap_malloc(size_t n)
{
    if (refuse()) return NULL;
    void *p = __real_malloc(n);
    track(p, n);
    return p;
}

void *__wrap_calloc(size_t a, size_t b)
{
    if (refuse()) return NULL;
    void *p = __real_calloc(a, b);
    track(p, a * b);
    return p;
}

void *__wrap_aligned_alloc(size_t a, size_t n)
{
    if (refuse()) return NULL;
    void *p = __real_aligned_alloc(a, n);
    track(p, n);
    return p;
}

void *__wrap_realloc(void *q, size_t n)
{
    untrack(q);
    void *p = __real_realloc(q, n);
    track(p, n);
    return p;
}

void __wrap_free(void *p)
{
    untrack(p);
    __real_free(p);
}
#endif

/* --- scripted host controller ------------------------------------------- */

#define DEVID (1u << 16 | 2u)

static struct {
    yusb_xurb_t *urbs[256];   /* submitted, in order */
    unsigned n;
    yusb_xurb_t *cancelled[256];
    unsigned ncancel;
    size_t align;             /* check URB data alignment when > 1 */
    bool control_stalls;      /* control completes synchronously with -EPIPE */
} hc;

static int h_claim(void *c, unsigned d) { (void)c; (void)d; return 0; }
static void h_release(void *c, unsigned d) { (void)c; (void)d; }

static int h_type(void *c, unsigned d, unsigned a)
{
    (void)c; (void)d;
    if (a == 0x81 || a == 0x02) return (int)YUSB_XFER_BULK;
    if (a == 0x83) return (int)YUSB_XFER_INT;
    return -1;
}

static int h_submit(void *c, unsigned d, yusb_xurb_t *u)
{
    (void)c; (void)d;
    if (hc.control_stalls && u->type == YUSB_XFER_CONTROL) return -YUSB_EPIPE;
    if (hc.n < 256) hc.urbs[hc.n++] = u;
    if (hc.align > 1 && (uintptr_t)u->data % hc.align) failures++;
    return 0;
}

static void h_cancel(void *c, unsigned d, yusb_xurb_t *u)
{
    (void)c; (void)d;
    if (hc.ncancel < 256) hc.cancelled[hc.ncancel++] = u;
}

static void *aligned_hook(void *ctx, size_t size, size_t align)
{
    (void)ctx;
    void *p = aligned_alloc(align, size);
    if (p) memset(p, 0, size);
    return p;
}

static void free_hook(void *ctx, void *p) { (void)ctx; free(p); }

static yusb_hc_ops_t ops_(void)
{
    yusb_hc_ops_t ops = {.claim = h_claim, .release = h_release, .endpoint_type = h_type,
                         .submit = h_submit, .cancel = h_cancel};
    return ops;
}

static yusb_export_dev_t dev_(void)
{
    yusb_export_dev_t dev;
    memset(&dev, 0, sizeof(dev));
    strcpy(dev.info.busid, "1-1");
    strcpy(dev.info.path, "/sys/devices/x/1-1");
    dev.info.busnum = 1;
    dev.info.devnum = 2;
    dev.num_ifaces = 1;
    dev.ifaces[0] = (yusb_iface_info_t){8, 6, 0x50};
    return dev;
}

/* The deployment sizing under test: Linux defaults, or the ESP32-S3's. */
static void cfg_(yusb_export_cfg_t *cfg)
{
    yusb_export_defaults(cfg);
#ifdef MCU_SIZED
    cfg->max_urbs = 16;
    cfg->max_urb_bytes = 4096;
#endif
}

/* --- scripted client ---------------------------------------------------- */

static uint8_t out[1u << 21];   /* bytes the core sent */
static size_t have;

/* Feed bytes until the core holds the stream; returns bytes taken. */
static size_t feed(yusb_export_t *x, const uint8_t *data, size_t len)
{
    size_t off = 0;
    while (off < len) {
        uint8_t *buf;
        size_t space = yusb_export_rx_space(x, &buf);
        if (!space) break;
        size_t n = len - off < space ? len - off : space;
        memcpy(buf, data + off, n);
        CHECK(yusb_export_rx_done(x, n, 0) == YUSB_OK);
        off += n;
    }
    return off;
}

/* Send everything queued (or at most `limit` bytes). */
static void drain_some(yusb_export_t *x, size_t limit)
{
    yusb_iov_t iov[64];
    size_t n;
    while (limit && (n = yusb_export_tx_iov(x, iov, 64))) {
        size_t total = 0;
        for (size_t i = 0; i < n && total < limit; ++i) {
            size_t take = iov[i].len < limit - total ? iov[i].len : limit - total;
            CHECK(have + take <= sizeof(out));
            if (have + take > sizeof(out)) return;
            memcpy(out + have, iov[i].base, take);
            have += take;
            total += take;
        }
        yusb_export_tx_done(x, total);
        limit -= total;
    }
}

static void drain(yusb_export_t *x) { drain_some(x, SIZE_MAX); }

static size_t put_submit(uint8_t *p, uint32_t seq, unsigned dir, unsigned ep, uint32_t len,
                         const uint8_t setup[8])
{
    yusb_usbip_hdr_t h = yusb_cmd_submit(seq, DEVID, dir, ep, dir ? YUSB_URB_DIR_IN : 0, len,
                                         ep == 3 ? 8 : 0, setup);
    yusb_usbip_encode(&h, p);
    return YUSB_USBIP_HEADER_SIZE;
}

static size_t put_unlink(uint8_t *p, uint32_t seq, uint32_t victim)
{
    yusb_usbip_hdr_t h = yusb_cmd_unlink(seq, DEVID, victim);
    yusb_usbip_encode(&h, p);
    return YUSB_USBIP_HEADER_SIZE;
}

static void submit_in(yusb_export_t *x, uint32_t seq, unsigned ep, uint32_t len)
{
    uint8_t m[48];
    put_submit(m, seq, YUSB_USBIP_DIR_IN, ep, len, NULL);
    CHECK(feed(x, m, sizeof(m)) == sizeof(m));
}

static const uint8_t get_device[8] = {0x80, 6, 0, 1, 0, 0, 18, 0};

/* OP_REQ_IMPORT (with a YX offer when given); the reply is consumed. */
static void import(yusb_export_t *x, const yusb_yx_params_t *offer)
{
    uint8_t req[YUSB_REQ_IMPORT_SIZE];
    yusb_req_import_encode("1-1", req);
    if (offer) CHECK(yusb_yx_offer_put(req + YUSB_OP_HEADER_SIZE, offer) == YUSB_OK);
    have = 0;
    CHECK(feed(x, req, sizeof(req)) == sizeof(req));
    drain(x);
    CHECK(have == YUSB_REP_IMPORT_SIZE);
    uint16_t code;
    uint32_t status;
    CHECK(yusb_op_decode(out, &code, &status) == YUSB_OK && code == YUSB_OP_REP_IMPORT && status == 0);
    have = 0;
}

/* One reply message from out[*pos]: header (YX code folded), and for
 * RET_SUBMIT of an IN URB its payload, decoded through dec when YX. */
typedef struct {
    yusb_usbip_hdr_t h;
    bool yx;
    const uint8_t *data;      /* plain payload */
    size_t wire;              /* payload bytes on the wire */
} msg_t;

static bool next_msg(size_t *pos, msg_t *m, bool (*is_in)(uint32_t seq), yusb_yx_dec_t *dec,
                     uint8_t *decoded)
{
    if (have - *pos < YUSB_USBIP_HEADER_SIZE) return false;
    uint8_t raw[YUSB_USBIP_HEADER_SIZE];
    memcpy(raw, out + *pos, sizeof(raw));
    memset(m, 0, sizeof(*m));
    m->yx = yusb_yx_unframe(raw);
    CHECK(yusb_usbip_decode(raw, &m->h) == YUSB_OK);
    *pos += YUSB_USBIP_HEADER_SIZE;
    if (m->h.command == YUSB_RET_SUBMIT && is_in && is_in(m->h.seqnum) && m->h.actual_length) {
        m->data = out + *pos;
        if (m->yx) {
            CHECK(dec != NULL);
            if (!dec) return false;
            CHECK(yusb_yx_decode(dec, out + *pos, have - *pos, decoded, m->h.actual_length, &m->wire) ==
                  YUSB_OK);
            m->data = decoded;
        } else {
            m->wire = m->h.actual_length;
        }
        *pos += m->wire;
    }
    return true;
}

static bool all_in(uint32_t seq) { (void)seq; return true; }
static bool none_in(uint32_t seq) { (void)seq; return false; }

static yusb_export_t *create_with(yusb_export_cfg_t *cfg, const yusb_hc_ops_t *ops)
{
    yusb_export_dev_t dev = dev_();
    memset(&hc, 0, sizeof(hc));
    yusb_export_t *x = yusb_export_create(cfg, ops, &dev, 1);
    CHECK(x != NULL);
    return x;
}

static void finish(yusb_export_t *x)
{
    yusb_export_close(x);
    for (unsigned i = 0; i < hc.n; ++i) yusb_export_complete(x, hc.urbs[i], -YUSB_ECONNRESET, 0, 0);
    CHECK(yusb_export_idle(x));
    yusb_export_destroy(x);
}

/* --- flush collateral --------------------------------------------------- */

static void test_export_flush_resubmits_urbs_behind_the_unlinked_one(void)
{
    yusb_buffers_t hook = {64, aligned_hook, free_hook, NULL};
    yusb_hc_ops_t ops = ops_();
    ops.buffers = &hook;
    ops.cancel_flushes_endpoint = true;
    yusb_export_cfg_t cfg;
    cfg_(&cfg);
    yusb_export_t *x = create_with(&cfg, &ops);
    hc.align = 64;
    import(x, NULL);
    uint8_t in[6 * 48];
    size_t len = 0;
    for (uint32_t seq = 1; seq <= 3; ++seq) len += put_submit(in + len, seq, 1, 1, 512, NULL);
    len += put_unlink(in + len, 4, 1);   /* unlink the oldest */
    /* A URB for the same endpoint that arrives before the flush completes
     * must queue behind the flushed ones. */
    len += put_submit(in + len, 5, 1, 1, 512, NULL);
    CHECK(feed(x, in, len) == len);
    CHECK(hc.n == 3 && hc.ncancel == 1);
    /* The backend flushes the whole endpoint. */
    yusb_xurb_t *flushed[3] = {hc.urbs[0], hc.urbs[1], hc.urbs[2]};
    for (int i = 0; i < 3; ++i) yusb_export_complete(x, flushed[i], -YUSB_ECONNRESET, 0, 0);
    CHECK(hc.n == 6);                    /* 2 and 3 went again, unchanged, then 5 */
    CHECK(hc.urbs[3]->seqnum == 2 && hc.urbs[4]->seqnum == 3 && hc.urbs[5]->seqnum == 5);
    CHECK(yusb_export_counters(x)->unlinked == 1);
    yusb_export_close(x);
    for (unsigned i = 3; i < hc.n; ++i) yusb_export_complete(x, hc.urbs[i], -YUSB_ECONNRESET, 0, 0);
    CHECK(yusb_export_idle(x));
    yusb_export_destroy(x);
}

/* --- finding 1: devlist bounds ------------------------------------------ */

static void test_export_devlist_rejects_bad_records_and_sizes_the_reply(void)
{
    yusb_hc_ops_t ops = ops_();
    yusb_export_cfg_t cfg;
    cfg_(&cfg);
    yusb_export_dev_t devs[YUSB_EXPORT_MAX_DEVICES];
    memset(devs, 0, sizeof(devs));
    for (int i = 0; i < YUSB_EXPORT_MAX_DEVICES; ++i) {
        snprintf(devs[i].info.busid, sizeof(devs[i].info.busid), "1-%d", i + 1);
        devs[i].num_ifaces = 255;   /* bNumInterfaces as a device reported it */
    }
    CHECK(yusb_export_create(&cfg, &ops, devs, YUSB_EXPORT_MAX_DEVICES) == NULL);
    for (int i = 0; i < YUSB_EXPORT_MAX_DEVICES; ++i) devs[i].num_ifaces = YUSB_EXPORT_MAX_IFACES + 1;
    CHECK(yusb_export_create(&cfg, &ops, devs, YUSB_EXPORT_MAX_DEVICES) == NULL);
    for (int i = 0; i < YUSB_EXPORT_MAX_DEVICES; ++i) {
        devs[i].num_ifaces = YUSB_EXPORT_MAX_IFACES;
        for (int k = 0; k < YUSB_EXPORT_MAX_IFACES; ++k)
            devs[i].ifaces[k] = (yusb_iface_info_t){(uint8_t)i, (uint8_t)k, 7};
    }
    /* Unterminated or non-ASCII strings are refused too. */
    yusb_export_dev_t bad = devs[0];
    memset(bad.info.busid, 'b', sizeof(bad.info.busid));
    CHECK(yusb_export_create(&cfg, &ops, &bad, 1) == NULL);
    bad = devs[0];
    memset(bad.info.path, 'p', sizeof(bad.info.path));
    CHECK(yusb_export_create(&cfg, &ops, &bad, 1) == NULL);
    bad = devs[0];
    bad.info.path[0] = (char)0xc3;
    CHECK(yusb_export_create(&cfg, &ops, &bad, 1) == NULL);
    CHECK(yusb_export_create(&cfg, NULL, devs, 1) == NULL);
    /* The largest valid list: every device with every interface. */
    yusb_export_t *x = yusb_export_create(&cfg, &ops, devs, YUSB_EXPORT_MAX_DEVICES);
    CHECK(x != NULL);
    if (!x) return;
    uint8_t req[8];
    yusb_op_encode(YUSB_OP_REQ_DEVLIST, 0, req);
    have = 0;
    CHECK(feed(x, req, sizeof(req)) == sizeof(req));
    drain(x);
    size_t expect = YUSB_OP_HEADER_SIZE + 4 +
                    YUSB_EXPORT_MAX_DEVICES * (YUSB_DEVICE_INFO_SIZE + 4 * YUSB_EXPORT_MAX_IFACES);
    CHECK(have == expect && yusb_export_finished(x));
    size_t at = YUSB_OP_HEADER_SIZE + 4;
    for (int i = 0; i < YUSB_EXPORT_MAX_DEVICES && have == expect; ++i) {
        yusb_device_info_t info;
        CHECK(yusb_device_info_decode(out + at, &info) == YUSB_OK);
        CHECK(info.num_interfaces == YUSB_EXPORT_MAX_IFACES && !strcmp(info.busid, devs[i].info.busid));
        at += YUSB_DEVICE_INFO_SIZE;
        for (int k = 0; k < YUSB_EXPORT_MAX_IFACES; ++k, at += 4)
            CHECK(out[at] == i && out[at + 1] == k && out[at + 2] == 7 && out[at + 3] == 0);
    }
    yusb_export_destroy(x);
}

/* --- finding 2: unknown unlinks ----------------------------------------- */

static void test_export_unknown_unlinks(void)
{
    yusb_hc_ops_t ops = ops_();
    yusb_export_cfg_t cfg;
    cfg_(&cfg);
    yusb_export_t *x = create_with(&cfg, &ops);
    import(x, NULL);
    enum { N = 24 };
    static uint8_t in[N * 48];
    size_t len = 0;
    for (uint32_t k = 0; k < N; ++k) len += put_unlink(in + len, 1000 + k, 5000 + k);
    /* A sender that took segments, released its lock and sends later sees
     * the same bytes: nothing the core queued is rewritten before tx_done. */
    size_t first = feed(x, in, 10 * 48);
    CHECK(first == 10 * 48);
    yusb_iov_t early[64];
    size_t nearly = yusb_export_tx_iov(x, early, 64);
    static uint8_t snapshot[N * 48];
    size_t snap = 0;
    for (size_t i = 0; i < nearly; ++i) {
        memcpy(snapshot + snap, early[i].base, early[i].len);
        snap += early[i].len;
    }
    size_t second = feed(x, in + first, len - first);   /* MCU: notes run out, the stream holds */
    CHECK(first + second == len);
    size_t check = 0;
    for (size_t i = 0; i < nearly; ++i) {
        CHECK(!memcmp(snapshot + check, early[i].base, early[i].len));
        check += early[i].len;
    }
    /* Now the transport drains; held bytes follow. */
    drain(x);
    size_t pos = 0;
    unsigned replies = 0;
    msg_t m;
    while (next_msg(&pos, &m, none_in, NULL, NULL)) {
        CHECK(m.h.command == YUSB_RET_UNLINK && m.h.status == 0);
        CHECK(m.h.seqnum == 1000 + replies);   /* exactly one each, in order */
        replies++;
    }
    CHECK(replies == N && pos == have);
    CHECK(yusb_export_counters(x)->unlinks == N);
    finish(x);
}

/* --- lane C: unlink while every slot is busy; control reserve ------------ */

static bool seq_in(uint32_t seq) { return seq < 100; }

static void test_export_unlink_reaches_urbs_parked_in_full_slots(void)
{
    yusb_hc_ops_t ops = ops_();
    yusb_export_cfg_t cfg;
    cfg_(&cfg);
    cfg.max_urbs = 4;
    yusb_export_t *x = create_with(&cfg, &ops);
    import(x, NULL);
    /* Interrupt reads that never complete take every non-control slot; two
     * more are held. The host then gives up on a parked one and on a held
     * one: both CMD_UNLINKs must be read. */
    uint8_t in[16 * 48];
    size_t len = 0;
    for (uint32_t seq = 1; seq <= 5; ++seq) len += put_submit(in + len, seq, 1, 3, 64, NULL);
    len += put_unlink(in + len, 101, 5);   /* held */
    len += put_unlink(in + len, 102, 1);   /* parked at the backend */
    CHECK(feed(x, in, len) == len);
    CHECK(hc.n == 3 && hc.ncancel == 1 && hc.cancelled[0] == hc.urbs[0]);
    /* Control is not starved by the parked reads: the reserved slot. */
    len = put_submit(in, 6, 1, 0, 18, get_device);
    CHECK(feed(x, in, len) == len);
    CHECK(hc.n == 4 && hc.urbs[3]->type == YUSB_XFER_CONTROL && hc.urbs[3]->data != NULL);
    memcpy(hc.urbs[3]->data, "\x12\x01", 2);
    yusb_export_complete(x, hc.urbs[3], 0, 18, 0);
    yusb_export_complete(x, hc.urbs[0], -YUSB_ECONNRESET, 0, 0);   /* the cancel */
    drain(x);
    /* The freed slot goes to the held read 4 (5 was unlinked). */
    CHECK(hc.n == 5 && hc.urbs[4]->seqnum == 4);
    size_t pos = 0;
    msg_t m;
    unsigned seen = 0;
    while (next_msg(&pos, &m, seq_in, NULL, NULL)) {
        if (seen == 0) CHECK(m.h.command == YUSB_RET_UNLINK && m.h.seqnum == 101 &&
                             m.h.status == -YUSB_ECONNRESET);
        if (seen == 1) CHECK(m.h.command == YUSB_RET_SUBMIT && m.h.seqnum == 6 && m.h.actual_length == 18);
        if (seen == 2) CHECK(m.h.command == YUSB_RET_UNLINK && m.h.seqnum == 102 &&
                             m.h.status == -YUSB_ECONNRESET);
        CHECK(!(m.h.command == YUSB_RET_SUBMIT && (m.h.seqnum == 5 || m.h.seqnum == 1)));
        seen++;
    }
    CHECK(seen == 3);
    CHECK(yusb_export_counters(x)->unlinked == 2);
    /* Held set full: the stream stops (backpressure), then resumes. */
    len = 0;
    for (uint32_t seq = 10; seq < 10 + 4 + 1; ++seq) len += put_submit(in + len, seq, 1, 3, 64, NULL);
    uint8_t *buf;
    CHECK(feed(x, in, len) == len);   /* staged; 4 held, the fifth waits */
    CHECK(yusb_export_rx_space(x, &buf) == 0 && hc.n == 5);
    yusb_export_complete(x, hc.urbs[1], 0, 8, 0);
    drain(x);
    CHECK(hc.n == 6 && hc.urbs[5]->seqnum == 10);
    CHECK(yusb_export_rx_space(x, &buf) > 0);
    finish(x);
}

/* --- lane C: buffers sized per URB, pool exhaustion ---------------------- */

static bool overlap(const yusb_xurb_t *a, const yusb_xurb_t *b)
{
    return a->data < b->data + b->length && b->data < a->data + a->length;
}

static void test_export_pool_sizes_buffers_per_urb(void)
{
    yusb_buffers_t hook = {64, aligned_hook, free_hook, NULL};
    yusb_hc_ops_t ops = ops_();
    ops.buffers = &hook;
    yusb_export_cfg_t cfg;
    yusb_export_defaults(&cfg);
    cfg.max_urbs = 16;
    cfg.max_urb_bytes = 4096;
    yusb_export_t *x = create_with(&cfg, &ops);
    hc.align = 64;
    import(x, NULL);
    /* 15 small parked reads fit the automatic pool (8 x 4 KiB). */
    for (uint32_t seq = 1; seq <= 15; ++seq) submit_in(x, seq, 3, 64);
    CHECK(hc.n == 15);
    for (unsigned i = 0; i < hc.n; ++i)
        for (unsigned k = 0; k < i; ++k) CHECK(!overlap(hc.urbs[i], hc.urbs[k]));
    finish(x);

    /* A small pool: room for three 4 KiB URBs. Exhaustion holds URBs. */
    cfg.pool_bytes = 3 * (64 + 4096) + (64 + 1024);
    x = create_with(&cfg, &ops);
    hc.align = 64;
    import(x, NULL);
    for (uint32_t seq = 1; seq <= 5; ++seq) submit_in(x, seq, 1, 4096);
    CHECK(hc.n == 3);
    memset(hc.urbs[0]->data, 0xa5, 4096);
    yusb_export_complete(x, hc.urbs[0], 0, 4096, 0);
    /* Its reply is unsent: the buffer stays its own. */
    CHECK(hc.n == 3);
    yusb_export_complete(x, hc.urbs[1], 0, 0, 0);   /* nothing to send from it */
    CHECK(hc.n == 4 && hc.urbs[3]->seqnum == 4);
    CHECK(!overlap(hc.urbs[3], hc.urbs[0]));
    drain(x);
    CHECK(hc.n == 5 && hc.urbs[4]->seqnum == 5);
    size_t pos = 0;
    msg_t m;
    CHECK(next_msg(&pos, &m, all_in, NULL, NULL) && m.h.seqnum == 1 && m.h.actual_length == 4096);
    CHECK(m.data && m.data[0] == 0xa5 && m.data[4095] == 0xa5);
    CHECK(next_msg(&pos, &m, all_in, NULL, NULL) && m.h.seqnum == 2 && m.h.actual_length == 0);
    /* OUT data waits for a buffer: the stream stops before its payload. */
    static uint8_t in[48 + 4096 + 48];
    size_t len = put_submit(in, 20, 0, 2, 4096, NULL);
    memset(in + len, 0x3c, 4096);
    len += 4096;
    len += put_submit(in + len, 21, 1, 0, 18, get_device);
    uint8_t *buf;
    size_t took = feed(x, in, len);
    CHECK(yusb_export_rx_space(x, &buf) == 0 && hc.n == 5);
    yusb_export_complete(x, hc.urbs[2], 0, 0, 0);
    drain(x);
    CHECK(feed(x, in + took, len - took) == len - took);
    CHECK(hc.n == 7 && hc.urbs[5]->seqnum == 20 && hc.urbs[5]->data[4095] == 0x3c);
    CHECK(hc.urbs[6]->seqnum == 21);
    finish(x);

    /* A pool that cannot hold a URB fails it with -ENOMEM; zero-length
     * control still works from the reserve. */
    cfg.pool_bytes = 200;
    x = create_with(&cfg, &ops);
    import(x, NULL);
    uint8_t setconf[8] = {0, 9, 1, 0, 0, 0, 0, 0};
    len = put_submit(in, 1, 1, 1, 512, NULL);
    len += put_submit(in + len, 2, 0, 0, 0, setconf);
    CHECK(feed(x, in, len) == len);
    CHECK(hc.n == 1 && hc.urbs[0]->tweak == YUSB_TWEAK_SET_CONFIGURATION);
    yusb_export_complete(x, hc.urbs[0], 0, 0, 0);
    drain(x);
    pos = 0;
    CHECK(next_msg(&pos, &m, none_in, NULL, NULL) && m.h.seqnum == 1 && m.h.status == -YUSB_ENOMEM);
    CHECK(next_msg(&pos, &m, none_in, NULL, NULL) && m.h.seqnum == 2 && m.h.status == 0);
    finish(x);
}

/* --- item 6: YX IN replies without copying RAW bodies --------------------- */

static void fill_mixed(uint8_t *p, size_t n, unsigned salt)
{
    uint32_t r = 0x9e3779b9u ^ salt;
    for (size_t at = 0; at < n; at += 4096) {
        size_t len = n - at < 4096 ? n - at : 4096;
        unsigned kind = (unsigned)((at / 4096 + salt) % 4);
        for (size_t i = 0; i < len; ++i) {
            r = r * 1664525u + 1013904223u;
            p[at + i] = kind == 0 ? 0 : kind == 1 ? (uint8_t)"yoke text "[i % 10] : (uint8_t)(r >> 24);
        }
    }
}

static void test_export_yx_replies_match_the_reference_encoder(void)
{
    yusb_hc_ops_t ops = ops_();
    yusb_export_cfg_t cfg;
    cfg_(&cfg);
    cfg.yx = (yusb_yx_params_t){YUSB_YX_VERSION, YUSB_YX_FILL | YUSB_YX_LZ4 | YUSB_YX_CACHE, 16};
    yusb_export_t *x = create_with(&cfg, &ops);
    yusb_yx_params_t offer = cfg.yx, agreed;
    import(x, &offer);
    CHECK(yusb_export_yx(x, &agreed) && agreed.levers == offer.levers && agreed.cache_blocks == 16);
    yusb_yx_enc_t ref;
    yusb_yx_dec_t *dec = calloc(1, sizeof(*dec));
    CHECK(dec && !yusb_yx_enc_init(&ref, &agreed) && !yusb_yx_dec_init(dec, &agreed));
    uint32_t max = cfg.max_urb_bytes < 65536 ? cfg.max_urb_bytes : 65536;
    static uint8_t src[65536], decoded[65536], wire[65536 + 64];
    for (uint32_t seq = 1; seq <= 12; ++seq) {
        uint32_t len = seq == 1 ? 100 : seq % 3 == 0 ? max : 512 + (seq * 4099u) % (max - 512);
        submit_in(x, seq, 1, max);
        CHECK(hc.n == seq);
        fill_mixed(src, len, seq % 5 == 0 ? 0 : seq);   /* repeats hit the cache */
        memcpy(hc.urbs[seq - 1]->data, src, len);
        have = 0;
        yusb_export_complete(x, hc.urbs[seq - 1], 0, len, 0);
        /* RAW bodies are sent from the URB buffer itself. */
        yusb_iov_t iov[64];
        size_t n = yusb_export_tx_iov(x, iov, 64), raw_segments = 0;
        for (size_t i = 0; i < n; ++i)
            if (iov[i].base >= hc.urbs[seq - 1]->data && iov[i].base < hc.urbs[seq - 1]->data + len)
                raw_segments++;
        CHECK(len < YUSB_YX_MIN_LENGTH || n <= 1 + yusb_yx_segments(len));
        drain(x);
        size_t pos = 0;
        msg_t m;
        CHECK(next_msg(&pos, &m, all_in, dec, decoded) && m.h.seqnum == seq && pos == have);
        CHECK(m.h.actual_length == len && !memcmp(m.data, src, len));
        CHECK(m.yx == (len >= YUSB_YX_MIN_LENGTH));
        if (m.yx) {
            size_t w = yusb_yx_encode(&ref, src, len, wire);
            CHECK(w == m.wire && !memcmp(wire, out + YUSB_USBIP_HEADER_SIZE, w));
            if (seq == 2 || seq == 6) CHECK(raw_segments > 0);   /* random blocks */
        }
    }
    CHECK(yusb_export_counters(x)->yx_wire_out < yusb_export_counters(x)->yx_raw_out);
    yusb_yx_enc_free(&ref);
    yusb_yx_dec_free(dec);
    free(dec);
    have = 0;
    finish(x);
}

/* A YX offer whose agreement has only READAHEAD: plain USB/IP, no accept. */
static void test_export_readahead_alone_is_not_framed(void)
{
    yusb_hc_ops_t ops = ops_();
    yusb_export_cfg_t cfg;
    cfg_(&cfg);
    cfg.yx = (yusb_yx_params_t){YUSB_YX_VERSION, YUSB_YX_READAHEAD | YUSB_YX_FILL, 0};
    yusb_export_t *x = create_with(&cfg, &ops);
    uint8_t req[YUSB_REQ_IMPORT_SIZE];
    yusb_req_import_encode("1-1", req);
    yusb_yx_params_t offer = {YUSB_YX_VERSION, YUSB_YX_READAHEAD, 0};
    yusb_yx_offer_put(req + YUSB_OP_HEADER_SIZE, &offer);
    have = 0;
    CHECK(feed(x, req, sizeof(req)) == sizeof(req));
    drain(x);
    yusb_yx_params_t accept;
    CHECK(have == YUSB_REP_IMPORT_SIZE && !yusb_yx_accept_get(out + YUSB_OP_HEADER_SIZE, &accept));
    CHECK(!yusb_export_yx(x, NULL));
    have = 0;
    finish(x);
}

/* --- footprints ---------------------------------------------------------- */

static void test_footprints(void)
{
    yusb_export_cfg_t cfg;
    yusb_export_defaults(&cfg);
    cfg.max_urbs = 16;
    cfg.max_urb_bytes = 4096;
    size_t mcu = yusb_export_footprint(&cfg, NULL);
    cfg.yx = (yusb_yx_params_t){YUSB_YX_VERSION, YUSB_YX_FILL | YUSB_YX_LZ4 | YUSB_YX_CACHE, 16};
    size_t mcu_yx = yusb_export_footprint(&cfg, NULL);
    CHECK(mcu < 60 * 1024);
    CHECK(mcu_yx > mcu);
    yusb_export_defaults(&cfg);
    size_t linux_plain = yusb_export_footprint(&cfg, NULL);
    cfg.yx = (yusb_yx_params_t){YUSB_YX_VERSION, YUSB_YX_FILL | YUSB_YX_LZ4 | YUSB_YX_CACHE, 64};
    size_t linux_yx = yusb_export_footprint(&cfg, NULL);
    cfg_(&cfg);
    printf("footprints mcu=%zu mcu+yx16=%zu linux=%zu linux+yx64=%zu\n", mcu, mcu_yx, linux_plain, linux_yx);
    printf("footprint export=%zu\n", yusb_export_footprint(&cfg, NULL));
}

#ifdef COUNT_ALLOCS
/* --- items 4 and 5: counted allocations ---------------------------------- */

static void counted_case(const char *name, yusb_export_cfg_t *cfg, const yusb_buffers_t *buffers, bool offer)
{
    yusb_hc_ops_t ops = ops_();
    ops.buffers = buffers;
    size_t fp = yusb_export_footprint(cfg, buffers);
    live = peak = 0;
    allocs = fail_at = 0;
    tracking = true;
    yusb_export_t *x = create_with(cfg, &ops);
    size_t at_create = live;
    yusb_yx_params_t o = {YUSB_YX_VERSION, YUSB_YX_LEVERS, cfg->yx.cache_blocks};
    import(x, offer ? &o : NULL);
    size_t after_import = live;
    /* Traffic allocates nothing: the pool and rings are fixed. */
    for (uint32_t seq = 1; seq <= 8; ++seq) {
        submit_in(x, seq, 1, cfg->max_urb_bytes);
        if (hc.n == seq) {
            memset(hc.urbs[seq - 1]->data, (int)seq, cfg->max_urb_bytes);
            yusb_export_complete(x, hc.urbs[seq - 1], 0, cfg->max_urb_bytes, 0);
        }
        drain(x);
        have = 0;
    }
    CHECK(live == after_import && peak == after_import);
    yusb_yx_params_t limits;
    yusb_yx_agree(&cfg->yx, &cfg->yx, &limits);
    size_t codec = yusb_yx_framed(&limits) ? sizeof(yusb_yx_enc_t) + sizeof(yusb_yx_dec_t) +
                                                 yusb_yx_enc_footprint(&limits) + yusb_yx_dec_footprint(&limits)
                                           : 0;
    printf("counted %-28s footprint=%8zu create=%8zu import=%8zu yx=%d\n", name, fp, at_create, after_import,
           yusb_export_yx(x, NULL));
    CHECK(fp >= after_import);
    /* Exact: the codec is counted only when an offer agreed to it. */
    CHECK(fp - after_import == (yusb_export_yx(x, NULL) ? 0 : codec));
    finish(x);
    CHECK(live == 0);
    tracking = false;
}

static void test_counted_footprints(void)
{
    yusb_buffers_t hook = {64, aligned_hook, free_hook, NULL};
    yusb_buffers_t plain64 = {64, NULL, NULL, NULL};
    yusb_export_cfg_t cfg;
    yusb_export_defaults(&cfg);
    counted_case("linux", &cfg, NULL, false);
    cfg.yx = (yusb_yx_params_t){YUSB_YX_VERSION, YUSB_YX_FILL | YUSB_YX_LZ4 | YUSB_YX_CACHE, 64};
    counted_case("linux+yx64 offered", &cfg, NULL, true);
    counted_case("linux+yx64 not offered", &cfg, NULL, false);
    yusb_export_defaults(&cfg);
    cfg.max_urbs = 16;
    cfg.max_urb_bytes = 4096;
    counted_case("mcu", &cfg, NULL, false);
    counted_case("mcu align64 hook", &cfg, &hook, false);
    counted_case("mcu align64 aligned_alloc", &cfg, &plain64, false);
    cfg.yx = (yusb_yx_params_t){YUSB_YX_VERSION, YUSB_YX_FILL | YUSB_YX_LZ4 | YUSB_YX_CACHE, 16};
    counted_case("mcu+yx16 offered", &cfg, &hook, true);
    cfg.yx = (yusb_yx_params_t){YUSB_YX_VERSION, YUSB_YX_FILL | YUSB_YX_LZ4, 0};
    counted_case("mcu+yx fill,lz4 offered", &cfg, NULL, true);
    cfg.yx = (yusb_yx_params_t){YUSB_YX_VERSION, YUSB_YX_READAHEAD, 0};
    counted_case("mcu readahead only", &cfg, NULL, true);
    cfg.yx.levers = 0;
    cfg.pool_bytes = 20000;
    counted_case("mcu pool 20000", &cfg, &hook, false);
}

/* The codec allocates exactly its footprint; a refused allocation leaves
 * nothing behind, and free is safe after it, on a zeroed struct and twice. */
static void test_counted_codec(void)
{
    const yusb_yx_params_t sets[] = {
        {1, 0, 0}, {1, YUSB_YX_FILL, 0}, {1, YUSB_YX_LZ4, 0}, {1, YUSB_YX_CACHE, 1},
        {1, YUSB_YX_CACHE | YUSB_YX_LZ4, 17}, {1, YUSB_YX_LEVERS, YUSB_YX_MAX_CACHE_BLOCKS},
    };
    for (size_t i = 0; i < sizeof(sets) / sizeof(sets[0]); ++i) {
        yusb_yx_enc_t e;
        yusb_yx_dec_t *d = calloc(1, sizeof(*d));
        memset(&e, 0, sizeof(e));
        yusb_yx_enc_free(&e);
        yusb_yx_dec_free(d);
        live = 0;
        allocs = fail_at = 0;
        tracking = true;
        CHECK(yusb_yx_enc_init(&e, &sets[i]) == YUSB_OK && live == yusb_yx_enc_footprint(&sets[i]));
        yusb_yx_enc_free(&e);
        CHECK(live == 0);
        CHECK(yusb_yx_dec_init(d, &sets[i]) == YUSB_OK && live == yusb_yx_dec_footprint(&sets[i]));
        yusb_yx_dec_free(d);
        yusb_yx_dec_free(d);
        CHECK(live == 0);
        for (unsigned k = 1; k <= 4; ++k) {
            allocs = 0;
            fail_at = k;
            int rc = yusb_yx_enc_init(&e, &sets[i]);
            CHECK(rc == (allocs >= k ? YUSB_ERR_NOMEM : YUSB_OK));
            yusb_yx_enc_free(&e);
            yusb_yx_enc_free(&e);
            allocs = 0;
            rc = yusb_yx_dec_init(d, &sets[i]);
            CHECK(rc == (allocs >= k ? YUSB_ERR_NOMEM : YUSB_OK));
            yusb_yx_dec_free(d);
            CHECK(live == 0);
        }
        tracking = false;
        free(d);
    }
}

/* Item 5: every allocation of the YX setup at import refused in turn. The
 * import is still answered, without an accept, and nothing leaks. */
static void test_counted_yx_setup_failure(void)
{
    yusb_hc_ops_t ops = ops_();
    yusb_export_cfg_t cfg;
    yusb_export_defaults(&cfg);
    cfg.max_urbs = 4;
    cfg.max_urb_bytes = 8192;
    cfg.yx = (yusb_yx_params_t){YUSB_YX_VERSION, YUSB_YX_FILL | YUSB_YX_LZ4 | YUSB_YX_CACHE, 8};
    unsigned k = 1;
    for (; k < 32; ++k) {
        live = 0;
        allocs = fail_at = 0;
        tracking = true;
        yusb_export_t *x = create_with(&cfg, &ops);
        allocs = 0;
        fail_at = k;
        uint8_t req[YUSB_REQ_IMPORT_SIZE];
        yusb_req_import_encode("1-1", req);
        yusb_yx_params_t offer = cfg.yx;
        yusb_yx_offer_put(req + YUSB_OP_HEADER_SIZE, &offer);
        have = 0;
        CHECK(feed(x, req, sizeof(req)) == sizeof(req));
        bool refused = allocs >= k;
        fail_at = 0;
        drain(x);
        uint16_t code;
        uint32_t status;
        yusb_yx_params_t accept;
        CHECK(have == YUSB_REP_IMPORT_SIZE && !yusb_op_decode(out, &code, &status) && status == 0);
        CHECK(yusb_yx_accept_get(out + YUSB_OP_HEADER_SIZE, &accept) == !refused);
        CHECK(yusb_export_yx(x, NULL) == !refused);
        uint8_t zeros[8] = {0};
        if (!refused) {   /* every allocation got through: done */
            finish(x);
            CHECK(live == 0);
            tracking = false;
            break;
        }
        CHECK(!memcmp(out + YUSB_OP_HEADER_SIZE + YUSB_PATH_SIZE - 8, zeros, 8));
        /* The connection works, plain. */
        submit_in(x, 1, 1, 4096);
        CHECK(hc.n == 1);
        yusb_export_complete(x, hc.urbs[0], 0, 4096, 0);
        have = 0;
        drain(x);
        size_t pos = 0;
        msg_t m;
        CHECK(next_msg(&pos, &m, all_in, NULL, NULL) && m.yx == false && m.h.actual_length == 4096);
        have = 0;
        finish(x);
        CHECK(live == 0);
        tracking = false;
    }
    CHECK(k == 8);   /* refused in turn: enc, dec, LZ4 state, cache, hash, index, dec cache */
}
#endif

int main(void)
{
    test_export_flush_resubmits_urbs_behind_the_unlinked_one();
    test_export_devlist_rejects_bad_records_and_sizes_the_reply();
    test_export_unknown_unlinks();
    test_export_unlink_reaches_urbs_parked_in_full_slots();
    test_export_pool_sizes_buffers_per_urb();
    test_export_yx_replies_match_the_reference_encoder();
    test_export_readahead_alone_is_not_framed();
#ifdef COUNT_ALLOCS
    test_counted_codec();
    test_counted_yx_setup_failure();
    test_counted_footprints();
#endif
    test_footprints();
    if (failures) {
        fprintf(stderr, "%d failures\n", failures);
        return 1;
    }
    puts("all export tests passed");
    return 0;
}
