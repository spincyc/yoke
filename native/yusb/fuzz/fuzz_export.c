/* libFuzzer: the device-side export core with the simulated disk behind
 * it. Input is the client's byte stream (after an optional valid import,
 * with or without a YX offer), cut into random recv sizes, with the
 * transport closing at a random point.
 *
 * Header bytes:
 *   data[0]: bits 0-3 max_urbs - 1; bits 4-6 the buffer pool (automatic
 *            down to a few bytes); bit 7 max_urb_bytes 4 KiB (else 64 KiB).
 *   data[1]: bits 0-3 YX levers; bit 4 a 64-byte DMA alignment hook;
 *            bit 5 a flushing backend (cancel_flushes_endpoint: a cancel
 *            also ends the URBs queued behind it); bit 6 prefix a valid
 *            import; bit 7 offer YX in it.
 *   data[2]: where the transport closes.
 * The stream bytes also pace the transmitter (partial sends, or none, so
 * slots, held URBs and unlink replies pile up) and the disk's completions
 * (delivered late or at once), so the full-slot, held-submit, unlink and
 * tiny-pool paths all run. A queued reply that does not fit the transmit
 * ring is a sizing bug and aborts.
 *
 * Progress oracle (data[1] low nibble 0xf): the input instead drives a
 * well-formed client against a device that completes every write and
 * control request at once but parks every read until it chooses to
 * answer one; max_held is automatic or tiny (data[0] bits 4-6). However
 * many reads the client leaves outstanding, every byte it sends must be
 * parsed: the stream never stops at a read, and writes never wait on
 * parked reads (yusb_export.h). Reads beyond the held set fail with
 * RET_SUBMIT(-ENOMEM). At the end the device answers everything, and every
 * CMD_SUBMIT must have exactly one answer (RET_SUBMIT, or RET_UNLINK
 * -ECONNRESET when unlinked first) and every CMD_UNLINK exactly one
 * RET_UNLINK (status 0 only for an unknown seqnum or a URB already
 * answered before it). */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "simdev.h"

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size);

static uint8_t disk[512 * 128];

static void on_log(void *ctx, const char *message)
{
    (void)ctx;
    if (strstr(message, "transmit queue")) abort();
}

/* The flushing backend: the disk, with cancel ending every URB submitted
 * after the cancelled one on its endpoint (the disk ignores those it has
 * already completed). */
static struct {
    yusb_hc_ops_t disk;
    yusb_xurb_t *order[64];
    unsigned n;
} flush;

static int flush_submit(void *ctx, unsigned dev, yusb_xurb_t *urb)
{
    unsigned k = 0;
    for (unsigned i = 0; i < flush.n; ++i)
        if (flush.order[i] != urb) flush.order[k++] = flush.order[i];
    flush.n = k;
    if (flush.n < 64) flush.order[flush.n++] = urb;
    return flush.disk.submit(ctx, dev, urb);
}

static void flush_cancel(void *ctx, unsigned dev, yusb_xurb_t *urb)
{
    bool after = false;
    for (unsigned i = 0; i < flush.n; ++i) {
        yusb_xurb_t *u = flush.order[i];
        if (u == urb) after = true;
        else if (!after || u->ep != urb->ep || u->dir_in != urb->dir_in) continue;
        flush.disk.cancel(ctx, dev, u);
    }
    if (!after) flush.disk.cancel(ctx, dev, urb);
}

static void *hook_alloc(void *ctx, size_t size, size_t align)
{
    (void)ctx;
    void *p = aligned_alloc(align, size);
    if (p) memset(p, 0, size);
    return p;
}

static void hook_free(void *ctx, void *p) { (void)ctx; free(p); }

/* Send up to `limit` queued bytes. */
static void send_some(yusb_export_t *x, size_t limit)
{
    yusb_iov_t iov[16];
    size_t n;
    while (limit && (n = yusb_export_tx_iov(x, iov, 16))) {
        size_t total = 0;
        for (size_t i = 0; i < n && total < limit; ++i)
            total += iov[i].len < limit - total ? iov[i].len : limit - total;
        yusb_export_tx_done(x, total);
        limit -= total;
    }
}

/* --- progress oracle ------------------------------------------------------ */

#define P_SEQS 4096u
#define P_DEVID (1u << 16 | 2u)
enum { K_IN, K_OUT, K_CTRL, K_UNLINK };

typedef struct {
    yusb_xurb_t *u;
    int status;
    uint32_t actual;
} pm_done_t;

static struct {
    yusb_xurb_t *parked[64];
    unsigned nparked;
    pm_done_t done[256];
    unsigned ndone;
    bool parse;                /* after the import reply */
    uint8_t kind[P_SEQS];
    uint32_t victim[P_SEQS];   /* K_UNLINK: the seqnum unlinked */
    uint8_t answers[P_SEQS];   /* RET_SUBMIT for submits, RET_UNLINK for unlinks */
    bool unlinked[P_SEQS];     /* answered by RET_UNLINK(-ECONNRESET) */
    bool unlinking[P_SEQS];
    uint32_t length[P_SEQS];
    bool dir_in[P_SEQS];
    uint8_t rx[1 << 17];       /* replies not yet parsed */
    size_t have;
} pm;

static void pm_done(yusb_xurb_t *u, int status, uint32_t actual)
{
    if (pm.ndone == 256) abort();
    pm.done[pm.ndone].u = u;
    pm.done[pm.ndone].status = status;
    pm.done[pm.ndone++].actual = actual;
}

static int pm_type(void *c, unsigned d, unsigned a)
{
    (void)c; (void)d;
    return a == 0x81 || a == 0x02 || a == 0x04 ? (int)YUSB_XFER_BULK : a == 0x83 ? (int)YUSB_XFER_INT : -1;
}

static int pm_submit(void *c, unsigned d, yusb_xurb_t *u)
{
    (void)c; (void)d;
    if (u->dir_in && u->type != YUSB_XFER_CONTROL) {
        if (pm.nparked == 64) abort();
        pm.parked[pm.nparked++] = u;
    } else {
        if (u->dir_in) memset(u->data, 0x12, u->length);
        pm_done(u, 0, u->length);
    }
    return 0;
}

static void pm_cancel(void *c, unsigned d, yusb_xurb_t *u)
{
    (void)c; (void)d;
    for (unsigned i = 0; i < pm.nparked; ++i) {
        if (pm.parked[i] != u) continue;
        pm.parked[i] = pm.parked[--pm.nparked];
        pm_done(u, -YUSB_ECONNRESET, 0);
        return;
    }
}

/* The device answers parked read i with data. */
static void pm_answer(unsigned i, uint32_t actual)
{
    yusb_xurb_t *u = pm.parked[i];
    pm.parked[i] = pm.parked[--pm.nparked];
    if (actual > u->length) actual = u->length;
    memset(u->data, 0x34, actual);
    pm_done(u, 0, actual);
}

/* Deliver completions, send every reply and account for it; true if
 * anything happened. */
static bool pm_settle(yusb_export_t *x)
{
    bool moved = false;
    while (pm.ndone) {
        unsigned n = pm.ndone;
        pm_done_t batch[256];
        memcpy(batch, pm.done, n * sizeof(batch[0]));
        pm.ndone = 0;
        for (unsigned i = 0; i < n; ++i) yusb_export_complete(x, batch[i].u, batch[i].status, batch[i].actual, 0);
        moved = true;
    }
    yusb_iov_t iov[16];
    size_t n;
    while ((n = yusb_export_tx_iov(x, iov, 16))) {
        size_t total = 0;
        for (size_t i = 0; i < n; ++i) {
            if (pm.have + iov[i].len > sizeof(pm.rx)) abort();
            memcpy(pm.rx + pm.have, iov[i].base, iov[i].len);
            pm.have += iov[i].len;
            total += iov[i].len;
        }
        yusb_export_tx_done(x, total);
        moved = true;
    }
    size_t pos = 0;
    while (pm.parse && pm.have - pos >= YUSB_USBIP_HEADER_SIZE) {
        yusb_usbip_hdr_t h;
        if (yusb_usbip_decode(pm.rx + pos, &h) || h.seqnum >= P_SEQS) abort();
        size_t payload = h.command == YUSB_RET_SUBMIT && pm.dir_in[h.seqnum] ? h.actual_length : 0;
        if (pm.have - pos < YUSB_USBIP_HEADER_SIZE + payload) break;
        if (h.command == YUSB_RET_SUBMIT) {
            if (pm.kind[h.seqnum] == K_UNLINK || h.actual_length > pm.length[h.seqnum]) abort();
            if (h.status == -YUSB_ENOMEM) {   /* held set full: holdable submits only */
                if (h.actual_length || (!pm.dir_in[h.seqnum] && pm.length[h.seqnum])) abort();
            } else if (h.status || (!pm.dir_in[h.seqnum] && h.actual_length != pm.length[h.seqnum])) {
                abort();
            }
        } else if (h.command == YUSB_RET_UNLINK) {
            if (pm.kind[h.seqnum] != K_UNLINK) abort();
            uint32_t v = pm.victim[h.seqnum];
            if (h.status == -YUSB_ECONNRESET) {
                if (v >= P_SEQS / 2 || pm.kind[v] != K_IN || pm.unlinked[v]) abort();
                pm.unlinked[v] = true;
            }
            else if (h.status || (v < P_SEQS / 2 && !pm.answers[v])) abort();   /* unknown or answered */
        } else {
            abort();
        }
        if (++pm.answers[h.seqnum] > 1) abort();
        pos += YUSB_USBIP_HEADER_SIZE + payload;
    }
    memmove(pm.rx, pm.rx + pos, pm.have - pos);
    pm.have -= pos;
    return moved;
}

/* Feed bytes; settle until they are all parsed (the core takes more:
 * staged bytes wait while it holds the stream). The device completes all
 * but reads, so a stop that outlasts settling is a lost write. */
static void pm_feed(yusb_export_t *x, const uint8_t *msg, size_t len)
{
    size_t off = 0;
    for (int idle = 0;;) {
        uint8_t *buf;
        size_t space = yusb_export_rx_space(x, &buf), n = len - off < space ? len - off : space;
        if (off == len && space) break;
        if (n) {
            memcpy(buf, msg + off, n);
            if (yusb_export_rx_done(x, n, 0) < 0) abort();
            off += n;
        }
        bool moved = pm_settle(x);
        idle = n || moved ? 0 : idle + 1;
        if (idle > 2) abort();   /* the stream stopped for good */
    }
    pm_settle(x);
}

static void progress(const uint8_t *data, size_t size)
{
    memset(&pm, 0, sizeof(pm));
    yusb_export_cfg_t cfg;
    yusb_export_defaults(&cfg);
    cfg.max_urbs = (uint16_t)(3 + data[0] % 14);
    cfg.max_held = (uint16_t)((data[0] >> 4) & 7);
    cfg.max_urb_bytes = 4096;
    yusb_buffers_t hook = {64, hook_alloc, hook_free, NULL};
    yusb_hc_ops_t ops = {.endpoint_type = pm_type, .submit = pm_submit, .cancel = pm_cancel, .log = on_log};
    if (data[0] & 0x80) ops.buffers = &hook;
    yusb_export_dev_t dev;
    memset(&dev, 0, sizeof(dev));
    strcpy(dev.info.busid, "1-1");
    dev.info.busnum = 1;
    dev.info.devnum = 2;
    yusb_export_t *x = yusb_export_create(&cfg, &ops, &dev, 1);
    if (!x) abort();
    uint8_t imp[YUSB_REQ_IMPORT_SIZE];
    yusb_req_import_encode("1-1", imp);
    pm_feed(x, imp, sizeof(imp));
    if (pm.have != YUSB_REP_IMPORT_SIZE) abort();
    pm.have = 0;
    pm.parse = true;
    static uint8_t msg[16 * (48 + 4096)];
    uint32_t seq = 1, unknown = P_SEQS / 2;
    size_t at = 1;
    while (at + 2 < size && seq + 16 < P_SEQS / 2) {
        uint8_t act = data[at++], count = (uint8_t)(1 + data[at++] % 8);
        if (act & 0x80 && pm.nparked) pm_answer(act % pm.nparked, (uint32_t)(act & 0x7f) * 5u);
        pm_settle(x);
        size_t len = 0;
        bool control = false;
        for (unsigned c = 0; c < count && at < size; ++c) {
            uint8_t b = data[at++];
            unsigned op = b % 8;
            uint32_t n = (uint32_t)(at < size ? data[at] : b) * 17u % 4097u;
            yusb_usbip_hdr_t h;
            if (op <= 2) {
                unsigned ep = b & 8 ? 3 : 1;
                h = yusb_cmd_submit(seq, P_DEVID, 1, ep, YUSB_URB_DIR_IN, 1 + n % 512, ep == 3 ? 8 : 0, NULL);
                pm.kind[seq] = K_IN;
            } else if (op <= 4) {
                h = yusb_cmd_submit(seq, P_DEVID, 0, b & 8 ? 4 : 2, 0, n, 0, NULL);
                pm.kind[seq] = K_OUT;
            } else if (op == 5) {
                if (control) continue;
                control = true;
                static const uint8_t get_dev[8] = {0x80, 6, 0, 1, 0, 0, 18, 0};
                static const uint8_t coding[8] = {0x21, 0x20, 0, 0, 0, 0, 7, 0};
                bool in = b & 8;
                h = yusb_cmd_submit(seq, P_DEVID, in, 0, in ? YUSB_URB_DIR_IN : 0, in ? 18 : 7, 0,
                                    in ? get_dev : coding);
                pm.kind[seq] = K_CTRL;
            } else {
                uint32_t v = unknown++;
                if (op == 6) {   /* an outstanding read, else an unknown seqnum */
                    for (uint32_t k = 0, s = 1 + n % seq; k < seq; ++k, s = s % seq + 1)
                        if (s < seq && pm.kind[s] == K_IN && !pm.answers[s] && !pm.unlinking[s]) {
                            v = s;
                            break;
                        }
                }
                h = yusb_cmd_unlink(seq, P_DEVID, v);
                pm.kind[seq] = K_UNLINK;
                pm.victim[seq] = v;
                if (v < P_SEQS / 2) pm.unlinking[v] = true;
            }
            yusb_usbip_encode(&h, msg + len);
            len += YUSB_USBIP_HEADER_SIZE;
            pm.length[seq] = h.command == YUSB_CMD_SUBMIT ? h.length : 0;
            pm.dir_in[seq] = h.command == YUSB_CMD_SUBMIT && h.direction == YUSB_USBIP_DIR_IN;
            if (h.command == YUSB_CMD_SUBMIT && h.direction == YUSB_USBIP_DIR_OUT) {
                memset(msg + len, (int)seq, h.length);
                len += h.length;
            }
            seq++;
        }
        pm_feed(x, msg, len);
    }
    /* The device answers every read; everything must be answered once. */
    for (unsigned round = 0; round < P_SEQS && (pm.nparked || pm.ndone); ++round) {   /* one per held read */
        while (pm.nparked) pm_answer(0, 3);
        pm_settle(x);
    }
    for (uint32_t s = 1; s < seq; ++s) {
        bool ok = pm.kind[s] == K_IN ? pm.answers[s] + pm.unlinked[s] == 1 : pm.answers[s] == 1;
        if (pm.kind[s] == K_IN && pm.unlinked[s] && !pm.unlinking[s]) ok = false;
        if (!ok) abort();
    }
    yusb_export_close(x);
    if (!yusb_export_idle(x)) abort();
    yusb_export_destroy(x);
}

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)
{
    static const size_t pools[8] = {0, 64, 200, 1100, 4500, 9000, 20000, 140000};
    if (size < 3) return 0;
    if ((data[1] & 0x0f) == 0x0f) {
        progress(data, size);
        return 0;
    }
    simdev_t *dev = simdev_create(disk, sizeof(disk));
    if (!dev) return 0;
    yusb_export_dev_t desc;
    simdev_describe(dev, &desc, "1-1");
    yusb_export_cfg_t cfg;
    yusb_export_defaults(&cfg);
    cfg.max_urbs = (uint16_t)(1 + data[0] % 16);
    cfg.pool_bytes = pools[(data[0] >> 4) & 7];
    cfg.max_urb_bytes = data[0] & 0x80 ? 4096 : 65536;
    cfg.yx = (yusb_yx_params_t){YUSB_YX_VERSION, (uint8_t)(data[1] & YUSB_YX_LEVERS), 8};
    yusb_buffers_t hook = {64, hook_alloc, hook_free, NULL};
    yusb_hc_ops_t ops = *simdev_ops(dev);
    ops.log = on_log;
    if (data[1] & 0x10) ops.buffers = &hook;
    if (data[1] & 0x20) {
        flush.disk = ops;
        flush.n = 0;
        ops.submit = flush_submit;
        ops.cancel = flush_cancel;
        ops.cancel_flushes_endpoint = true;
    }
    yusb_export_t *x = yusb_export_create(&cfg, &ops, &desc, 1);
    if (!x) { simdev_destroy(dev); return 0; }
    uint8_t prefix[YUSB_REQ_IMPORT_SIZE];
    yusb_req_import_encode("1-1", prefix);
    if (data[1] & 0x80) {
        yusb_yx_params_t offer = {YUSB_YX_VERSION, (uint8_t)(data[1] & YUSB_YX_LEVERS), 8};
        yusb_yx_offer_put(prefix + YUSB_OP_HEADER_SIZE, &offer);
    }
    size_t close_at = size > 4 ? data[2] * (size / 256 + 1) : size;
    const uint8_t *stream[2] = {prefix, data};
    size_t lens[2] = {data[1] & 0x40 ? sizeof(prefix) : 0, size};
    size_t offs[2] = {0, 3};
    unsigned step = 0;
    for (int part = 0; part < 2; ++part) {
        while (offs[part] < lens[part]) {
            if (part && offs[part] >= close_at) goto closed;
            uint8_t pace = data[(offs[part] + step++) % size];
            uint8_t *buf;
            size_t space = yusb_export_rx_space(x, &buf);
            if (!space) {
                /* Held: send everything and deliver completions, then try
                 * again; still held means URBs are parked for good. */
                send_some(x, SIZE_MAX);
                simdev_drain(dev, x, 0);
                send_some(x, SIZE_MAX);
                if (!yusb_export_rx_space(x, &buf)) goto closed;
                continue;
            }
            size_t chunk = 1 + pace % 97;
            size_t n = lens[part] - offs[part];
            if (n > chunk) n = chunk;
            if (n > space) n = space;
            memcpy(buf, stream[part] + offs[part], n);
            offs[part] += n;
            int rc = yusb_export_rx_done(x, n, 0);
            if (pace & 0x80) simdev_drain(dev, x, 0);   /* else completions wait */
            if (pace & 0x40) send_some(x, (size_t)(pace & 0x3f) * 13u);
            if (rc < 0 || yusb_export_finished(x)) goto closed;
        }
    }
closed:
    send_some(x, (size_t)data[2] * 7u);   /* a send in flight at close */
    yusb_export_close(x);
    simdev_drain(dev, x, 0);
    if (!yusb_export_idle(x)) abort();
    yusb_export_destroy(x);
    simdev_destroy(dev);
    return 0;
}
