/* Device-side export core: a USB/IP server over a host-controller backend
 * (see yusb_export.h). One object per connection:
 *
 *   X_OP -> (DEVLIST: reply, finished) | (IMPORT: busid -> claim -> reply)
 *        -> X_URBS: CMD_SUBMIT / CMD_UNLINK until the transport closes.
 *
 * Memory, all allocated at create (the YX codec at import):
 *  - slots: one small descriptor per URB handed to the backend, with its
 *    reply headers. A slot is reused only after its reply bytes were sent.
 *    One slot is kept for control transfers.
 *  - pool: one byte budget from which each URB's buffer is cut to its own
 *    length (first fit over two arenas: a small control reserve, then the
 *    rest). The backend's DMA alignment holds for every buffer. With YX, a
 *    bulk IN buffer also has room for the encoded reply's headers and
 *    bodies (RAW bodies are sent from the data itself).
 *  - held: CMD_SUBMITs without data that found no slot or no buffer. They
 *    wait in arrival order while the stream keeps being parsed, so a
 *    CMD_UNLINK behind them is still read (a client can park every slot in
 *    reads that never complete and then unlink one).
 *  - notes: RET_UNLINKs that belong to no slot (unknown seqnum, held URB),
 *    each with its own bytes until they are sent.
 * When the held set, the notes, or (for OUT data) a slot and buffer run
 * out, the core stops receiving (TCP backpressure) until bytes are sent or
 * URBs complete. Per endpoint, URBs reach the backend in arrival order.
 */
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "buffers.h"
#include "wire.h"
#include "yusb_export.h"

enum { X_OP, X_BUSID, X_URBS, X_PAYLOAD, X_DONE };
enum { XS_FREE, XS_BUSY, XS_UNLINKING, XS_REPLIED };
enum { A_EP0, A_MAIN, A_COUNT };

#define KEYS 32u              /* endpoint keys: 0 control, ep | 16 * dir_in */
#define IOV_CHUNK 16u         /* stack segments per YX encode step */
#define CHUNK_BYTES (IOV_CHUNK / 2u * YUSB_YX_BLOCK)

typedef struct {
    size_t off, len;          /* in the pool block */
    uint8_t arena;
    bool used;
} extent_t;

typedef struct {
    uint8_t state;
    bool collateral;          /* flushed by another URB's unlink: resubmit */
    bool control;             /* counts against the control reservation */
    bool fits;                /* has a buffer; else completes -ENOMEM */
    uint8_t key;
    yusb_xurb_t urb;
    uint64_t order;           /* submission order, per connection */
    uint32_t unlink_seq;
    extent_t ext;             /* the buffer */
    size_t wire_at;           /* YX reply room at ext.off + wire_at; 0: none */
    uint64_t tx_end;
    uint8_t ret[YUSB_USBIP_HEADER_SIZE];
    uint8_t unlink[YUSB_USBIP_HEADER_SIZE];
} xslot_t;

typedef struct {              /* a held CMD_SUBMIT (IN, or OUT without data) */
    uint32_t seqnum, flags, length;
    int32_t interval;
    yusb_setup_t setup;
    uint8_t ep, type, key;
    bool dir_in;
} xheld_t;

typedef struct {              /* a RET_UNLINK owned by no slot */
    uint64_t tx_end;
    bool used;
    uint8_t raw[YUSB_USBIP_HEADER_SIZE];
} xnote_t;

typedef struct {
    size_t base, size;
    uint16_t *ids;            /* slots holding an extent here, by offset */
    unsigned n;
} arena_t;

#define REPLY_BYTES \
    (YUSB_OP_HEADER_SIZE + 4u + \
     YUSB_EXPORT_MAX_DEVICES * (YUSB_DEVICE_INFO_SIZE + YUSB_INTERFACE_INFO_SIZE * YUSB_EXPORT_MAX_IFACES))

struct yusb_export {
    yusb_export_cfg_t cfg;
    yusb_hc_ops_t ops;
    yusb_export_dev_t devs[YUSB_EXPORT_MAX_DEVICES];
    unsigned ndevs;
    int dev;
    bool claimed, closing, finished, violated;
    bool busy, kick_later;    /* parsing or promoting; a kick is deferred */
    uint32_t devid;
    uint8_t phase;
    rx_t rx;
    txq_t tx;
    ybuf_layout_t layout;
    void *pool;
    arena_t arenas[A_COUNT];
    xslot_t *slots;
    unsigned nslots, nfree, nonctl;
    xheld_t *held;            /* arrival order */
    unsigned nheld;
    xnote_t *notes;           /* nslots entries */
    uint16_t held_on[KEYS];   /* held URBs per endpoint key */
    uint16_t flushed_on[KEYS];/* collateral URBs awaiting resubmission */
    uint64_t submitted;
    int receiving;            /* slot whose OUT payload is arriving */
    yusb_usbip_hdr_t hdr;
    bool hdr_yx;
    bool yx;
    yusb_yx_params_t yxp;
    yusb_yx_enc_t *enc;       /* IN payloads to the client */
    yusb_yx_dec_t *dec;       /* OUT payloads from the client */
    uint64_t now;
    yusb_export_counters_t counters;
    uint8_t reply[REPLY_BYTES];
};

static void xlog(yusb_export_t *x, const char *fmt, ...)
{
    if (!x->ops.log) return;
    char text[200];
    va_list args;
    va_start(args, fmt);
    vsnprintf(text, sizeof(text), fmt, args);
    va_end(args);
    x->ops.log(x->ops.ctx, text);
}

static int violation(yusb_export_t *x, const char *what)
{
    x->counters.violations++;
    x->violated = x->finished = true;
    xlog(x, "closing connection: %s", what);
    return YUSB_ERR_PROTOCOL;
}

/* The transmit ring is sized for the worst case (txq_capacity), so a full
 * ring is a bug; the connection closes rather than lose a reply. */
static bool push(yusb_export_t *x, const void *base, size_t len)
{
    if (txq_push(&x->tx, base, len)) return true;
    if (!x->violated) violation(x, "transmit queue overflow");
    return false;
}

static bool room_for(yusb_export_t *x, size_t segments)
{
    if (txq_room(&x->tx, segments)) return true;
    if (!x->violated) violation(x, "transmit queue overflow");
    return false;
}

void yusb_export_defaults(yusb_export_cfg_t *cfg)
{
    memset(cfg, 0, sizeof(*cfg));
    cfg->max_urbs = 32;
    cfg->max_urb_bytes = 128u * 1024u;   /* Linux usb-storage: 120 KiB per command */
}

static bool cfg_ok(const yusb_export_cfg_t *cfg)
{
    return cfg && cfg->max_urbs && cfg->max_urbs <= 1024 && cfg->max_urb_bytes &&
           cfg->max_urb_bytes <= YUSB_MAX_TRANSFER;
}

/* --- buffer pool -------------------------------------------------------- */

/* Bytes a URB's data needs: IN rounded up to whole 1024-byte packets
 * (yusb_export.h), at least one byte. */
static size_t data_room(uint32_t length, bool dir_in)
{
    size_t n = dir_in ? ((size_t)length + 1023u) / 1024u * 1024u : length;
    return n ? n : 1;
}

/* An extent holding `room` data bytes with YUSB_HEADROOM before them, laid
 * out for the backend's DMA. */
static size_t extent_bytes(const ybuf_layout_t *l, size_t room)
{
    return ybuf_round(l, ybuf_offset(l) + room);
}

static void pool_plan(const yusb_export_cfg_t *cfg, const ybuf_layout_t *l, size_t *ep0, size_t *main)
{
    uint32_t ep0_max = cfg->max_urb_bytes < YUSB_EXPORT_EP0_BYTES ? cfg->max_urb_bytes : YUSB_EXPORT_EP0_BYTES;
    size_t reserve = extent_bytes(l, data_room(ep0_max, true));
    if (!cfg->pool_bytes) {
        unsigned urbs = cfg->max_urbs < YUSB_EXPORT_AUTO_POOL_URBS ? cfg->max_urbs : YUSB_EXPORT_AUTO_POOL_URBS;
        *ep0 = reserve;
        *main = urbs * extent_bytes(l, data_room(cfg->max_urb_bytes, true));
        return;
    }
    size_t total = cfg->pool_bytes / l->hook.align * l->hook.align;
    *ep0 = reserve < total ? reserve : total;
    *main = total - *ep0;
}

/* Bytes ybuf_alloc takes for the pool block. */
static size_t pool_alloc_bytes(const ybuf_layout_t *l, size_t total)
{
    return l->custom ? ybuf_round(l, total ? total : 1) : (total ? total : 1);
}

/* Every slot can hold a reply header, a payload and a RET_UNLINK, every
 * note a RET_UNLINK, plus the operation reply. A YX reply takes up to two
 * segments per 4 KiB block instead of one payload segment; a reply's blocks
 * number at most its buffer / 4096 + 1, and the buffers fit in the pool. */
static size_t txq_capacity(const yusb_export_cfg_t *cfg, size_t pool, bool yx)
{
    size_t cap = 1u + 4u * cfg->max_urbs;
    if (yx) cap += 2u * (pool / YUSB_YX_BLOCK + cfg->max_urbs);
    return cap;
}

/* The YX levers a connection could agree under cfg, normalized (masked,
 * cache capped, CACHE dropped without blocks): it bounds any agreement. */
static bool yx_limits(const yusb_export_cfg_t *cfg, yusb_yx_params_t *limits)
{
    yusb_yx_agree(&cfg->yx, &cfg->yx, limits);
    return yusb_yx_framed(limits);
}

size_t yusb_export_footprint(const yusb_export_cfg_t *cfg, const yusb_buffers_t *buffers)
{
    if (!cfg_ok(cfg)) return 0;
    ybuf_layout_t layout;
    ybuf_layout(&layout, buffers);
    size_t ep0, main;
    pool_plan(cfg, &layout, &ep0, &main);
    yusb_yx_params_t limits;
    bool yx = yx_limits(cfg, &limits);
    size_t n = cfg->max_urbs;
    size_t bytes = sizeof(yusb_export_t) + n * (sizeof(xslot_t) + sizeof(xheld_t) + sizeof(xnote_t)) +
                   A_COUNT * n * sizeof(uint16_t) +
                   txq_capacity(cfg, ep0 + main, yx) * sizeof(yusb_iov_t) +
                   rx_stage_size(cfg->max_urb_bytes) + pool_alloc_bytes(&layout, ep0 + main);
    if (yx)   /* the codec, allocated at import when agreed */
        bytes += sizeof(yusb_yx_enc_t) + sizeof(yusb_yx_dec_t) + yusb_yx_enc_footprint(&limits) +
                 yusb_yx_dec_footprint(&limits);
    return bytes;
}

static extent_t *extent(yusb_export_t *x, unsigned id) { return &x->slots[id].ext; }

static uint8_t *extent_data(yusb_export_t *x, const extent_t *e)
{
    return ybuf_at(&x->layout, x->pool, e->off) + YUSB_HEADROOM;
}

/* First fit by offset. Lengths are multiples of the alignment, so every
 * offset stays aligned. */
static bool extent_alloc(yusb_export_t *x, unsigned a, unsigned id, size_t len)
{
    arena_t *ar = &x->arenas[a];
    if (len > ar->size) return false;
    size_t at = ar->base;
    unsigned i = 0;
    for (; i < ar->n; ++i) {
        const extent_t *e = extent(x, ar->ids[i]);
        if (e->off - at >= len) break;
        at = e->off + e->len;
    }
    if (i == ar->n && ar->base + ar->size - at < len) return false;
    memmove(ar->ids + i + 1, ar->ids + i, (ar->n - i) * sizeof(*ar->ids));
    ar->ids[i] = (uint16_t)id;
    ar->n++;
    *extent(x, id) = (extent_t){at, len, (uint8_t)a, true};
    return true;
}

static void extent_free(yusb_export_t *x, unsigned id)
{
    extent_t *e = extent(x, id);
    if (!e->used) return;
    arena_t *ar = &x->arenas[e->arena];
    for (unsigned i = 0; i < ar->n; ++i) {
        if (ar->ids[i] != id) continue;
        memmove(ar->ids + i, ar->ids + i + 1, (ar->n - i - 1) * sizeof(*ar->ids));
        ar->n--;
        break;
    }
    e->used = false;
}

/* --- create / destroy --------------------------------------------------- */

static bool dev_ok(const yusb_export_dev_t *d)
{
    uint8_t raw[YUSB_DEVICE_INFO_SIZE];
    /* Bounded strings (the encoder also wants ASCII) and an interface list
     * that fits the array the devlist reply is built from. */
    return d->num_ifaces <= YUSB_EXPORT_MAX_IFACES && memchr(d->info.busid, 0, YUSB_BUSID_SIZE) &&
           memchr(d->info.path, 0, YUSB_PATH_SIZE) && !yusb_device_info_encode(&d->info, raw);
}

yusb_export_t *yusb_export_create(const yusb_export_cfg_t *cfg, const yusb_hc_ops_t *ops,
                                  const yusb_export_dev_t *devs, unsigned count)
{
    if (!cfg_ok(cfg) || !ops || !ops->endpoint_type || !ops->submit || !ops->cancel || !devs || !count ||
        count > YUSB_EXPORT_MAX_DEVICES)
        return NULL;
    for (unsigned i = 0; i < count; ++i)
        if (!dev_ok(&devs[i])) return NULL;
    yusb_export_t *x = calloc(1, sizeof(*x));
    if (!x) return NULL;
    x->cfg = *cfg;
    x->ops = *ops;
    memcpy(x->devs, devs, count * sizeof(*devs));
    x->ndevs = count;
    x->dev = -1;
    x->receiving = -1;
    x->nslots = x->nfree = cfg->max_urbs;
    ybuf_layout(&x->layout, ops->buffers);
    size_t ep0, main;
    pool_plan(cfg, &x->layout, &ep0, &main);
    x->arenas[A_EP0] = (arena_t){0, ep0, NULL, 0};
    x->arenas[A_MAIN] = (arena_t){ep0, main, NULL, 0};
    yusb_yx_params_t limits;
    bool ok = (x->slots = calloc(x->nslots, sizeof(*x->slots))) &&
              (x->held = calloc(x->nslots, sizeof(*x->held))) &&
              (x->notes = calloc(x->nslots, sizeof(*x->notes))) &&
              (x->arenas[A_EP0].ids = calloc(x->nslots, sizeof(uint16_t))) &&
              (x->arenas[A_MAIN].ids = calloc(x->nslots, sizeof(uint16_t))) &&
              !txq_init(&x->tx, txq_capacity(cfg, ep0 + main, yx_limits(cfg, &limits))) &&
              !rx_init(&x->rx, rx_stage_size(cfg->max_urb_bytes)) &&
              (x->pool = ybuf_alloc(&x->layout, ep0 + main));
    if (!ok) {
        yusb_export_destroy(x);
        return NULL;
    }
    rx_record(&x->rx, YUSB_OP_HEADER_SIZE);
    x->phase = X_OP;
    return x;
}

static void yx_teardown(yusb_export_t *x)
{
    yusb_yx_enc_free(x->enc);   /* both safe on zeroed or failed state */
    yusb_yx_dec_free(x->dec);
    free(x->enc);
    free(x->dec);
    x->enc = NULL;
    x->dec = NULL;
}

void yusb_export_destroy(yusb_export_t *x)
{
    if (!x) return;
    if (x->claimed && x->ops.release) x->ops.release(x->ops.ctx, (unsigned)x->dev);
    yx_teardown(x);
    ybuf_free(&x->layout, x->pool);
    for (unsigned a = 0; a < A_COUNT; ++a) free(x->arenas[a].ids);
    free(x->notes);
    free(x->held);
    free(x->slots);
    txq_free(&x->tx);
    rx_free(&x->rx);
    free(x);
}

/* --- operation phase --------------------------------------------------- */

static int on_op(yusb_export_t *x)
{
    uint16_t code;
    uint32_t status;
    if (yusb_op_decode(x->rx.record, &code, &status)) return violation(x, "bad USB/IP version");
    if (code == YUSB_OP_REQ_DEVLIST) {
        /* create checked every num_ifaces against YUSB_EXPORT_MAX_IFACES, so
         * the reply fits REPLY_BYTES; its length follows the actual counts. */
        size_t n = yusb_devlist_head_encode(x->ndevs, x->reply);
        for (unsigned i = 0; i < x->ndevs; ++i) {
            yusb_device_info_t info = x->devs[i].info;
            info.num_interfaces = x->devs[i].num_ifaces;
            yusb_device_info_encode(&info, x->reply + n);
            n += YUSB_DEVICE_INFO_SIZE;
            for (unsigned k = 0; k < x->devs[i].num_ifaces; ++k, n += YUSB_INTERFACE_INFO_SIZE)
                yusb_iface_info_encode(&x->devs[i].ifaces[k], x->reply + n);
        }
        x->finished = true;
        x->phase = X_DONE;
        return push(x, x->reply, n) ? YUSB_OK : YUSB_ERR_PROTOCOL;
    }
    if (code != YUSB_OP_REQ_IMPORT) return violation(x, "unknown operation");
    rx_record(&x->rx, YUSB_BUSID_SIZE);
    x->phase = X_BUSID;
    return YUSB_OK;
}

/* Allocate and initialize both codec directions; false on any failure. */
static bool yx_setup(yusb_export_t *x, const yusb_yx_params_t *agreed)
{
    x->enc = calloc(1, sizeof(*x->enc));
    x->dec = calloc(1, sizeof(*x->dec));
    return x->enc && x->dec && !yusb_yx_enc_init(x->enc, agreed) && !yusb_yx_dec_init(x->dec, agreed);
}

static int on_busid(yusb_export_t *x)
{
    char busid[YUSB_BUSID_SIZE];
    if (yusb_busid_decode(x->rx.record, busid)) return violation(x, "bad busid");
    int dev = -1;
    for (unsigned i = 0; i < x->ndevs; ++i)
        if (!strcmp(x->devs[i].info.busid, busid)) dev = (int)i;
    if (dev < 0 || (x->ops.claim && x->ops.claim(x->ops.ctx, (unsigned)dev) < 0)) {
        yusb_op_encode(YUSB_OP_REP_IMPORT, 1, x->reply);
        x->counters.refused++;
        x->finished = true;
        x->phase = X_DONE;
        return push(x, x->reply, YUSB_OP_HEADER_SIZE) ? YUSB_OK : YUSB_ERR_PROTOCOL;
    }
    x->dev = dev;
    x->claimed = true;
    yusb_op_encode(YUSB_OP_REP_IMPORT, 0, x->reply);
    uint8_t *path = x->reply + YUSB_OP_HEADER_SIZE;
    yusb_device_info_encode(&x->devs[dev].info, path);
    yusb_yx_params_t offer, agreed;
    if (x->cfg.yx.levers && yusb_yx_offer_get(x->rx.record, &offer)) {
        yusb_yx_agree(&offer, &x->cfg.yx, &agreed);
        /* Accept only what this connection will use. When no wire lever
         * survives (READAHEAD alone changes no bytes), the path is too long
         * for the trailer, or the codec cannot be set up, the trailer stays
         * zero: no accept means plain USB/IP throughout (ADR 0028 section 2). */
        if (yusb_yx_framed(&agreed) && !yusb_yx_accept_put(path, &agreed) && yx_setup(x, &agreed)) {
            x->yx = true;
            x->yxp = agreed;
        } else {
            yx_teardown(x);
            memset(path + YUSB_PATH_SIZE - 8u, 0, 8);
        }
    }
    x->devid = yusb_devid(&x->devs[dev].info);
    rx_record(&x->rx, YUSB_USBIP_HEADER_SIZE);
    x->phase = X_URBS;
    return push(x, x->reply, YUSB_REP_IMPORT_SIZE) ? YUSB_OK : YUSB_ERR_PROTOCOL;
}

/* --- slots ---------------------------------------------------------------- */

static void set_collateral(yusb_export_t *x, xslot_t *s, bool on)
{
    if (s->collateral == on) return;
    s->collateral = on;
    if (on) x->flushed_on[s->key]++;
    else x->flushed_on[s->key]--;
}

/* Whether a URB can ever get a buffer (else it completes -ENOMEM). */
static bool fits(const yusb_export_t *x, const xheld_t *r)
{
    size_t cap = x->arenas[A_MAIN].size;
    if (r->type == YUSB_XFER_CONTROL && x->arenas[A_EP0].size > cap) cap = x->arenas[A_EP0].size;
    return r->length <= x->cfg.max_urb_bytes &&
           extent_bytes(&x->layout, data_room(r->length, r->dir_in)) <= cap;
}

/* A free slot and, when the URB fits, its buffer: control first from the
 * reserve, then from the rest; other URBs leave one slot for control. A
 * bulk IN URB with YX also gets room for its encoded reply when the pool
 * could ever hold both; otherwise its reply goes plain. */
static xslot_t *acquire(yusb_export_t *x, const xheld_t *r)
{
    bool control = r->type == YUSB_XFER_CONTROL;
    if (!x->nfree || (!control && x->nslots > 1 && x->nonctl >= x->nslots - 1)) return NULL;
    unsigned i = 0;
    while (x->slots[i].state != XS_FREE) ++i;
    xslot_t *s = &x->slots[i];
    size_t len = extent_bytes(&x->layout, data_room(r->length, r->dir_in)), wire_at = 0;
    bool fit = fits(x, r);
    if (fit && x->yx && r->dir_in && yusb_yx_eligible(r->type, r->length)) {
        size_t wire = ybuf_round(&x->layout, yusb_yx_bound(r->length));
        if (wire <= x->arenas[A_MAIN].size - len) {
            wire_at = len;
            len += wire;
        }
    }
    if (fit && !(control && extent_alloc(x, A_EP0, i, len)) && !extent_alloc(x, A_MAIN, i, len))
        return NULL;
    s->state = XS_BUSY;
    s->collateral = false;
    s->control = control;
    s->fits = fit;
    s->wire_at = wire_at;
    s->key = r->key;
    s->tx_end = 0;
    s->urb = (yusb_xurb_t){
        .seqnum = r->seqnum, .ep = r->ep, .dir_in = r->dir_in, .type = r->type, .flags = r->flags,
        .length = r->length, .interval = r->interval, .setup = r->setup,
    };
    if (control) {
        const yusb_setup_t *t = &r->setup;
        if (t->request == YUSB_REQ_CLEAR_FEATURE && t->request_type == YUSB_RECIP_ENDPOINT &&
            t->value == YUSB_FEATURE_ENDPOINT_HALT)
            s->urb.tweak = YUSB_TWEAK_CLEAR_HALT;
        else if (t->request == YUSB_REQ_SET_INTERFACE && t->request_type == YUSB_RECIP_INTERFACE)
            s->urb.tweak = YUSB_TWEAK_SET_INTERFACE;
        else if (t->request == YUSB_REQ_SET_CONFIGURATION && t->request_type == YUSB_RECIP_DEVICE)
            s->urb.tweak = YUSB_TWEAK_SET_CONFIGURATION;
        else if (t->request == YUSB_REQ_SET_FEATURE && t->request_type == YUSB_RT_PORT &&
                 t->value == YUSB_PORT_FEAT_RESET)
            s->urb.tweak = YUSB_TWEAK_RESET_DEVICE;
    }
    s->urb.data = fit ? extent_data(x, &s->ext) : NULL;
    x->nfree--;
    if (!control) x->nonctl++;
    return s;
}

static void release(yusb_export_t *x, xslot_t *s)
{
    if (s->state == XS_FREE) return;
    extent_free(x, (unsigned)(s - x->slots));
    set_collateral(x, s, false);
    s->state = XS_FREE;
    x->nfree++;
    if (!s->control) x->nonctl--;
}

/* RET_SUBMIT and its payload. True when the payload references the URB
 * buffer (it must then live until sent). `extra` segments follow. */
static bool reply(yusb_export_t *x, xslot_t *s, int32_t status, uint32_t actual, size_t extra)
{
    yusb_usbip_hdr_t h = yusb_ret_submit(s->urb.seqnum, status, actual);
    h.number_of_packets = YUSB_NON_ISO_PACKETS;
    yusb_usbip_encode(&h, s->ret);
    x->counters.ret_submits++;
    size_t len = s->urb.dir_in ? actual : 0;
    if (s->urb.dir_in) x->counters.in_bytes += actual;
    if (len && x->yx && s->wire_at && yusb_yx_eligible(s->urb.type, len)) {
        /* Headers and small bodies go to the reply room; RAW bodies are
         * sent from the URB data. */
        if (!room_for(x, 1u + yusb_yx_segments(len) + extra)) return false;
        yusb_yx_frame(s->ret, true);
        if (!push(x, s->ret, sizeof(s->ret))) return false;
        uint8_t *out = (uint8_t *)x->pool + s->ext.off + s->wire_at;
        size_t wire = 0;
        for (size_t at = 0; at < len; at += CHUNK_BYTES) {
            yusb_iov_t seg[IOV_CHUNK];
            size_t w, part = len - at < CHUNK_BYTES ? len - at : CHUNK_BYTES;
            size_t n = yusb_yx_encode_iov(x->enc, s->urb.data + at, part, out + yusb_yx_bound(at), seg,
                                          IOV_CHUNK, &w);
            for (size_t i = 0; i < n; ++i)
                if (!push(x, seg[i].base, seg[i].len)) return false;
            wire += w;
        }
        x->counters.yx_raw_out += len;
        x->counters.yx_wire_out += wire;
        return true;
    }
    if (!room_for(x, 2u + extra)) return false;
    return push(x, s->ret, sizeof(s->ret)) && push(x, s->urb.data, len) && len;
}

static bool push_unlink(yusb_export_t *x, uint8_t *buf, uint32_t seqnum, int32_t status)
{
    yusb_usbip_hdr_t h = yusb_ret_unlink(seqnum, status);
    yusb_usbip_encode(&h, buf);
    return push(x, buf, YUSB_USBIP_HEADER_SIZE);
}

static void submit(yusb_export_t *x, xslot_t *s)
{
    x->counters.submits++;
    s->order = ++x->submitted;
    int rc = x->ops.submit(x->ops.ctx, (unsigned)x->dev, &s->urb);
    if (rc < 0) yusb_export_complete(x, &s->urb, rc, 0, x->now);
}

static void start(yusb_export_t *x, xslot_t *s)
{
    if (s->fits) submit(x, s);
    else yusb_export_complete(x, &s->urb, -YUSB_ENOMEM, 0, x->now);
}

/* Start held URBs in arrival order while slots and buffers allow. One that
 * cannot start yet keeps the later ones of its endpoint waiting, as do
 * flushed URBs that have not been resubmitted. */
static void promote(yusb_export_t *x)
{
    uint32_t waiting = 0;
    unsigned i = 0;
    while (i < x->nheld && !x->closing && !x->finished) {
        xheld_t r = x->held[i];
        uint32_t bit = 1u << r.key;
        xslot_t *s = (waiting & bit) || x->flushed_on[r.key] ? NULL : acquire(x, &r);
        if (!s) {
            waiting |= bit;
            ++i;
            continue;
        }
        memmove(x->held + i, x->held + i + 1, (x->nheld - i - 1) * sizeof(*x->held));
        x->nheld--;
        x->held_on[r.key]--;
        start(x, s);
    }
}

static int on_element(void *owner);

/* Resources were freed: start held URBs and resume a waiting receiver. Runs
 * later when called from inside parsing or promotion. */
static void kick(yusb_export_t *x)
{
    if (x->busy) {
        x->kick_later = true;
        return;
    }
    x->busy = true;
    do {
        x->kick_later = false;
        if (x->closing || x->finished) break;
        promote(x);
        if (x->rx.blocked && rx_resume(&x->rx, on_element, x) < 0 && !x->violated)
            violation(x, "malformed stream");
    } while (x->kick_later);
    x->busy = false;
}

/* --- URBs --------------------------------------------------------------- */

static int on_submit(yusb_export_t *x)
{
    const yusb_usbip_hdr_t *h = &x->hdr;
    if (h->devid != x->devid) return violation(x, "CMD_SUBMIT devid mismatch");
    bool dir_in = h->direction == YUSB_USBIP_DIR_IN;
    unsigned address = h->ep | (dir_in ? 0x80u : 0u);
    int type = h->ep == 0 ? (int)YUSB_XFER_CONTROL
                          : x->ops.endpoint_type(x->ops.ctx, (unsigned)x->dev, address);
    if (type < 0) return violation(x, "CMD_SUBMIT to an endpoint outside the configuration");
    yusb_setup_t setup = yusb_setup_unpack(h->setup);
    if (type == YUSB_XFER_CONTROL && setup.length != h->length) return violation(x, "control wLength mismatch");
    if (type == YUSB_XFER_INT && h->interval <= 0) return violation(x, "interrupt URB interval");
    if (x->hdr_yx && (!x->yx || dir_in || type != YUSB_XFER_BULK || !h->length))
        return violation(x, "YX payload for an ineligible URB");
    xheld_t r = {
        .seqnum = h->seqnum, .flags = h->flags, .length = h->length, .interval = h->interval,
        .setup = setup, .ep = (uint8_t)h->ep, .type = (uint8_t)type,
        .key = (uint8_t)(type == YUSB_XFER_CONTROL ? 0u : h->ep | (dir_in ? 16u : 0u)), .dir_in = dir_in,
    };
    /* A block stream cannot be skipped: its decoder needs the buffer. */
    if (x->hdr_yx && !fits(x, &r)) return violation(x, "oversized YX payload");
    bool payload = !dir_in && h->length;
    xslot_t *s = NULL;
    if (!x->held_on[r.key] && !x->flushed_on[r.key]) s = acquire(x, &r);
    if (!s) {
        /* OUT data follows the header: it needs its buffer now. */
        if (payload || x->nheld == x->nslots) return RX_WAIT;
        x->held[x->nheld++] = r;
        x->held_on[r.key]++;
    }
    if (type == YUSB_XFER_CONTROL && (!yusb_setup_is_in(&setup) || !setup.length) == dir_in)
        xlog(x, "BOGUS control dir");
    rx_record(&x->rx, YUSB_USBIP_HEADER_SIZE);
    if (!s) return YUSB_OK;
    if (payload) {
        x->counters.out_bytes += h->length;
        x->receiving = (int)(s - x->slots);
        x->phase = X_PAYLOAD;
        if (!s->fits) rx_discard(&x->rx, h->length);
        else if (x->hdr_yx) {
            yusb_yx_dec_begin(x->dec, s->urb.data, h->length);
            rx_decode(&x->rx, x->dec);
        } else {
            rx_payload(&x->rx, s->urb.data, h->length);
        }
        return YUSB_OK;
    }
    start(x, s);
    return YUSB_OK;
}

static xnote_t *free_note(yusb_export_t *x)
{
    for (unsigned i = 0; i < x->nslots; ++i)
        if (!x->notes[i].used) return &x->notes[i];
    return NULL;
}

static int on_unlink(yusb_export_t *x)
{
    const yusb_usbip_hdr_t *h = &x->hdr;
    if (h->devid != x->devid) return violation(x, "CMD_UNLINK devid mismatch");
    for (unsigned i = 0; i < x->nslots; ++i) {
        xslot_t *s = &x->slots[i];
        if (s->state != XS_BUSY || s->urb.seqnum != h->unlink_seqnum) continue;
        x->counters.unlinks++;
        rx_record(&x->rx, YUSB_USBIP_HEADER_SIZE);
        s->state = XS_UNLINKING;
        s->unlink_seq = h->seqnum;
        if (x->ops.cancel_flushes_endpoint) {
            /* The flush also ends URBs queued behind this one on the same
             * endpoint; they never started, so they go again. */
            for (unsigned k = 0; k < x->nslots; ++k) {
                xslot_t *t = &x->slots[k];
                if (t->state == XS_BUSY && t->order > s->order && t->key == s->key) set_collateral(x, t, true);
            }
        }
        x->ops.cancel(x->ops.ctx, (unsigned)x->dev, &s->urb);
        return YUSB_OK;
    }
    /* Held (never started) or unknown: answered now, as the stub does. */
    xnote_t *note = free_note(x);
    if (!note) return RX_WAIT;
    x->counters.unlinks++;
    rx_record(&x->rx, YUSB_USBIP_HEADER_SIZE);
    int32_t status = 0;
    for (unsigned i = 0; i < x->nheld; ++i) {
        if (x->held[i].seqnum != h->unlink_seqnum) continue;
        x->held_on[x->held[i].key]--;
        memmove(x->held + i, x->held + i + 1, (x->nheld - i - 1) * sizeof(*x->held));
        x->nheld--;
        status = -YUSB_ECONNRESET;   /* RET_UNLINK only, no RET_SUBMIT */
        x->counters.unlinked++;
        x->kick_later = true;        /* later URBs of that endpoint may start */
        break;
    }
    note->used = true;
    if (!push_unlink(x, note->raw, h->seqnum, status)) return YUSB_ERR_PROTOCOL;
    note->tx_end = x->tx.queued;
    return YUSB_OK;
}

static int on_header(yusb_export_t *x)
{
    uint8_t raw[YUSB_USBIP_HEADER_SIZE];
    memcpy(raw, x->rx.record, sizeof(raw));
    x->hdr_yx = yusb_yx_unframe(raw);
    if (yusb_usbip_decode(raw, &x->hdr)) return violation(x, "malformed USB/IP header");
    if (x->hdr.command == YUSB_CMD_SUBMIT) return on_submit(x);
    if (x->hdr.command == YUSB_CMD_UNLINK) return on_unlink(x);
    return violation(x, "client sent a reply message");
}

static int on_element(void *owner)
{
    yusb_export_t *x = owner;
    switch (x->phase) {
    case X_OP: return on_op(x);
    case X_BUSID: return on_busid(x);
    case X_URBS: return on_header(x);
    case X_PAYLOAD: {
        xslot_t *s = &x->slots[x->receiving];
        x->receiving = -1;
        x->phase = X_URBS;
        if (x->hdr_yx) {
            x->counters.yx_raw_in = x->dec->stats.raw_bytes;
            x->counters.yx_wire_in = x->dec->stats.wire_bytes;
        }
        rx_record(&x->rx, YUSB_USBIP_HEADER_SIZE);
        start(x, s);
        return YUSB_OK;
    }
    default:
        return violation(x, "bytes after the operation finished");
    }
}

void yusb_export_complete(yusb_export_t *x, yusb_xurb_t *urb, int status, uint32_t actual,
                          uint64_t now_ms)
{
    x->now = now_ms;
    xslot_t *s = (xslot_t *)((uint8_t *)urb - offsetof(xslot_t, urb));
    if (actual > urb->length) actual = urb->length;
    bool collateral = s->collateral;
    set_collateral(x, s, false);
    if (x->closing) {
        release(x, s);
        return;
    }
    if (collateral && s->state == XS_BUSY && (status == -YUSB_ECONNRESET || status == -YUSB_ENOENT)) {
        submit(x, s);
        kick(x);   /* the endpoint's held URBs may follow it now */
        return;
    }
    bool referenced = false;
    if (s->state == XS_UNLINKING) {
        if (status == -YUSB_ECONNRESET || status == -YUSB_ENOENT) {
            push_unlink(x, s->unlink, s->unlink_seq, -YUSB_ECONNRESET);
            x->counters.unlinked++;
        } else {
            /* The completion won the race: deliver it, then RET_UNLINK(0). */
            referenced = reply(x, s, status, actual, 1);
            push_unlink(x, s->unlink, s->unlink_seq, 0);
        }
    } else {
        referenced = reply(x, s, status, actual, 0);
    }
    s->state = XS_REPLIED;
    s->tx_end = x->tx.queued;
    if (!referenced) extent_free(x, (unsigned)(s - x->slots));
    kick(x);
}

/* --- transport ---------------------------------------------------------- */

size_t yusb_export_rx_space(yusb_export_t *x, uint8_t **buf)
{
    if (x->finished || x->closing) return 0;
    return rx_space(&x->rx, buf);
}

int yusb_export_rx_done(yusb_export_t *x, size_t n, uint64_t now_ms)
{
    x->now = now_ms;
    if (x->finished || x->closing) return YUSB_ERR_STATE;
    x->busy = true;
    int rc = rx_done(&x->rx, n, on_element, x);
    x->busy = false;
    if (rc < 0 && !x->violated) violation(x, "malformed stream");
    if (rc >= 0 && x->kick_later) kick(x);
    return rc < 0 || x->violated ? YUSB_ERR_PROTOCOL : YUSB_OK;
}

size_t yusb_export_tx_iov(const yusb_export_t *x, yusb_iov_t *iov, size_t max)
{
    return txq_iov(&x->tx, iov, max);
}

void yusb_export_tx_done(yusb_export_t *x, size_t n)
{
    txq_done(&x->tx, n);
    for (unsigned i = 0; i < x->nslots; ++i) {
        xslot_t *s = &x->slots[i];
        if (s->state == XS_REPLIED && s->tx_end <= x->tx.sent) release(x, s);
        if (x->notes[i].used && x->notes[i].tx_end <= x->tx.sent) x->notes[i].used = false;
    }
    kick(x);
}

bool yusb_export_finished(const yusb_export_t *x) { return x->finished; }

void yusb_export_close(yusb_export_t *x)
{
    if (x->closing) return;
    x->closing = true;
    x->nheld = 0;
    memset(x->held_on, 0, sizeof(x->held_on));
    for (unsigned i = 0; i < x->nslots; ++i) {
        xslot_t *s = &x->slots[i];
        if ((s->state == XS_BUSY || s->state == XS_UNLINKING) && (int)i != x->receiving)
            x->ops.cancel(x->ops.ctx, (unsigned)x->dev, &s->urb);
        else
            release(x, s);   /* never submitted, or answered */
    }
    x->receiving = -1;
}

bool yusb_export_idle(const yusb_export_t *x)
{
    for (unsigned i = 0; i < x->nslots; ++i)
        if (x->slots[i].state == XS_BUSY || x->slots[i].state == XS_UNLINKING) return false;
    return true;
}

int yusb_export_device(const yusb_export_t *x) { return x->dev; }

bool yusb_export_yx(const yusb_export_t *x, yusb_yx_params_t *agreed)
{
    if (x->yx && agreed) *agreed = x->yxp;
    return x->yx;
}

const yusb_export_counters_t *yusb_export_counters(const yusb_export_t *x) { return &x->counters; }
