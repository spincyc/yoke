/* Descriptor parsing, checks, rewrites and the presentation builder
 * (reference: descriptors.py). Messages keep the reference's wording. */
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include "yusb_desc.h"

static uint16_t le16(const uint8_t *p) { return (uint16_t)(p[0] | p[1] << 8); }

static int fail(int err, char *why, size_t cap, const char *fmt, ...)
{
    if (why && cap) {
        va_list args;
        va_start(args, fmt);
        vsnprintf(why, cap, fmt, args);
        va_end(args);
    }
    return err;
}

int yusb_parse_device(const uint8_t *raw, size_t len, yusb_device_desc_t *out, char *why,
                      size_t cap)
{
    if (len < 18 || raw[0] < 18 || raw[1] != YUSB_DT_DEVICE)
        return fail(YUSB_ERR_DESCRIPTOR, why, cap, "device descriptor is malformed");
    if (raw[17] < 1 || raw[17] > YUSB_MAX_CONFIGS)
        return fail(YUSB_ERR_DESCRIPTOR, why, cap, "bNumConfigurations %u out of range", raw[17]);
    unsigned mp0 = raw[7];
    if (mp0 != 8 && mp0 != 16 && mp0 != 32 && mp0 != 64 && mp0 != 9)
        return fail(YUSB_ERR_DESCRIPTOR, why, cap, "bMaxPacketSize0 %u invalid", mp0);
    out->bcd_usb = le16(raw + 2);
    out->cls = raw[4];
    out->subclass = raw[5];
    out->protocol = raw[6];
    out->max_packet0 = raw[7];
    out->vendor = le16(raw + 8);
    out->product = le16(raw + 10);
    out->bcd_device = le16(raw + 12);
    out->i_manufacturer = raw[14];
    out->i_product = raw[15];
    out->i_serial = raw[16];
    out->num_configurations = raw[17];
    return YUSB_OK;
}

/* descriptors.iter_descriptors' checks, with its messages. */
static int check_tlv(const uint8_t *raw, size_t len, char *why, size_t cap)
{
    size_t offset = 0;
    while (offset < len) {
        if (len - offset < 2)
            return fail(YUSB_ERR_DESCRIPTOR, why, cap, "truncated descriptor header");
        size_t n = raw[offset];
        if (n < 2 || n > len - offset)
            return fail(YUSB_ERR_DESCRIPTOR, why, cap, "descriptor bLength %zu at %zu invalid",
                        n, offset);
        offset += n;
    }
    return YUSB_OK;
}

int yusb_desc_check_tlv(const uint8_t *raw, size_t len) { return check_tlv(raw, len, NULL, 0); }

/* Close the alternate setting opened by the last interface descriptor. */
static int finish_alt(yusb_config_t *c, bool open, char *why, size_t cap)
{
    if (!open) return YUSB_OK;
    yusb_alt_t *a = &c->alts[c->num_alts - 1];
    unsigned found = c->num_endpoints - a->first_endpoint;
    if (found != a->num_endpoints)
        return fail(YUSB_ERR_DESCRIPTOR, why, cap,
                    "interface %u alt %u: %u endpoints, bNumEndpoints %u", a->number,
                    a->alternate, found, a->num_endpoints);
    for (unsigned i = 0; i + 1 < c->num_alts; ++i)
        if (c->alts[i].number == a->number && c->alts[i].alternate == a->alternate)
            return fail(YUSB_ERR_DESCRIPTOR, why, cap, "duplicate interface %u alt %u",
                        a->number, a->alternate);
    return YUSB_OK;
}

int yusb_parse_config(const uint8_t *raw, size_t len, yusb_config_t *out, char *why, size_t cap)
{
    if (len < 9 || raw[0] < 9 ||
        (raw[1] != YUSB_DT_CONFIG && raw[1] != YUSB_DT_OTHER_SPEED_CONFIG))
        return fail(YUSB_ERR_DESCRIPTOR, why, cap, "configuration descriptor is malformed");
    unsigned total = le16(raw + 2);
    if (total != len)
        return fail(YUSB_ERR_DESCRIPTOR, why, cap, "wTotalLength %u != %zu bytes received",
                    total, len);
    memset(out, 0, sizeof(*out));
    out->value = raw[5];
    out->string_index = raw[6];
    out->attributes = raw[7];
    out->max_power = raw[8];
    bool open = false;
    size_t base = raw[0], offset = base;
    while (offset < len) {
        if (len - offset < 2)
            return fail(YUSB_ERR_DESCRIPTOR, why, cap, "truncated descriptor header");
        size_t n = raw[offset];
        if (n < 2 || n > len - offset)
            return fail(YUSB_ERR_DESCRIPTOR, why, cap, "descriptor bLength %zu at %zu invalid",
                        n, offset - base);
        const uint8_t *body = raw + offset;
        unsigned type = body[1];
        offset += n;
        if (type == YUSB_DT_INTERFACE) {
            if (n < 9) return fail(YUSB_ERR_DESCRIPTOR, why, cap, "short interface descriptor");
            int rc = finish_alt(out, open, why, cap);
            if (rc) return rc;
            if (out->num_alts >= YUSB_MAX_ALTS)
                return fail(YUSB_ERR_NOMEM, why, cap, "descriptors exceed %u interfaces",
                            (unsigned)YUSB_MAX_ALTS);
            yusb_alt_t *a = &out->alts[out->num_alts++];
            a->number = body[2];
            a->alternate = body[3];
            a->num_endpoints = body[4];
            a->cls = body[5];
            a->subclass = body[6];
            a->protocol = body[7];
            a->string_index = body[8];
            a->first_endpoint = out->num_endpoints;
            open = true;
        } else if (type == YUSB_DT_ENDPOINT) {
            if (!open || n < 7)
                return fail(YUSB_ERR_DESCRIPTOR, why, cap,
                            "endpoint descriptor outside an interface");
            unsigned address = body[2];
            if (address & 0x70 || !(address & 0x0f))
                return fail(YUSB_ERR_DESCRIPTOR, why, cap, "endpoint address 0x%02x invalid",
                            address);
            unsigned mp = le16(body + 4);
            if (!yusb_packet_size(mp))
                return fail(YUSB_ERR_DESCRIPTOR, why, cap, "endpoint 0x%02x has wMaxPacketSize 0",
                            address);
            yusb_alt_t *a = &out->alts[out->num_alts - 1];
            for (unsigned i = a->first_endpoint; i < out->num_endpoints; ++i)
                if (out->endpoints[i].address == address)
                    return fail(YUSB_ERR_DESCRIPTOR, why, cap, "duplicate endpoint 0x%02x",
                                address);
            /* Alternate settings of one interface may reuse an address; two
             * interfaces of one configuration may not (descriptors.py). */
            for (unsigned i = 0; i + 1 < out->num_alts; ++i) {
                const yusb_alt_t *o = &out->alts[i];
                if (o->number == a->number) continue;
                for (unsigned j = 0; j < o->num_endpoints; ++j)
                    if (out->endpoints[o->first_endpoint + j].address == address)
                        return fail(YUSB_ERR_DESCRIPTOR, why, cap,
                                    "duplicate endpoint 0x%02x in interfaces %u and %u", address,
                                    o->number, a->number);
            }
            if (out->num_endpoints >= YUSB_MAX_CONFIG_EPS)
                return fail(YUSB_ERR_NOMEM, why, cap, "descriptors exceed %u endpoints",
                            (unsigned)YUSB_MAX_CONFIG_EPS);
            yusb_endpoint_t *e = &out->endpoints[out->num_endpoints++];
            e->address = (uint8_t)address;
            e->attributes = body[3];
            e->max_packet = (uint16_t)mp;
            e->interval = body[6];
            e->raw_len = (uint8_t)(n < sizeof(e->raw) ? n : sizeof(e->raw));
            memcpy(e->raw, body, e->raw_len);
        } else if (type == YUSB_DT_HID && open && n >= 9) {
            yusb_alt_t *a = &out->alts[out->num_alts - 1];
            for (size_t start = 6; start < 6 + 3u * body[5]; start += 3) {
                if (start + 3 > n)
                    return fail(YUSB_ERR_DESCRIPTOR, why, cap, "truncated HID descriptor");
                /* More report descriptors than the cache holds are forwarded. */
                if (body[start] == YUSB_DT_HID_REPORT && a->num_hid_reports < YUSB_MAX_HID_REPORTS)
                    a->hid_report_length[a->num_hid_reports++] = le16(body + start + 1);
            }
        }
    }
    int rc = finish_alt(out, open, why, cap);
    if (rc) return rc;
    unsigned distinct = 0;
    for (unsigned i = 0; i < out->num_alts; ++i) {
        bool seen = false;
        for (unsigned j = 0; j < i && !seen; ++j) seen = out->alts[j].number == out->alts[i].number;
        distinct += !seen;
    }
    if (distinct != raw[4])
        return fail(YUSB_ERR_DESCRIPTOR, why, cap, "bNumInterfaces %u but %u interfaces found",
                    raw[4], distinct);
    out->num_interfaces = (uint8_t)distinct;
    return YUSB_OK;
}

const yusb_alt_t *yusb_config_alt(const yusb_config_t *c, unsigned number, unsigned alternate)
{
    for (unsigned i = 0; i < c->num_alts; ++i)
        if (c->alts[i].number == number && c->alts[i].alternate == alternate) return &c->alts[i];
    return NULL;
}

bool yusb_config_has_interface(const yusb_config_t *c, unsigned number)
{
    for (unsigned i = 0; i < c->num_alts; ++i)
        if (c->alts[i].number == number) return true;
    return false;
}

/* Alternate settings in the reference's dict order: interfaces in order of
 * first appearance, each one's alternates in descriptor order. Request
 * sequences and the first reported problem therefore match exactly. */
static unsigned dict_order(const yusb_config_t *c, uint16_t order[YUSB_MAX_ALTS])
{
    unsigned count = 0;
    for (unsigned i = 0; i < c->num_alts; ++i) {
        bool first = true;
        for (unsigned j = 0; j < i && first; ++j) first = c->alts[j].number != c->alts[i].number;
        if (!first) continue;
        for (unsigned k = i; k < c->num_alts; ++k)
            if (c->alts[k].number == c->alts[i].number) order[count++] = (uint16_t)k;
    }
    return count;
}

void yusb_rewrite_device(uint8_t *raw, size_t len, unsigned ep0_size)
{
    if (len > 7) raw[7] = (uint8_t)ep0_size;
}

int yusb_rewrite_config(uint8_t *raw, size_t len, unsigned power_ma)
{
    if (power_ma > 500 || len < 9) return YUSB_ERR_RANGE;
    raw[7] = (uint8_t)((raw[7] | 0x80u) & ~0x60u);
    raw[8] = (uint8_t)((power_ma + 1) / 2);
    return YUSB_OK;
}

int yusb_rewrite_bos(uint8_t *raw, size_t len)
{
    size_t offset = len ? raw[0] : 0;
    if (offset > len) return YUSB_OK;
    if (yusb_desc_check_tlv(raw + offset, len - offset)) return YUSB_ERR_DESCRIPTOR;
    while (offset < len) {
        const uint8_t *body = raw + offset;
        if (body[1] == YUSB_DT_DEVICE_CAPABILITY && body[0] >= 7 && body[2] == 0x02)
            raw[offset + 3] &= (uint8_t)~0x02u;   /* USB 2.0 Extension: LPM */
        offset += body[0];
    }
    return YUSB_OK;
}

bool yusb_udc_ep_supports(const yusb_udc_ep_t *ep, unsigned address, unsigned type,
                          unsigned packet_size)
{
    bool number = ep->number == YUSB_UDC_EP_ANY || ep->number == (address & 0x0fu);
    bool dir = (address & 0x80u) ? ep->dir_in : ep->dir_out;
    bool kind = type == YUSB_XFER_BULK ? ep->bulk : type == YUSB_XFER_INT ? ep->interrupt
              : type == YUSB_XFER_ISOC ? ep->iso : false;
    return number && dir && kind && packet_size <= ep->maxpacket_limit;
}

int yusb_check_speed(unsigned speed, char *why, size_t cap)
{
    if (speed == YUSB_SPEED_SUPER || speed == YUSB_SPEED_SUPER_PLUS)
        return fail(YUSB_ERR_UNSUPPORTED, why, cap,
                    "super-speed devices are unsupported (USB 2 first); attach the device "
                    "through a USB 2 cable or hub so it enumerates at high speed");
    if (speed != YUSB_SPEED_LOW && speed != YUSB_SPEED_FULL && speed != YUSB_SPEED_HIGH)
        return fail(YUSB_ERR_UNSUPPORTED, why, cap, "device speed %u is unsupported", speed);
    return YUSB_OK;
}

yusb_speed_t yusb_presented_speed(unsigned remote)
{
    if (yusb_check_speed(remote, NULL, 0)) return YUSB_SPEED_UNKNOWN;
    return remote == YUSB_SPEED_HIGH ? YUSB_SPEED_HIGH : YUSB_SPEED_FULL;
}

int yusb_check_supported(const yusb_device_desc_t *device, const yusb_config_t *configs,
                         size_t count, unsigned speed, const yusb_budget_t *budget,
                         char *why, size_t cap)
{
    int rc = yusb_check_speed(speed, why, cap);
    if (rc) return rc;
    if (device->cls == YUSB_CLASS_HUB)
        return fail(YUSB_ERR_UNSUPPORTED, why, cap,
                    "hubs are unsupported; export the device behind the hub");
    for (size_t ci = 0; ci < count; ++ci) {
        const yusb_config_t *c = &configs[ci];
        uint16_t order[YUSB_MAX_ALTS];
        unsigned n = dict_order(c, order);
        unsigned worst_in = 0, worst_out = 0, ins = 0, outs = 0;
        for (unsigned k = 0; k < n; ++k) {
            const yusb_alt_t *a = &c->alts[order[k]];
            if (k == 0 || c->alts[order[k - 1]].number != a->number) {
                worst_in += ins;
                worst_out += outs;
                ins = outs = 0;
            }
            if (a->cls == YUSB_CLASS_HUB)
                return fail(YUSB_ERR_UNSUPPORTED, why, cap, "hub interfaces are unsupported");
            unsigned alt_in = 0;
            for (unsigned i = 0; i < a->num_endpoints; ++i)
                alt_in += yusb_ep_is_in(yusb_alt_endpoint(c, a, i));
            if (alt_in > ins) ins = alt_in;
            if (a->num_endpoints - alt_in > outs) outs = a->num_endpoints - alt_in;
            for (unsigned i = 0; i < a->num_endpoints; ++i) {
                const yusb_endpoint_t *e = yusb_alt_endpoint(c, a, i);
                unsigned type = yusb_ep_type(e);
                if (type == YUSB_XFER_ISOC)
                    return fail(YUSB_ERR_UNSUPPORTED, why, cap,
                                "isochronous endpoint 0x%02x (interface %u) is unsupported",
                                e->address, a->number);
                if (type == YUSB_XFER_CONTROL)
                    return fail(YUSB_ERR_UNSUPPORTED, why, cap,
                                "extra control endpoint 0x%02x is unsupported", e->address);
                if (speed == YUSB_SPEED_LOW && type == YUSB_XFER_BULK)
                    return fail(YUSB_ERR_UNSUPPORTED, why, cap,
                                "low-speed bulk endpoints are invalid");
                if (yusb_ep_number(e) > budget->max_number)
                    return fail(YUSB_ERR_UNSUPPORTED, why, cap,
                                "endpoint 0x%02x exceeds the UDC endpoint number limit %u",
                                e->address, budget->max_number);
            }
        }
        worst_in += ins;
        worst_out += outs;
        if (worst_in > budget->max_in || worst_out > budget->max_out)
            return fail(YUSB_ERR_UNSUPPORTED, why, cap,
                        "configuration %u needs %u IN and %u OUT endpoints; UDC budget is "
                        "%u IN and %u OUT", c->value, worst_in, worst_out, budget->max_in,
                        budget->max_out);
    }
    return YUSB_OK;
}

/* --- presentation ------------------------------------------------------ */

const yusb_config_t *yusb_presentation_config(const yusb_presentation_t *p, unsigned value)
{
    for (unsigned i = 0; i < p->num_configs; ++i)
        if (p->configs[i].value == value) return &p->configs[i];
    return NULL;
}

static int answer(const yusb_presentation_t *p, yusb_blob_t b, const uint8_t **data, size_t *len)
{
    *data = yusb_blob(p, b);
    *len = b.present ? b.len : 0;
    return 1;
}

int yusb_presentation_lookup(const yusb_presentation_t *p, const yusb_setup_t *s,
                             const uint8_t **data, size_t *len)
{
    unsigned type = s->value >> 8, index = s->value & 0xffu;
    static const yusb_blob_t stall = {0, 0, 0};
    switch (type) {
    case YUSB_DT_DEVICE: return answer(p, p->device_raw, data, len);
    case YUSB_DT_CONFIG: return answer(p, index < p->num_configs ? p->config_raw[index] : stall, data, len);
    case YUSB_DT_DEVICE_QUALIFIER: return answer(p, p->qualifier, data, len);
    case YUSB_DT_OTHER_SPEED_CONFIG: return answer(p, index == 0 ? p->other_speed : stall, data, len);
    case YUSB_DT_BOS: return answer(p, p->bos, data, len);
    case YUSB_DT_STRING:
        for (unsigned i = 0; i < p->num_strings; ++i)
            if (p->strings[i].index == index && p->strings[i].lang == s->index)
                return answer(p, p->strings[i].data, data, len);
        return 0;
    default:
        return 0;
    }
}

const uint8_t *yusb_presentation_class(const yusb_presentation_t *p, unsigned interface,
                                       unsigned type, unsigned index, size_t *len)
{
    for (unsigned i = 0; i < p->num_class; ++i) {
        if (p->class_descs[i].interface == interface && p->class_descs[i].type == type &&
            p->class_descs[i].index == index) {
            *len = p->class_descs[i].data.len;
            return yusb_blob(p, p->class_descs[i].data);
        }
    }
    return NULL;
}

size_t yusb_missing_endpoints(const yusb_presentation_t *p, const yusb_udc_ep_t *eps,
                              size_t count, char *out, size_t cap)
{
    bool missing[256] = {false};
    size_t found = 0;
    for (unsigned ci = 0; ci < p->num_configs; ++ci) {
        const yusb_config_t *c = &p->configs[ci];
        for (unsigned i = 0; i < c->num_endpoints; ++i) {
            const yusb_endpoint_t *e = &c->endpoints[i];
            bool ok = false;
            for (size_t u = 0; u < count && !ok; ++u)
                ok = yusb_udc_ep_supports(&eps[u], e->address, yusb_ep_type(e),
                                          yusb_packet_size(e->max_packet));
            if (!ok && !missing[e->address]) { missing[e->address] = true; found++; }
        }
    }
    size_t used = 0;
    if (cap) out[0] = 0;
    for (unsigned a = 0; a < 256; ++a) {
        if (!missing[a] || used >= cap) continue;
        int n = snprintf(out + used, cap - used, "%s0x%02x", used ? ", " : "", a);
        if (n > 0) used += (size_t)n < cap - used ? (size_t)n : cap - used - 1;
    }
    return found;
}

/* --- builder ----------------------------------------------------------- */

enum {
    B_DEVICE, B_CONFIG_HEAD, B_CONFIG_FULL, B_QUALIFIER, B_OTHER_HEAD, B_OTHER_FULL,
    B_BOS_HEAD, B_BOS_FULL, B_LANGS, B_STRING, B_OS, B_HID, B_DONE, B_FAILED,
};

static int arena_put(yusb_builder_t *b, const uint8_t *data, size_t len, yusb_blob_t *blob)
{
    yusb_presentation_t *p = b->p;
    if (len > YUSB_MAX_DESCRIPTOR || len > sizeof(p->arena) - p->arena_used)
        return fail(YUSB_ERR_NOMEM, b->why, sizeof(b->why),
                    "descriptors exceed the presentation cache (%u bytes)",
                    (unsigned)sizeof(p->arena));
    memcpy(p->arena + p->arena_used, data, len);
    blob->offset = p->arena_used;
    blob->len = (uint16_t)len;
    blob->present = 1;
    p->arena_used += (uint32_t)len;
    return YUSB_OK;
}

static int failed(yusb_builder_t *b, int err)
{
    b->error = err;
    b->step = B_FAILED;
    return err;
}

void yusb_builder_init(yusb_builder_t *b, yusb_presentation_t *p, unsigned remote_speed,
                       unsigned power_ma, unsigned ep0_size, const yusb_budget_t *budget)
{
    memset(b, 0, sizeof(*b));
    memset(p, 0, sizeof(*p));
    b->p = p;
    b->power_ma = power_ma;
    b->ep0_size = ep0_size;
    b->budget = *budget;
    p->remote_speed = (yusb_speed_t)remote_speed;
    p->speed = yusb_presented_speed(remote_speed);
    b->step = B_DEVICE;
    b->max_request = 0xffff;
    int rc = yusb_check_speed(remote_speed, b->why, sizeof(b->why));
    if (rc) failed(b, rc);
}

static bool wanted(const yusb_builder_t *b, unsigned index)
{
    return b->wanted[index >> 3] & (1u << (index & 7));
}

/* Advance cursor to the next wanted string index >= from; false if none. */
static bool next_wanted(yusb_builder_t *b, unsigned from)
{
    for (unsigned i = from; i < 256; ++i)
        if (wanted(b, i)) { b->cursor = i; return true; }
    return false;
}

/* After the strings: the Microsoft OS string probe, when requested. */
static int after_strings(const yusb_builder_t *b) { return b->prefetch_os ? B_OS : B_HID; }

static void begin_strings(yusb_builder_t *b)
{
    const yusb_presentation_t *p = b->p;
    memset(b->wanted, 0, sizeof(b->wanted));
    unsigned want[3] = {p->device.i_manufacturer, p->device.i_product, p->device.i_serial};
    for (unsigned i = 0; i < 3; ++i) b->wanted[want[i] >> 3] |= (uint8_t)(1u << (want[i] & 7));
    for (unsigned ci = 0; ci < p->num_configs; ++ci) {
        const yusb_config_t *c = &p->configs[ci];
        b->wanted[c->string_index >> 3] |= (uint8_t)(1u << (c->string_index & 7));
        for (unsigned a = 0; a < c->num_alts; ++a)
            b->wanted[c->alts[a].string_index >> 3] |= (uint8_t)(1u << (c->alts[a].string_index & 7));
    }
    b->wanted[0] &= (uint8_t)~1u;
    b->step = next_wanted(b, 1) ? B_LANGS : after_strings(b);
    b->config = b->alt = b->report = 0;
}

static void after_qualifier(yusb_builder_t *b)
{
    if (b->p->device.bcd_usb >= 0x0201) b->step = B_BOS_HEAD;
    else begin_strings(b);
}

/* The next HID report descriptor to fetch, in reference order; false when
 * none remain. b->config/alt (dict-order position)/report form the cursor. */
static bool next_hid(yusb_builder_t *b, yusb_setup_t *req)
{
    const yusb_presentation_t *p = b->p;
    for (; b->config < p->num_configs; b->config++, b->alt = 0, b->report = 0) {
        const yusb_config_t *c = &p->configs[b->config];
        uint16_t order[YUSB_MAX_ALTS];
        unsigned n = dict_order(c, order);
        for (; b->alt < n; b->alt++, b->report = 0) {
            const yusb_alt_t *a = &c->alts[order[b->alt]];
            for (; b->report < a->num_hid_reports; b->report++) {
                size_t len;
                unsigned length = a->hid_report_length[b->report];
                /* Longer than the control buffer: left to forwarding (a STALL). */
                if (!length || length > b->max_request ||
                    yusb_presentation_class(p, a->number, YUSB_DT_HID_REPORT, b->report, &len))
                    continue;
                *req = yusb_get_descriptor(YUSB_DT_HID_REPORT, b->report, length, a->number,
                                           YUSB_RECIP_INTERFACE);
                return true;
            }
        }
    }
    return false;
}

int yusb_builder_next(yusb_builder_t *b, yusb_setup_t *req)
{
    if (b->step == B_FAILED) return b->error;
    switch (b->step) {
    case B_DEVICE: *req = yusb_get_descriptor(YUSB_DT_DEVICE, 0, 18, 0, 0); break;
    case B_CONFIG_HEAD: *req = yusb_get_descriptor(YUSB_DT_CONFIG, b->config, 9, 0, 0); break;
    case B_CONFIG_FULL: *req = yusb_get_descriptor(YUSB_DT_CONFIG, b->config, b->pending.length, 0, 0); break;
    case B_QUALIFIER: *req = yusb_get_descriptor(YUSB_DT_DEVICE_QUALIFIER, 0, 10, 0, 0); break;
    case B_OTHER_HEAD: *req = yusb_get_descriptor(YUSB_DT_OTHER_SPEED_CONFIG, 0, 9, 0, 0); break;
    case B_OTHER_FULL: *req = yusb_get_descriptor(YUSB_DT_OTHER_SPEED_CONFIG, 0, b->pending.length, 0, 0); break;
    case B_BOS_HEAD: *req = yusb_get_descriptor(YUSB_DT_BOS, 0, 5, 0, 0); break;
    case B_BOS_FULL: *req = yusb_get_descriptor(YUSB_DT_BOS, 0, b->pending.length, 0, 0); break;
    case B_LANGS: *req = yusb_get_descriptor(YUSB_DT_STRING, 0, 255, 0, 0); break;
    case B_STRING:
        *req = yusb_get_descriptor(YUSB_DT_STRING, b->cursor, 255, b->langs[b->lang], 0);
        break;
    case B_OS: *req = yusb_get_descriptor(YUSB_DT_STRING, 0xee, 255, 0, 0); break;
    case B_HID:
        if (next_hid(b, req)) break;
        b->step = B_DONE;
        return 0;
    case B_DONE:
        return 0;
    default:
        return failed(b, YUSB_ERR_STATE);
    }
    if (req->length > b->max_request)
        return failed(b, fail(YUSB_ERR_DESCRIPTOR, b->why, sizeof(b->why),
                              "descriptor of %u bytes exceeds the %u-byte control buffer",
                              req->length, b->max_request));
    b->pending = *req;
    return 1;
}

/* Header of a variable-length descriptor: the total to fetch next. */
static int head(yusb_builder_t *b, unsigned type, const uint8_t *data, size_t len)
{
    if (len < 4)
        return failed(b, fail(YUSB_ERR_DESCRIPTOR, b->why, sizeof(b->why),
                              "descriptor type %u header is truncated", type));
    unsigned total = le16(data + 2);
    if (total < len)
        return failed(b, fail(YUSB_ERR_DESCRIPTOR, b->why, sizeof(b->why),
                              "descriptor type %u wTotalLength %u", type, total));
    b->pending.length = (uint16_t)total;
    return YUSB_OK;
}

static int short_read(yusb_builder_t *b, unsigned type)
{
    return failed(b, fail(YUSB_ERR_DESCRIPTOR, b->why, sizeof(b->why),
                          "descriptor type %u returned a short read", type));
}

static int store_string(yusb_builder_t *b, unsigned index, unsigned lang, const uint8_t *data,
                        size_t len, bool stalled)
{
    yusb_presentation_t *p = b->p;
    if (p->num_strings >= YUSB_MAX_STRINGS) return YUSB_ERR_NOMEM;
    p->strings[p->num_strings].index = (uint8_t)index;
    p->strings[p->num_strings].lang = (uint16_t)lang;
    memset(&p->strings[p->num_strings].data, 0, sizeof(yusb_blob_t));
    if (!stalled) {
        int rc = arena_put(b, data, len, &p->strings[p->num_strings].data);
        if (rc) return rc;
    }
    p->num_strings++;
    return YUSB_OK;
}

int yusb_builder_feed(yusb_builder_t *b, const uint8_t *data, size_t len, bool stalled)
{
    yusb_presentation_t *p = b->p;
    int rc;
    switch (b->step) {
    case B_DEVICE:
        if (stalled)
            return failed(b, fail(YUSB_ERR_DESCRIPTOR, b->why, sizeof(b->why),
                                  "device stalled GET_DESCRIPTOR(DEVICE)"));
        rc = yusb_parse_device(data, len, &p->device, b->why, sizeof(b->why));
        if (rc) return failed(b, rc);
        if ((rc = arena_put(b, data, 18, &p->device_raw))) return failed(b, rc);
        yusb_rewrite_device(p->arena + p->device_raw.offset, 18, b->ep0_size);
        b->config = 0;
        b->step = B_CONFIG_HEAD;
        return YUSB_OK;
    case B_CONFIG_HEAD:
        if (stalled)
            return failed(b, fail(YUSB_ERR_DESCRIPTOR, b->why, sizeof(b->why),
                                  "configuration %u stalled", b->config));
        if ((rc = head(b, YUSB_DT_CONFIG, data, len))) return rc;
        b->step = B_CONFIG_FULL;
        return YUSB_OK;
    case B_CONFIG_FULL: {
        if (stalled || len != b->pending.length) return short_read(b, YUSB_DT_CONFIG);
        rc = yusb_parse_config(data, len, &p->configs[b->config], b->why, sizeof(b->why));
        if (rc) return failed(b, rc);
        yusb_blob_t *blob = &p->config_raw[b->config];
        if ((rc = arena_put(b, data, len, blob))) return failed(b, rc);
        yusb_rewrite_config(p->arena + blob->offset, len, b->power_ma);
        p->num_configs = (uint8_t)++b->config;
        if (b->config < p->device.num_configurations) {
            b->step = B_CONFIG_HEAD;
            return YUSB_OK;
        }
        rc = yusb_check_supported(&p->device, p->configs, p->num_configs, p->remote_speed,
                                  &b->budget, b->why, sizeof(b->why));
        if (rc) return failed(b, rc);
        for (unsigned ci = 0; ci < p->num_configs; ++ci)
            for (unsigned a = 0; a < p->configs[ci].num_alts; ++a)
                if (p->configs[ci].alts[a].number >= 32)
                    return failed(b, fail(YUSB_ERR_NOMEM, b->why, sizeof(b->why),
                                          "interface number %u exceeds 31",
                                          p->configs[ci].alts[a].number));
        if (p->device.bcd_usb >= 0x0200) b->step = B_QUALIFIER;
        else after_qualifier(b);
        return YUSB_OK;
    }
    case B_QUALIFIER:
        if (stalled) { after_qualifier(b); return YUSB_OK; }
        if (len < 10 || data[1] != YUSB_DT_DEVICE_QUALIFIER)
            return failed(b, fail(YUSB_ERR_DESCRIPTOR, b->why, sizeof(b->why),
                                  "device qualifier is malformed"));
        if ((rc = arena_put(b, data, 10, &p->qualifier))) return failed(b, rc);
        yusb_rewrite_device(p->arena + p->qualifier.offset, 10, b->ep0_size);
        b->step = B_OTHER_HEAD;
        return YUSB_OK;
    case B_OTHER_HEAD:
        if (stalled) { after_qualifier(b); return YUSB_OK; }
        if ((rc = head(b, YUSB_DT_OTHER_SPEED_CONFIG, data, len))) return rc;
        b->step = B_OTHER_FULL;
        return YUSB_OK;
    case B_OTHER_FULL:
        if (stalled || len != b->pending.length) return short_read(b, YUSB_DT_OTHER_SPEED_CONFIG);
        rc = yusb_parse_config(data, len, &b->scratch, b->why, sizeof(b->why));
        if (rc) return failed(b, rc);
        if ((rc = arena_put(b, data, len, &p->other_speed))) return failed(b, rc);
        yusb_rewrite_config(p->arena + p->other_speed.offset, len, b->power_ma);
        after_qualifier(b);
        return YUSB_OK;
    case B_BOS_HEAD:
        if (stalled) { begin_strings(b); return YUSB_OK; }
        if ((rc = head(b, YUSB_DT_BOS, data, len))) return rc;
        b->step = B_BOS_FULL;
        return YUSB_OK;
    case B_BOS_FULL:
        if (stalled || len != b->pending.length) return short_read(b, YUSB_DT_BOS);
        if (data[0] <= len && (rc = check_tlv(data + data[0], len - data[0], b->why, sizeof(b->why))))
            return failed(b, rc);
        if ((rc = arena_put(b, data, len, &p->bos))) return failed(b, rc);
        yusb_rewrite_bos(p->arena + p->bos.offset, len);
        begin_strings(b);
        return YUSB_OK;
    case B_LANGS: {
        if (stalled) { b->step = after_strings(b); return YUSB_OK; }
        /* Strings beyond the cache bounds are forwarded instead (degrade). */
        if (store_string(b, 0, 0, data, len, false)) { b->step = after_strings(b); return YUSB_OK; }
        b->num_langs = 0;
        size_t end = len ? (data[0] < len ? data[0] : len) : 0;
        for (size_t i = 2; i + 1 < end && b->num_langs < YUSB_MAX_STRING_LANGS; i += 2)
            b->langs[b->num_langs++] = le16(data + i);
        b->lang = 0;
        b->step = b->num_langs && next_wanted(b, 1) ? B_STRING : after_strings(b);
        return YUSB_OK;
    }
    case B_STRING:
        if (store_string(b, b->cursor, b->langs[b->lang], data, len, stalled)) {
            b->step = after_strings(b);   /* cache full: the rest is forwarded */
            return YUSB_OK;
        }
        if (!next_wanted(b, b->cursor + 1)) {
            if (++b->lang >= b->num_langs) { b->step = after_strings(b); return YUSB_OK; }
            next_wanted(b, 1);
        }
        return YUSB_OK;
    case B_OS:
        store_string(b, 0xee, 0, data, len, stalled);   /* best effort */
        b->step = B_HID;
        return YUSB_OK;
    case B_HID: {
        b->report++;
        if (stalled) return YUSB_OK;
        yusb_blob_t blob;
        if (p->num_class >= YUSB_MAX_CLASS_DESCS || arena_put(b, data, len, &blob)) {
            b->step = B_DONE;       /* cache full: the rest is forwarded */
            return YUSB_OK;
        }
        unsigned iface = b->pending.index;
        p->class_descs[p->num_class].interface = (uint8_t)iface;
        p->class_descs[p->num_class].type = YUSB_DT_HID_REPORT;
        p->class_descs[p->num_class].index = (uint8_t)(b->pending.value & 0xff);
        p->class_descs[p->num_class].data = blob;
        p->num_class++;
        return YUSB_OK;
    }
    default:
        return failed(b, YUSB_ERR_STATE);
    }
}
