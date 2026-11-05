/* yusb_proxy_footprint against counted allocations (review M6/L7).
 *
 * Built by test_native_footprint.py without sanitizers and with
 * -Wl,--wrap=malloc,--wrap=calloc,--wrap=aligned_alloc,--wrap=realloc,--wrap=free
 * so every heap byte the core holds is counted. For each sizing (Linux and
 * MCU, with and without a YX offer the exporter accepts): live bytes after
 * the import never exceed footprint.create, and SET_CONFIGURATION of a BOT
 * device adds no more than pumps_max. An allocation failure during the YX
 * codec set-up ends the session as YUSB_END_YX_UNAVAILABLE without a leak. */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "yusb_proxy.h"
#include "yusb_yx.h"

void *__real_malloc(size_t);
void *__real_calloc(size_t, size_t);
void *__real_aligned_alloc(size_t, size_t);
void *__real_realloc(void *, size_t);
void __real_free(void *);
void *__wrap_malloc(size_t n);
void *__wrap_calloc(size_t a, size_t b);
void *__wrap_aligned_alloc(size_t a, size_t n);
void *__wrap_realloc(void *q, size_t n);
void __wrap_free(void *p);

static int failures;
#define CHECK(cond)                                                              \
    do {                                                                         \
        if (!(cond)) {                                                           \
            fprintf(stderr, "%s:%d: CHECK(%s) failed\n", __FILE__, __LINE__, #cond); \
            failures++;                                                          \
        }                                                                        \
    } while (0)

#define SLOTS 4096
static struct { void *p; size_t n; } tab[SLOTS];
static size_t live;
static int tracking;
static long fail_at = -1, allocs;   /* fail the fail_at-th tracked allocation */

static void *track(void *p, size_t n)
{
    if (!p || !tracking) return p;
    for (unsigned i = 0; i < SLOTS; ++i)
        if (!tab[i].p) { tab[i].p = p; tab[i].n = n; live += n; return p; }
    abort();
}
static void untrack(void *p)
{
    if (!p) return;
    for (unsigned i = 0; i < SLOTS; ++i)
        if (tab[i].p == p) { live -= tab[i].n; tab[i].p = NULL; return; }
}
static bool inject(void) { return tracking && fail_at >= 0 && allocs++ == fail_at; }
void *__wrap_malloc(size_t n) { return inject() ? NULL : track(__real_malloc(n), n); }
void *__wrap_calloc(size_t a, size_t b) { return inject() ? NULL : track(__real_calloc(a, b), a * b); }
void *__wrap_aligned_alloc(size_t a, size_t n) { return inject() ? NULL : track(__real_aligned_alloc(a, n), n); }
void *__wrap_realloc(void *q, size_t n) { untrack(q); return track(__real_realloc(q, n), n); }
void __wrap_free(void *p) { untrack(p); __real_free(p); }

/* --- minimal scripted peers ------------------------------------------- */

static struct { uint8_t stream[1 << 20]; size_t have; uint32_t seqs[256], lens[256]; unsigned n; } sx;
static void s_ep0_write(void *c, uint8_t *d, size_t l, bool z) { (void)c; (void)d; (void)l; (void)z; }
static void s_ep0_read(void *c, uint8_t *d, size_t l) { (void)c; (void)d; (void)l; }
static void s_ep0_stall(void *c) { (void)c; }
static int s_enable(void *c, const yusb_endpoint_t *e) { (void)c; return yusb_ep_number(e) + (yusb_ep_is_in(e) ? 16 : 0); }
static int s_idle(void *c, int h) { (void)c; (void)h; return 0; }
static int s_none(void *c) { (void)c; return 0; }
static int s_draw(void *c, unsigned m) { (void)c; (void)m; return 0; }
static yusb_speed_t s_speed(void *c) { (void)c; return YUSB_SPEED_HIGH; }
static void s_write(void *c, int h, uint8_t *d, size_t l, bool z) { (void)c; (void)h; (void)d; (void)l; (void)z; }
static void s_read(void *c, int h, uint8_t *d, size_t l) { (void)c; (void)h; (void)d; (void)l; }
static void s_cancel(void *c, int h) { (void)c; (void)h; }

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
    while (sx.have - pos >= 48 && sx.n < 256) {
        uint8_t raw[48];
        memcpy(raw, sx.stream + pos, 48);
        yusb_yx_unframe(raw);
        yusb_usbip_hdr_t h;
        if (yusb_usbip_decode(raw, &h)) abort();
        size_t need = 48 + (h.command == YUSB_CMD_SUBMIT && !h.direction ? h.length : 0);
        if (sx.have - pos < need) break;
        sx.seqs[sx.n] = h.seqnum;
        sx.lens[sx.n++] = h.length;
        pos += need;
    }
    memmove(sx.stream, sx.stream + pos, sx.have - pos);
    sx.have -= pos;
}

static void feed(yusb_proxy_t *p, const uint8_t *d, size_t len)
{
    while (len) {
        uint8_t *b;
        size_t space = yusb_proxy_rx_space(p, &b);
        if (!space) return;
        size_t n = len < space ? len : space;
        memcpy(b, d, n);
        yusb_proxy_rx_done(p, n, 0);
        d += n;
        len -= n;
    }
}

static void reply(yusb_proxy_t *p, uint32_t seq, int32_t st, const uint8_t *d, uint32_t len)
{
    uint8_t raw[48];
    yusb_usbip_hdr_t h = yusb_ret_submit(seq, st, len);
    yusb_usbip_encode(&h, raw);
    feed(p, raw, 48);
    if (d && len) feed(p, d, len);
}

static const uint8_t dev_desc[18] = {18, 1, 0, 2, 0, 0, 0, 64, 0x09, 0x12, 0x99, 0, 0, 1, 0, 0, 0, 1};
static const uint8_t bot_cfg[32] = {9, 2, 32, 0, 1, 1, 0, 0x80, 50, 9, 4, 0, 0, 2, 0x08, 0x06, 0x50, 0,
                                    7, 5, 0x81, 2, 0, 2, 0, 7, 5, 0x02, 2, 0, 2, 0};

/* Returns the end code; checks the footprint unless an allocation failed. */
static yusb_end_t proxy_case(const char *name, const yusb_policy_t *policy, long fail)
{
    yusb_gadget_ops_t ops = {
        .ep0_write = s_ep0_write, .ep0_read = s_ep0_read, .ep0_stall = s_ep0_stall,
        .ep_enable = s_enable, .ep_disable = s_idle, .ep_set_halt = s_idle, .configure = s_none,
        .vbus_draw = s_draw, .link_speed = s_speed, .ep_write = s_write, .ep_read = s_read,
        .ep_cancel = s_cancel,
    };
    yusb_footprint_t f = yusb_proxy_footprint(policy, NULL);
    memset(&sx, 0, sizeof(sx));
    live = 0;
    allocs = 0;
    fail_at = fail;
    tracking = 1;
    yusb_proxy_t *p = yusb_proxy_create(policy, &ops, "1-1", 0);
    if (!p) {
        tracking = 0;
        CHECK(live == 0);
        return YUSB_END_FAULT;
    }
    collect(p);
    uint8_t rep[YUSB_REP_IMPORT_SIZE] = {0};
    yusb_op_encode(YUSB_OP_REP_IMPORT, 0, rep);
    yusb_device_info_t info = {.busnum = 1, .devnum = 2, .speed = YUSB_SPEED_HIGH};
    strcpy(info.busid, "1-1");
    yusb_device_info_encode(&info, rep + 8);
    if (policy->yx_levers) {
        yusb_yx_params_t acc = {1, policy->yx_levers, policy->yx_cache_blocks};
        yusb_yx_accept_put(rep + 8, &acc);
    }
    feed(p, rep, sizeof(rep));
    size_t after_import = live;
    for (int g = 0; g < 64 && yusb_proxy_phase(p) == YUSB_PHASE_FETCH; ++g) {
        collect(p);
        if (!sx.n) break;
        uint32_t seq = sx.seqs[0], len = sx.lens[0];
        memmove(sx.seqs, sx.seqs + 1, --sx.n * 4);
        memmove(sx.lens, sx.lens + 1, sx.n * 4);
        if (len == 18) reply(p, seq, 0, dev_desc, 18);
        else if (len == 9) reply(p, seq, 0, bot_cfg, 9);
        else if (len == 32) reply(p, seq, 0, bot_cfg, 32);
        else reply(p, seq, -YUSB_EPIPE, NULL, 0);
    }
    yusb_proxy_status_t st;
    yusb_proxy_status(p, &st);
    yusb_end_t end = st.end;
    if (fail < 0) {
        CHECK(end == YUSB_END_NONE && yusb_proxy_phase(p) == YUSB_PHASE_READY);
        CHECK(st.yx == (policy->yx_levers != 0));
        yusb_proxy_gadget_started(p, NULL, 0, 0);
        size_t before_cfg = live;
        uint8_t setconf[8] = {0, 9, 1, 0, 0, 0, 0, 0};
        sx.n = 0;
        yusb_proxy_event(p, YUSB_EV_CONTROL, setconf, 0);
        collect(p);
        reply(p, sx.seqs[0], 0, NULL, 0);
        size_t pumps = live - before_cfg;
        printf("%-26s footprint.create=%8zu live after import=%8zu (%5.1f%%) pumps_max=%8zu BOT pumps=%8zu\n",
               name, f.create, after_import, 100.0 * (double)after_import / (double)f.create,
               f.pumps_max, pumps);
        CHECK(after_import <= f.create);
        CHECK(after_import + f.create / 20 >= f.create);   /* tight: within 5% */
        CHECK(pumps <= f.pumps_max);
    }
    yusb_proxy_stop(p, 0);
    yusb_proxy_destroy(p);
    tracking = 0;
    CHECK(live == 0);
    return end;
}

int main(void)
{
    yusb_policy_t pol;
    yusb_policy_defaults(&pol);
    proxy_case("linux defaults", &pol, -1);
    pol.yx_levers = 0x07;
    pol.yx_cache_blocks = 64;
    proxy_case("linux + YX cache 64", &pol, -1);
    /* Fail each allocation of the import in turn: no leak, and a codec
     * failure asks the platform to reconnect without the offer. */
    unsigned unavailable = 0;
    for (long k = 0; k < 64; ++k) {
        yusb_end_t end = proxy_case("linux + YX, failing", &pol, k);
        unavailable += end == YUSB_END_YX_UNAVAILABLE;
    }
    CHECK(unavailable >= 3);
    yusb_policy_defaults(&pol);
    pol.ep0_max = 4096;
    pol.max_transfer = 4096;
    pol.in_depth_bulk = pol.in_depth_interrupt = 2;
    pol.bot_in_slots = 2;
    pol.budget = (yusb_budget_t){4, 4, 6};
    proxy_case("mcu", &pol, -1);
    pol.yx_levers = 0x01;
    proxy_case("mcu + YX fill", &pol, -1);
    pol.yx_levers = 0x03;
    proxy_case("mcu + YX fill,lz4", &pol, -1);
    pol.yx_levers = 0x07;
    pol.yx_cache_blocks = 16;
    proxy_case("mcu + YX cache 16", &pol, -1);
    if (failures) {
        fprintf(stderr, "%d failures\n", failures);
        return 1;
    }
    puts("all footprint tests passed");
    return 0;
}
