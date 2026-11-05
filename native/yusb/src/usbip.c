/* USB/IP 1.1.1 codec (reference: usbip.py). Big endian throughout. */
#include <string.h>
#include "yusb_usbip.h"

static uint32_t get32(const uint8_t *p)
{
    return (uint32_t)p[0] << 24 | (uint32_t)p[1] << 16 | (uint32_t)p[2] << 8 | p[3];
}

static uint16_t get16(const uint8_t *p) { return (uint16_t)(p[0] << 8 | p[1]); }

static void put32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v >> 24);
    p[1] = (uint8_t)(v >> 16);
    p[2] = (uint8_t)(v >> 8);
    p[3] = (uint8_t)v;
}

static void put16(uint8_t *p, uint16_t v)
{
    p[0] = (uint8_t)(v >> 8);
    p[1] = (uint8_t)v;
}

/* Lengths travel as signed 32-bit ints; negative or oversized is invalid. */
static bool length_ok(uint32_t raw) { return (int32_t)raw >= 0 && raw <= YUSB_MAX_TRANSFER; }

int yusb_usbip_decode(const uint8_t raw[YUSB_USBIP_HEADER_SIZE], yusb_usbip_hdr_t *out)
{
    memset(out, 0, sizeof(*out));
    out->command = get32(raw);
    out->seqnum = get32(raw + 4);
    out->devid = get32(raw + 8);
    out->direction = get32(raw + 12);
    out->ep = get32(raw + 16);
    switch (out->command) {
    case YUSB_CMD_SUBMIT:
        if (out->direction > YUSB_USBIP_DIR_IN || out->ep > 15) return YUSB_ERR_PROTOCOL;
        out->flags = get32(raw + 20);
        out->length = get32(raw + 24);
        if (!length_ok(out->length)) return YUSB_ERR_PROTOCOL;
        out->start_frame = (int32_t)get32(raw + 28);
        out->number_of_packets = get32(raw + 32);
        out->interval = (int32_t)get32(raw + 36);
        memcpy(out->setup, raw + 40, 8);
        return YUSB_OK;
    case YUSB_RET_SUBMIT:
        out->status = (int32_t)get32(raw + 20);
        out->actual_length = get32(raw + 24);
        if (!length_ok(out->actual_length)) return YUSB_ERR_PROTOCOL;
        out->start_frame = (int32_t)get32(raw + 28);
        out->number_of_packets = get32(raw + 32);
        out->error_count = (int32_t)get32(raw + 36);
        return YUSB_OK;
    case YUSB_CMD_UNLINK:
        out->unlink_seqnum = get32(raw + 20);
        return YUSB_OK;
    case YUSB_RET_UNLINK:
        out->status = (int32_t)get32(raw + 20);
        return YUSB_OK;
    default:
        return YUSB_ERR_PROTOCOL;
    }
}

void yusb_usbip_encode(const yusb_usbip_hdr_t *h, uint8_t raw[YUSB_USBIP_HEADER_SIZE])
{
    memset(raw, 0, YUSB_USBIP_HEADER_SIZE);
    put32(raw, h->command);
    put32(raw + 4, h->seqnum);
    put32(raw + 8, h->devid);
    put32(raw + 12, h->direction);
    put32(raw + 16, h->ep);
    switch (h->command) {
    case YUSB_CMD_SUBMIT:
        put32(raw + 20, h->flags);
        put32(raw + 24, h->length);
        put32(raw + 28, (uint32_t)h->start_frame);
        put32(raw + 32, h->number_of_packets);
        put32(raw + 36, (uint32_t)h->interval);
        memcpy(raw + 40, h->setup, 8);
        break;
    case YUSB_RET_SUBMIT:
        put32(raw + 20, (uint32_t)h->status);
        put32(raw + 24, h->actual_length);
        put32(raw + 28, (uint32_t)h->start_frame);
        put32(raw + 32, h->number_of_packets);
        put32(raw + 36, (uint32_t)h->error_count);
        break;
    case YUSB_CMD_UNLINK:
        put32(raw + 20, h->unlink_seqnum);
        break;
    case YUSB_RET_UNLINK:
        put32(raw + 20, (uint32_t)h->status);
        break;
    default:
        break;
    }
}

yusb_usbip_hdr_t yusb_cmd_submit(uint32_t seqnum, uint32_t devid, unsigned direction,
                                 unsigned ep, uint32_t flags, uint32_t length,
                                 int32_t interval, const uint8_t setup[8])
{
    yusb_usbip_hdr_t h;
    memset(&h, 0, sizeof(h));
    h.command = YUSB_CMD_SUBMIT;
    h.seqnum = seqnum;
    h.devid = devid;
    h.direction = direction;
    h.ep = ep;
    h.flags = flags;
    h.length = length;
    h.number_of_packets = YUSB_NON_ISO_PACKETS;
    h.interval = interval;
    if (setup) memcpy(h.setup, setup, 8);
    return h;
}

yusb_usbip_hdr_t yusb_ret_submit(uint32_t seqnum, int32_t status, uint32_t actual)
{
    yusb_usbip_hdr_t h;
    memset(&h, 0, sizeof(h));
    h.command = YUSB_RET_SUBMIT;
    h.seqnum = seqnum;
    h.status = status;
    h.actual_length = actual;
    h.number_of_packets = YUSB_NON_ISO_PACKETS;
    return h;
}

yusb_usbip_hdr_t yusb_cmd_unlink(uint32_t seqnum, uint32_t devid, uint32_t victim)
{
    yusb_usbip_hdr_t h;
    memset(&h, 0, sizeof(h));
    h.command = YUSB_CMD_UNLINK;
    h.seqnum = seqnum;
    h.devid = devid;
    h.unlink_seqnum = victim;
    return h;
}

yusb_usbip_hdr_t yusb_ret_unlink(uint32_t seqnum, int32_t status)
{
    yusb_usbip_hdr_t h;
    memset(&h, 0, sizeof(h));
    h.command = YUSB_RET_UNLINK;
    h.seqnum = seqnum;
    h.status = status;
    return h;
}

/* A NUL-terminated ASCII string inside a fixed field (usbip._cstr). */
static int cstr(const uint8_t *field, size_t size, char *out)
{
    const uint8_t *nul = memchr(field, 0, size);
    if (!nul) return YUSB_ERR_PROTOCOL;
    for (const uint8_t *p = field; p < nul; ++p)
        if (*p & 0x80) return YUSB_ERR_PROTOCOL;
    memcpy(out, field, (size_t)(nul - field) + 1);
    return YUSB_OK;
}

static int fixed(const char *text, uint8_t *field, size_t size)
{
    size_t len = 0;
    while (len < size && text[len]) len++;
    if (len >= size) return YUSB_ERR_RANGE;
    for (size_t i = 0; i < len; ++i)
        if ((uint8_t)text[i] & 0x80) return YUSB_ERR_RANGE;
    memset(field, 0, size);
    memcpy(field, text, len);
    return YUSB_OK;
}

int yusb_device_info_decode(const uint8_t raw[YUSB_DEVICE_INFO_SIZE], yusb_device_info_t *out)
{
    memset(out, 0, sizeof(*out));
    if (cstr(raw, YUSB_PATH_SIZE, out->path) || cstr(raw + 256, YUSB_BUSID_SIZE, out->busid))
        return YUSB_ERR_PROTOCOL;
    const uint8_t *p = raw + 288;
    out->busnum = get32(p);
    out->devnum = get32(p + 4);
    out->speed = get32(p + 8);
    out->vendor = get16(p + 12);
    out->product = get16(p + 14);
    out->bcd_device = get16(p + 16);
    out->device_class = p[18];
    out->device_subclass = p[19];
    out->device_protocol = p[20];
    out->configuration_value = p[21];
    out->num_configurations = p[22];
    out->num_interfaces = p[23];
    return YUSB_OK;
}

int yusb_device_info_encode(const yusb_device_info_t *info, uint8_t raw[YUSB_DEVICE_INFO_SIZE])
{
    if (fixed(info->path, raw, YUSB_PATH_SIZE) || fixed(info->busid, raw + 256, YUSB_BUSID_SIZE))
        return YUSB_ERR_RANGE;
    uint8_t *p = raw + 288;
    put32(p, info->busnum);
    put32(p + 4, info->devnum);
    put32(p + 8, info->speed);
    put16(p + 12, info->vendor);
    put16(p + 14, info->product);
    put16(p + 16, info->bcd_device);
    p[18] = info->device_class;
    p[19] = info->device_subclass;
    p[20] = info->device_protocol;
    p[21] = info->configuration_value;
    p[22] = info->num_configurations;
    p[23] = info->num_interfaces;
    return YUSB_OK;
}

void yusb_op_encode(uint16_t code, uint32_t status, uint8_t raw[YUSB_OP_HEADER_SIZE])
{
    put16(raw, YUSB_USBIP_VERSION);
    put16(raw + 2, code);
    put32(raw + 4, status);
}

int yusb_op_decode(const uint8_t raw[YUSB_OP_HEADER_SIZE], uint16_t *code, uint32_t *status)
{
    if (get16(raw) != YUSB_USBIP_VERSION) return YUSB_ERR_PROTOCOL;
    *code = get16(raw + 2);
    *status = get32(raw + 4);
    return YUSB_OK;
}

int yusb_req_import_encode(const char *busid, uint8_t raw[YUSB_REQ_IMPORT_SIZE])
{
    yusb_op_encode(YUSB_OP_REQ_IMPORT, 0, raw);
    return fixed(busid, raw + YUSB_OP_HEADER_SIZE, YUSB_BUSID_SIZE);
}

int yusb_busid_decode(const uint8_t field[YUSB_BUSID_SIZE], char busid[YUSB_BUSID_SIZE])
{
    return cstr(field, YUSB_BUSID_SIZE, busid);
}

size_t yusb_devlist_head_encode(uint32_t count, uint8_t raw[YUSB_OP_HEADER_SIZE + 4])
{
    yusb_op_encode(YUSB_OP_REP_DEVLIST, 0, raw);
    put32(raw + YUSB_OP_HEADER_SIZE, count);
    return YUSB_OP_HEADER_SIZE + 4;
}

int yusb_iface_info_encode(const yusb_iface_info_t *iface, uint8_t raw[YUSB_INTERFACE_INFO_SIZE])
{
    raw[0] = iface->cls;
    raw[1] = iface->subclass;
    raw[2] = iface->protocol;
    raw[3] = 0;
    return YUSB_OK;
}
