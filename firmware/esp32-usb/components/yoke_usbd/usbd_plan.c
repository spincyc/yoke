#include "usbd_plan.h"
#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#define CLASS_HUB 9u
#define EP0_SIZE 64u

static int refuse(char *why, size_t cap, const char *format, ...)
{
    va_list args;
    va_start(args, format);
    if (cap) vsnprintf(why, cap, format, args);
    va_end(args);
    return -1;
}

static unsigned words(unsigned bytes) { return (bytes + 3u) / 4u; }

size_t usbd_plan_fifos(const uint8_t *cfg, usbd_fifo_t *out, size_t max)
{
    usb_walk_t w;
    usb_walk_init(&w, cfg);
    size_t count = 0;
    for (const uint8_t *p; (p = usb_walk_next(&w));) {
        if (p[1] != USB_DT_ENDPOINT || p[0] < 7) continue;
        uint16_t packet = usb_le16(p + 4) & 0x7ffu;
        size_t i = 0;
        while (i < count && out[i].address != p[2]) ++i;
        if (i == count) {
            if (count == max) return count;
            out[count++] = (usbd_fifo_t){.address = p[2], .max_packet = packet};
        } else if (packet > out[i].max_packet) {
            out[i].max_packet = packet;
        }
    }
    return count;
}

unsigned usbd_plan_fifo_words(const uint8_t *cfg, const usbd_limits_t *lim)
{
    usbd_fifo_t fifos[32];
    size_t count = usbd_plan_fifos(cfg, fifos, sizeof(fifos) / sizeof(fifos[0]));
    unsigned largest_out = EP0_SIZE, tx = words(EP0_SIZE);
    for (size_t i = 0; i < count; ++i) {
        unsigned bytes = 4u * words(fifos[i].max_packet);
        if (fifos[i].address & 0x80u) tx += bytes / 4u;
        else if (bytes > largest_out) largest_out = bytes;
    }
    /* dcd_dwc2.c calc_device_grxfsiz: SETUP packets, global NAK, two
     * largest OUT packets with status words, two words per endpoint. */
    unsigned rx = 13u + 1u + 2u * (largest_out / 4u + 1u) + 2u * lim->ep_count;
    return rx + tx;
}

static int check_endpoint(const uint8_t *p, const usbd_limits_t *lim, char *why, size_t cap)
{
    unsigned address = p[2], type = p[3] & 3u, packet = usb_le16(p + 4) & 0x7ffu;
    unsigned number = address & 0x0fu;
    if (p[0] < 7 || address & 0x70u || number == 0)
        return refuse(why, cap, "endpoint 0x%02x is malformed", address);
    if (number >= lim->ep_count)
        return refuse(why, cap, "endpoint 0x%02x exceeds this controller's endpoint numbers 1..%u",
                      address, lim->ep_count - 1u);
    /* dcd_dwc2 gives IN endpoint n TX FIFO n. */
    if (address & 0x80u && number >= lim->ep_in_count)
        return refuse(why, cap, "endpoint 0x%02x exceeds this controller's IN endpoint numbers 1..%u",
                      address, lim->ep_in_count - 1u);
    if (type != USB_XFER_BULK && type != USB_XFER_INT)
        return refuse(why, cap, "endpoint 0x%02x: %s endpoints are unsupported", address,
                      type == 1u ? "isochronous" : "extra control");
    if (usb_le16(p + 4) >> 11)
        return refuse(why, cap, "endpoint 0x%02x: high-bandwidth packets are unsupported", address);
    bool ok = lim->high_speed
                  ? (type == USB_XFER_BULK ? packet == 512u : packet >= 1u && packet <= 1024u)
                  : (type == USB_XFER_BULK ? (packet == 8u || packet == 16u || packet == 32u ||
                                          packet == 64u)
                                       : packet >= 1u && packet <= 64u);
    if (!ok)
        return refuse(why, cap, "endpoint 0x%02x: wMaxPacketSize %u is invalid at %s speed",
                      address, packet, lim->high_speed ? "high" : "full");
    return 0;
}

static int check_config(const uint8_t *cfg, unsigned index, const usbd_limits_t *lim,
                        char *why, size_t cap)
{
    if (!cfg || cfg[0] < 9 || cfg[1] != USB_DT_CONFIG || usb_le16(cfg + 2) < cfg[0])
        return refuse(why, cap, "configuration %u is malformed", index);
    /* usbd.c opens configuration value N from descriptor index N - 1. */
    if (cfg[5] != index + 1u)
        return refuse(why, cap, "configuration %u has bConfigurationValue %u; TinyUSB needs %u",
                      index, cfg[5], index + 1u);
    usb_walk_t w;
    usb_walk_init(&w, cfg);
    bool in_interface = false;
    for (const uint8_t *p; (p = usb_walk_next(&w));) {
        if (p[1] == USB_DT_IAD) {
            if (p[0] < 8 || p[2] + p[3] > lim->max_interfaces)
                return refuse(why, cap, "interface association at %u is invalid", p[2]);
        } else if (p[1] == USB_DT_INTERFACE) {
            if (p[0] < 9 || p[2] >= lim->max_interfaces)
                return refuse(why, cap, "interface %u exceeds TinyUSB's %u interfaces", p[2],
                              lim->max_interfaces);
            if (p[5] == CLASS_HUB) return refuse(why, cap, "hub interfaces are unsupported");
            in_interface = true;
        } else if (!in_interface) {
            /* usbd.c expects an interface right after the header (or IAD). */
            return refuse(why, cap, "configuration %u: descriptor type %u before the first interface",
                          index, p[1]);
        } else if (p[1] == USB_DT_ENDPOINT && check_endpoint(p, lim, why, cap)) {
            return -1;
        }
    }
    if (!usb_walk_complete(&w)) return refuse(why, cap, "configuration %u: bad bLength", index);
    unsigned need = usbd_plan_fifo_words(cfg, lim);
    if (need > lim->fifo_words)
        return refuse(why, cap, "configuration %u needs %u FIFO words; this controller has %u",
                      index, need, lim->fifo_words);
    return 0;
}

int usbd_plan_check(const yoke_usbd_descriptors_t *d, const usbd_limits_t *lim,
                    char *why, size_t cap)
{
    if (d->speed != (lim->high_speed ? YOKE_USBD_SPEED_HIGH : YOKE_USBD_SPEED_FULL))
        return refuse(why, cap, "this port presents %s-speed devices only",
                      lim->high_speed ? "high" : "full");
    const uint8_t *dev = d->device;
    if (!dev || dev[0] != 18 || dev[1] != USB_DT_DEVICE)
        return refuse(why, cap, "device descriptor is malformed");
    if (dev[7] != EP0_SIZE) return refuse(why, cap, "bMaxPacketSize0 must be presented as 64");
    if (!d->num_configs || d->num_configs > YOKE_USBD_MAX_CONFIGS || dev[17] != d->num_configs)
        return refuse(why, cap, "bNumConfigurations %u does not match %u configurations", dev[17],
                      d->num_configs);
    for (unsigned i = 0; i < d->num_configs; ++i)
        if (check_config(d->configs[i], i, lim, why, cap)) return -1;
    return 0;
}

const uint8_t *usbd_plan_endpoint(const uint8_t *cfg, uint8_t interface, uint8_t alternate,
                                  uint8_t address)
{
    usb_walk_t w;
    usb_walk_init(&w, cfg);
    bool seen_interface = false;
    for (const uint8_t *p; (p = usb_walk_next(&w));) {
        if (p[1] == USB_DT_INTERFACE) seen_interface = true;
        if (seen_interface && p[1] == USB_DT_ENDPOINT && p[0] >= 7 && p[2] == address &&
            w.interface == interface && w.alternate == alternate)
            return p;
    }
    return NULL;
}

const uint8_t *usbd_plan_string(const yoke_usbd_descriptors_t *d, uint8_t index, uint16_t lang)
{
    for (size_t i = 0; i < d->num_strings; ++i)
        if (d->strings[i].index == index && (index == 0 || d->strings[i].lang == lang))
            return d->strings[i].data;
    return NULL;
}

int usbd_plan_config_of(const yoke_usbd_descriptors_t *d, const void *p)
{
    const uint8_t *q = p;
    for (unsigned i = 0; i < d->num_configs; ++i) {
        const uint8_t *cfg = d->configs[i];
        if (q >= cfg && q < cfg + usb_le16(cfg + 2)) return (int)i;
    }
    return -1;
}

void usbd_plan_other_speed(const yoke_usbd_descriptors_t *d, uint8_t qualifier[10],
                           uint8_t *other, size_t cap)
{
    const uint8_t *dev = d->device;
    const uint8_t own[10] = {10, USB_DT_QUALIFIER, dev[2], dev[3], dev[4], dev[5], dev[6], dev[7],
                             0, 0};
    memcpy(qualifier, d->qualifier && d->qualifier[0] >= 10 ? d->qualifier : own, 10);
    size_t len = d->other_speed ? usb_le16(d->other_speed + 2) : 0;
    if (len >= 9 && len <= cap) {
        memcpy(other, d->other_speed, len);
        return;
    }
    const uint8_t empty[9] = {9, USB_DT_OTHER_SPEED, 9, 0, 0, 1, 0, 0x80, 50};
    memcpy(other, empty, sizeof(empty));
    qualifier[8] = 0; /* no other-speed configuration to offer */
}
