/* Simulated BOT mass-storage device (see simdev.h). */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "simdev.h"

#define PACKET 512u
#define QUEUE 64u
#define DONE_MAX 512u
#define IN_MAX (1u << 20)
#define VENDOR 0x1209u
#define PRODUCT 0x5eefu

enum { P_CBW, P_DATA_OUT, P_IN };

typedef struct {
    yusb_xurb_t *urb;
    int status;
    uint32_t actual;
} done_t;

struct simdev {
    yusb_hc_ops_t ops;
    uint8_t *disk;
    size_t size;
    bool claimed;
    uint8_t config;
    yusb_xurb_t *in_q[QUEUE];
    uint32_t in_fill[QUEUE];
    unsigned in_n;
    done_t done[DONE_MAX];
    unsigned done_n;
    int phase;
    bool halt_in, halt_out, wedged;
    uint8_t tag[4];
    uint32_t expected;
    uint8_t cdb[16];
    uint8_t *in_data;
    size_t in_len, in_pos;
    bool zlp, csw_pending;
    uint8_t csw[13];
    size_t out_len;
    uint8_t *out_data;
    uint8_t sense_key, asc;
};

static const uint8_t device_desc[18] = {
    18, 1, 0x00, 0x02, 0, 0, 0, 64, VENDOR & 0xff, VENDOR >> 8, PRODUCT & 0xff, PRODUCT >> 8,
    0x00, 0x01, 1, 2, 3, 1,
};
static const uint8_t qualifier_desc[10] = {10, 6, 0x00, 0x02, 0, 0, 0, 64, 1, 0};

static size_t config_desc(uint8_t *out, bool other_speed)
{
    unsigned mp = other_speed ? 64 : PACKET;
    const uint8_t d[32] = {
        9, other_speed ? 7 : 2, 32, 0, 1, 1, 0, 0x80, 50,
        9, 4, 0, 0, 2, 0x08, 0x06, 0x50, 0,
        7, 5, 0x81, 2, (uint8_t)(mp & 0xff), (uint8_t)(mp >> 8), 0,
        7, 5, 0x02, 2, (uint8_t)(mp & 0xff), (uint8_t)(mp >> 8), 0,
    };
    memcpy(out, d, sizeof(d));
    return sizeof(d);
}

static size_t string_desc(uint8_t *out, unsigned index)
{
    static const char *const text[] = {NULL, "Yoke", "Export disk", "YOKEEXP00001"};
    if (index == 0) {
        const uint8_t langs[4] = {4, 3, 0x09, 0x04};
        memcpy(out, langs, 4);
        return 4;
    }
    if (index >= 4) return 0;
    size_t n = strlen(text[index]);
    out[0] = (uint8_t)(2 + 2 * n);
    out[1] = 3;
    for (size_t i = 0; i < n; ++i) {
        out[2 + 2 * i] = (uint8_t)text[index][i];
        out[3 + 2 * i] = 0;
    }
    return 2 + 2 * n;
}

static void complete(simdev_t *d, yusb_xurb_t *urb, int status, uint32_t actual)
{
    if (d->done_n < DONE_MAX) d->done[d->done_n++] = (done_t){urb, status, actual};
}

static void fail_in(simdev_t *d, int status)
{
    for (unsigned i = 0; i < d->in_n; ++i) complete(d, d->in_q[i], status, d->in_fill[i]);
    d->in_n = 0;
}

static void pop_in(simdev_t *d)
{
    memmove(d->in_q, d->in_q + 1, (d->in_n - 1) * sizeof(d->in_q[0]));
    memmove(d->in_fill, d->in_fill + 1, (d->in_n - 1) * sizeof(d->in_fill[0]));
    d->in_n--;
}

/* Feed queued IN URBs packet by packet: data, an optional ZLP, the CSW. */
static void pump_in(simdev_t *d)
{
    while (d->in_n) {
        if (d->halt_in) { fail_in(d, -YUSB_EPIPE); return; }
        yusb_xurb_t *urb = d->in_q[0];
        const uint8_t *src;
        size_t packet;
        if (d->in_pos < d->in_len) {
            packet = d->in_len - d->in_pos < PACKET ? d->in_len - d->in_pos : PACKET;
            src = d->in_data + d->in_pos;
        } else if (d->zlp) {
            packet = 0;
            src = NULL;
        } else if (d->csw_pending) {
            packet = sizeof(d->csw);
            src = d->csw;
        } else {
            return;
        }
        uint32_t fill = d->in_fill[0];
        if (packet > urb->length - fill) {          /* babble */
            complete(d, urb, -YUSB_EOVERFLOW, fill);
            pop_in(d);
            continue;
        }
        if (packet) memcpy(urb->data + fill, src, packet);
        fill += (uint32_t)packet;
        d->in_fill[0] = fill;
        if (d->in_pos < d->in_len) d->in_pos += packet;
        else if (d->zlp) d->zlp = false;
        else { d->csw_pending = false; d->phase = P_CBW; }
        if (packet < PACKET || fill == urb->length) {
            complete(d, urb, 0, fill);
            pop_in(d);
        }
    }
}

static void put32be(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v >> 24); p[1] = (uint8_t)(v >> 16); p[2] = (uint8_t)(v >> 8); p[3] = (uint8_t)v;
}

static void csw(simdev_t *d, uint32_t residue, uint8_t status)
{
    memcpy(d->csw, "USBS", 4);
    memcpy(d->csw + 4, d->tag, 4);
    for (int i = 0; i < 4; ++i) d->csw[8 + i] = (uint8_t)(residue >> (8 * i));
    d->csw[12] = status;
    d->csw_pending = true;
    d->phase = P_IN;
}

static void sense(simdev_t *d, uint8_t key, uint8_t asc)
{
    d->sense_key = key;
    d->asc = asc;
}

/* Answer a data-in command with n produced bytes (or fail). */
static void respond(simdev_t *d, size_t n, bool ok)
{
    if (!ok && d->expected) {
        /* BOT case Hi > Dn on failure: STALL bulk-in, then the CSW. */
        d->in_len = d->in_pos = 0;
        d->zlp = false;
        csw(d, d->expected, 1);
        d->halt_in = true;
        return;
    }
    size_t send = n < d->expected ? n : d->expected;
    d->in_len = send;
    d->in_pos = 0;
    d->zlp = send < d->expected && send % PACKET == 0 && send;
    csw(d, d->expected - (uint32_t)send, ok ? 0 : 1);
}

static uint32_t be32(const uint8_t *p)
{
    return (uint32_t)p[0] << 24 | (uint32_t)p[1] << 16 | (uint32_t)p[2] << 8 | p[3];
}

static void execute(simdev_t *d, bool data_in)
{
    const uint8_t *c = d->cdb;
    uint8_t *o = d->in_data;
    uint32_t blocks = (uint32_t)(d->size / 512);
    if (!data_in && d->expected) {                 /* data-out phase first */
        d->phase = P_DATA_OUT;
        d->out_len = 0;
        return;
    }
    switch (c[0]) {
    case 0x00: case 0x1e: case 0x1b: case 0x35:    /* TUR, PREVENT, START STOP, SYNC */
        sense(d, 0, 0);
        respond(d, 0, true);
        return;
    case 0x03: {                                   /* REQUEST SENSE */
        memset(o, 0, 18);
        o[0] = 0x70; o[2] = d->sense_key; o[7] = 10; o[12] = d->asc;
        sense(d, 0, 0);
        respond(d, 18 < c[4] ? 18 : c[4], true);
        return;
    }
    case 0x12:                                     /* INQUIRY */
        if (c[1] & 1) { sense(d, 5, 0x24); respond(d, 0, false); return; }
        memset(o, 0, 36);
        o[1] = 0x80; o[2] = 0x02; o[3] = 0x02; o[4] = 31;
        memcpy(o + 8, "YOKE    Export disk     0.1 ", 28);
        respond(d, 36 < c[4] ? 36 : c[4], true);
        return;
    case 0x1a:                                     /* MODE SENSE(6) */
        memset(o, 0, 4);
        o[0] = 3;
        respond(d, 4, true);
        return;
    case 0x5a:                                     /* MODE SENSE(10) */
        memset(o, 0, 8);
        o[1] = 6;
        respond(d, 8, true);
        return;
    case 0x23:                                     /* READ FORMAT CAPACITIES */
        memset(o, 0, 12);
        o[3] = 8;
        put32be(o + 4, blocks);
        o[8] = 2; o[10] = 2;
        respond(d, 12, true);
        return;
    case 0x25:                                     /* READ CAPACITY(10) */
        put32be(o, blocks - 1);
        put32be(o + 4, 512);
        respond(d, 8, true);
        return;
    case 0x28: {                                   /* READ(10) */
        uint32_t lba = be32(c + 2), count = (uint32_t)(c[7] << 8 | c[8]);
        size_t bytes = (size_t)count * 512;
        if ((uint64_t)lba + count > blocks || bytes > IN_MAX) {
            sense(d, 5, 0x21);
            respond(d, 0, false);
            return;
        }
        memcpy(o, d->disk + (size_t)lba * 512, bytes);
        respond(d, bytes, true);
        return;
    }
    default:
        sense(d, 5, 0x20);                         /* invalid command opcode */
        respond(d, 0, false);
        return;
    }
}

static void finish_out(simdev_t *d)
{
    const uint8_t *c = d->cdb;
    uint32_t blocks = (uint32_t)(d->size / 512);
    uint8_t status = 1;
    if (c[0] == 0x2a) {
        uint32_t lba = be32(c + 2), count = (uint32_t)(c[7] << 8 | c[8]);
        if ((uint64_t)lba + count <= blocks && (size_t)count * 512 <= d->out_len) {
            memcpy(d->disk + (size_t)lba * 512, d->out_data, (size_t)count * 512);
            status = 0;
            sense(d, 0, 0);
        } else {
            sense(d, 5, 0x21);
        }
    } else {
        sense(d, 5, 0x20);
    }
    d->in_len = d->in_pos = 0;
    d->zlp = false;
    csw(d, status ? d->expected : 0, status);
}

static void out(simdev_t *d, yusb_xurb_t *urb)
{
    if (d->phase == P_DATA_OUT) {
        size_t take = urb->length;
        if (take > d->expected - d->out_len) take = d->expected - d->out_len;
        if (d->out_len + take <= IN_MAX) memcpy(d->out_data + d->out_len, urb->data, take);
        d->out_len += take;
        complete(d, urb, 0, urb->length);
        if (d->out_len >= d->expected) finish_out(d);
        return;
    }
    const uint8_t *p = urb->data;
    if (d->phase != P_CBW || urb->length != 31 || memcmp(p, "USBC", 4) || p[14] < 1 || p[14] > 16) {
        /* Invalid CBW: stall both pipes until reset recovery. */
        d->wedged = d->halt_in = d->halt_out = true;
        complete(d, urb, -YUSB_EPIPE, 0);
        return;
    }
    memcpy(d->tag, p + 4, 4);
    d->expected = (uint32_t)p[8] | (uint32_t)p[9] << 8 | (uint32_t)p[10] << 16 | (uint32_t)p[11] << 24;
    memset(d->cdb, 0, sizeof(d->cdb));
    memcpy(d->cdb, p + 15, p[14]);
    complete(d, urb, 0, 31);
    execute(d, p[12] & 0x80);
}

static void bot_reset(simdev_t *d)
{
    d->phase = P_CBW;
    d->in_len = d->in_pos = 0;
    d->zlp = d->csw_pending = false;
    d->wedged = false;
}

static int control(simdev_t *d, yusb_xurb_t *urb)
{
    const yusb_setup_t *s = &urb->setup;
    uint8_t buf[64];
    size_t n = 0;
    switch (urb->tweak) {
    case YUSB_TWEAK_SET_CONFIGURATION:
        if (s->value > 1) return -YUSB_EPIPE;
        d->config = (uint8_t)s->value;
        complete(d, urb, 0, 0);
        return 0;
    case YUSB_TWEAK_SET_INTERFACE:
        if (s->value) return -YUSB_EPIPE;
        complete(d, urb, 0, 0);
        return 0;
    case YUSB_TWEAK_CLEAR_HALT:
        if (!d->wedged) {
            if ((s->index & 0x8f) == 0x81) d->halt_in = false;
            if ((s->index & 0x8f) == 0x02) d->halt_out = false;
        }
        complete(d, urb, 0, 0);
        pump_in(d);
        return 0;
    case YUSB_TWEAK_RESET_DEVICE:
        bot_reset(d);
        d->halt_in = d->halt_out = false;
        sense(d, 6, 0x29);                         /* UNIT ATTENTION: reset */
        complete(d, urb, 0, 0);
        return 0;
    default:
        break;
    }
    if (s->request_type == 0x80 && s->request == YUSB_REQ_GET_DESCRIPTOR) {
        unsigned type = s->value >> 8, index = s->value & 0xff;
        if (type == YUSB_DT_DEVICE) { memcpy(buf, device_desc, 18); n = 18; }
        else if (type == YUSB_DT_CONFIG && index == 0) n = config_desc(buf, false);
        else if (type == YUSB_DT_OTHER_SPEED_CONFIG && index == 0) n = config_desc(buf, true);
        else if (type == YUSB_DT_DEVICE_QUALIFIER) { memcpy(buf, qualifier_desc, 10); n = 10; }
        else if (type == YUSB_DT_STRING) n = string_desc(buf, index);
        if (!n) return -YUSB_EPIPE;
    } else if (s->request_type == 0x80 && s->request == YUSB_REQ_GET_STATUS) {
        buf[0] = buf[1] = 0;
        n = 2;
    } else if (s->request_type == 0x80 && s->request == YUSB_REQ_GET_CONFIGURATION) {
        buf[0] = d->config;
        n = 1;
    } else if (s->request_type == 0xa1 && s->request == 0xfe) {   /* Get Max LUN */
        buf[0] = 0;
        n = 1;
    } else if (s->request_type == 0x21 && s->request == 0xff) {   /* BOT reset */
        bot_reset(d);
    } else if (!(s->request_type & 0x80) && (s->request == YUSB_REQ_SET_ADDRESS ||
               s->request == YUSB_REQ_SET_FEATURE || s->request == YUSB_REQ_CLEAR_FEATURE)) {
        /* acknowledged */
    } else {
        return -YUSB_EPIPE;
    }
    if (n > urb->length) n = urb->length;
    if (n) memcpy(urb->data, buf, n);
    complete(d, urb, 0, (s->request_type & 0x80) ? (uint32_t)n : urb->length);
    return 0;
}

static int op_claim(void *ctx, unsigned dev)
{
    simdev_t *d = ctx;
    if (dev || d->claimed) return -YUSB_EBUSY;
    d->claimed = true;
    return 0;
}

static void op_release(void *ctx, unsigned dev)
{
    simdev_t *d = ctx;
    (void)dev;
    d->claimed = false;
    d->in_n = 0;
}

static int op_type(void *ctx, unsigned dev, unsigned address)
{
    simdev_t *d = ctx;
    (void)dev;
    if (!d->config) return -1;
    return address == 0x81 || address == 0x02 ? (int)YUSB_XFER_BULK : -1;
}

static int op_submit(void *ctx, unsigned dev, yusb_xurb_t *urb)
{
    simdev_t *d = ctx;
    (void)dev;
    if (urb->type == YUSB_XFER_CONTROL) return control(d, urb);
    if (urb->dir_in) {
        if (d->in_n == QUEUE) return -YUSB_ENOMEM;
        d->in_q[d->in_n] = urb;
        d->in_fill[d->in_n++] = 0;
        pump_in(d);
        return 0;
    }
    if (d->halt_out) return -YUSB_EPIPE;
    out(d, urb);
    pump_in(d);
    return 0;
}

static void op_cancel(void *ctx, unsigned dev, yusb_xurb_t *urb)
{
    simdev_t *d = ctx;
    (void)dev;
    for (unsigned i = 0; i < d->in_n; ++i) {
        if (d->in_q[i] != urb) continue;
        complete(d, urb, -YUSB_ECONNRESET, d->in_fill[i]);
        memmove(d->in_q + i, d->in_q + i + 1, (d->in_n - i - 1) * sizeof(d->in_q[0]));
        memmove(d->in_fill + i, d->in_fill + i + 1, (d->in_n - i - 1) * sizeof(d->in_fill[0]));
        d->in_n--;
        return;
    }
}

simdev_t *simdev_create(uint8_t *disk, size_t size)
{
    simdev_t *d = calloc(1, sizeof(*d));
    if (!d) return NULL;
    d->in_data = malloc(IN_MAX);
    d->out_data = malloc(IN_MAX);
    if (!d->in_data || !d->out_data) {
        simdev_destroy(d);
        return NULL;
    }
    d->disk = disk;
    d->size = size;
    d->ops = (yusb_hc_ops_t){
        .ctx = d, .claim = op_claim, .release = op_release, .endpoint_type = op_type,
        .submit = op_submit, .cancel = op_cancel,
    };
    return d;
}

void simdev_destroy(simdev_t *d)
{
    if (!d) return;
    free(d->in_data);
    free(d->out_data);
    free(d);
}

const yusb_hc_ops_t *simdev_ops(simdev_t *d) { return &d->ops; }

void simdev_describe(const simdev_t *d, yusb_export_dev_t *dev, const char *busid)
{
    memset(dev, 0, sizeof(*dev));
    snprintf(dev->info.path, sizeof(dev->info.path), "/sys/devices/platform/yoke-sim/usb1/%s", busid);
    snprintf(dev->info.busid, sizeof(dev->info.busid), "%s", busid);
    dev->info.busnum = 1;
    dev->info.devnum = 2;
    dev->info.speed = YUSB_SPEED_HIGH;
    dev->info.vendor = VENDOR;
    dev->info.product = PRODUCT;
    dev->info.bcd_device = 0x0100;
    dev->info.configuration_value = d->config;
    dev->info.num_configurations = 1;
    dev->info.num_interfaces = 1;
    dev->num_ifaces = 1;
    dev->ifaces[0] = (yusb_iface_info_t){0x08, 0x06, 0x50};
}

unsigned simdev_drain(simdev_t *d, yusb_export_t *x, uint64_t now_ms)
{
    unsigned count = 0;
    while (d->done_n) {
        /* Completions may enqueue more (resubmits): take a snapshot. */
        unsigned n = d->done_n;
        done_t batch[DONE_MAX];
        memcpy(batch, d->done, n * sizeof(batch[0]));
        d->done_n = 0;
        for (unsigned i = 0; i < n; ++i) yusb_export_complete(x, batch[i].urb, batch[i].status, batch[i].actual, now_ms);
        count += n;
    }
    return count;
}
