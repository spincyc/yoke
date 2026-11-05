#include "usb_desc.h"
#include <stdio.h>

void usb_walk_init(usb_walk_t *w, const uint8_t *cfg)
{
    *w = (usb_walk_t){.cfg = cfg, .total = usb_le16(cfg + 2), .offset = cfg[0]};
    if (w->offset > w->total) w->offset = w->total;
}

const uint8_t *usb_walk_next(usb_walk_t *w)
{
    if (w->offset + 2u > w->total) return NULL;
    const uint8_t *p = w->cfg + w->offset;
    if (p[0] < 2 || p[0] > w->total - w->offset) return NULL;
    w->offset = (uint16_t)(w->offset + p[0]);
    if (p[1] == USB_DT_INTERFACE && p[0] >= 9) {
        w->interface = p[2];
        w->alternate = p[3];
        w->in_interface = true;
    }
    return p;
}

bool usb_walk_complete(const usb_walk_t *w) { return w->offset == w->total; }

static const char *class_name(uint8_t cls)
{
    switch (cls) {
    case 0x00: return "per-interface";
    case 0x01: return "audio";
    case 0x02: return "communications";
    case 0x03: return "HID";
    case 0x07: return "printer";
    case 0x08: return "mass storage";
    case 0x09: return "hub";
    case 0x0a: return "CDC data";
    case 0x0e: return "video";
    case 0xe0: return "wireless";
    case 0xef: return "miscellaneous";
    case 0xfe: return "application";
    case 0xff: return "vendor";
    default: return "other";
    }
}

void usb_desc_device(const uint8_t *dev, char *out, size_t cap)
{
    snprintf(out, cap, "USB %x.%02x %04x:%04x class %02x/%02x/%02x (%s) ep0 %u, %u configuration%s",
             dev[3], dev[2], usb_le16(dev + 8), usb_le16(dev + 10), dev[4], dev[5], dev[6],
             class_name(dev[4]), dev[7], dev[17], dev[17] == 1 ? "" : "s");
}

bool usb_desc_line(const uint8_t *p, char *out, size_t cap)
{
    static const char *const types[] = {"control", "isochronous", "bulk", "interrupt"};
    if (p[1] == USB_DT_CONFIG && p[0] >= 9) {
        snprintf(out, cap, "configuration %u: %u interface%s, %s powered, %u mA", p[5], p[4],
                 p[4] == 1 ? "" : "s", p[7] & 0x40u ? "self" : "bus", 2u * p[8]);
    } else if (p[1] == USB_DT_IAD && p[0] >= 8) {
        snprintf(out, cap, " function: interfaces %u-%u class %02x/%02x/%02x (%s)", p[2],
                 p[2] + p[3] - 1, p[4], p[5], p[6], class_name(p[4]));
    } else if (p[1] == USB_DT_INTERFACE && p[0] >= 9) {
        snprintf(out, cap, " interface %u alt %u: class %02x/%02x/%02x (%s), %u endpoint%s", p[2],
                 p[3], p[5], p[6], p[7], class_name(p[5]), p[4], p[4] == 1 ? "" : "s");
    } else if (p[1] == USB_DT_ENDPOINT && p[0] >= 7) {
        unsigned packet = usb_le16(p + 4);
        snprintf(out, cap, "  endpoint 0x%02x %s %s, %u bytes%s, interval %u", p[2],
                 types[p[3] & 3u], p[2] & 0x80u ? "IN" : "OUT", packet & 0x7ffu,
                 packet >> 11 ? " (high bandwidth)" : "", p[6]);
    } else {
        return false;
    }
    return true;
}

void usb_desc_string(const uint8_t *desc, char *out, size_t cap)
{
    if (!cap) return;
    size_t n = 0;
    for (size_t i = 2; i + 1 < desc[0] && n + 1 < cap; i += 2) {
        unsigned unit = usb_le16(desc + i);
        out[n++] = unit >= 0x20 && unit < 0x7f ? (char)unit : '?';
    }
    out[n] = 0;
}
