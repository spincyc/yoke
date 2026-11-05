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
 * ring is a sizing bug and aborts. */
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

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)
{
    static const size_t pools[8] = {0, 64, 200, 1100, 4500, 9000, 20000, 140000};
    if (size < 3) return 0;
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
