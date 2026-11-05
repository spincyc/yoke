#include "box_host.h"
#include <stdio.h>
#include "box_media.h"
#include "usb_desc.h"

typedef struct {
    yoke_usbh_urb_t urb;
    yusb_xurb_t *xurb;
    box_export_t *owner;    /* NULL: free */
    bool local;             /* completed here, never submitted */
} wrapper_t;

static wrapper_t wrappers[YOKE_USBH_URBS];
static void (*after)(box_export_t *e);
static uint64_t (*clock_ms)(void);
static yusb_log_fn log_fn;

void box_host_init(void (*serviced)(box_export_t *e), uint64_t (*now_ms)(void), yusb_log_fn log)
{
    after = serviced;
    clock_ms = now_ms;
    log_fn = log;
}

static void finish(wrapper_t *w, int status, uint32_t actual)
{
    box_export_t *e = w->owner;
    w->owner = NULL;
    if (e->x) yusb_export_complete(e->x, w->xurb, status, actual, clock_ms());
    after(e);
}

static void finish_local(void *arg) { finish(arg, 0, 0); }

void box_host_complete(yoke_usbh_urb_t *urb, int status, uint32_t actual)
{
    finish(urb->owner, status, actual); /* statuses are already wire errnos */
}

static int op_claim(void *ctx, unsigned dev)
{
    (void)ctx;
    return dev == 0 ? yoke_usbh_claim() : -YUSB_ENODEV;
}

static void op_release(void *ctx, unsigned dev)
{
    (void)ctx;
    (void)dev;
    yoke_usbh_release();
}

static int op_endpoint_type(void *ctx, unsigned dev, unsigned address)
{
    (void)ctx;
    (void)dev;
    return yoke_usbh_endpoint_type((uint8_t)address);
}

static int op_submit(void *ctx, unsigned dev, yusb_xurb_t *xurb)
{
    (void)dev;
    wrapper_t *w = NULL;
    for (size_t i = 0; i < YOKE_USBH_URBS && !w; ++i)
        if (!wrappers[i].owner) w = &wrappers[i];
    if (!w) return -YUSB_ENOMEM; /* the exports' max_urbs keeps this unreachable */
    const uint8_t *cfg = yoke_usbh_config_descriptor();
    if (xurb->tweak == YUSB_TWEAK_SET_CONFIGURATION) {
        /* The library configured the device at enumeration; only that
         * configuration can be selected, and needs no bus request. */
        if (!cfg || xurb->setup.value != cfg[5]) return -YUSB_EPIPE;
        *w = (wrapper_t){.xurb = xurb, .owner = ctx, .local = true};
        yoke_usbh_post(finish_local, w); /* never re-enter the core */
        return 0;
    }
    if (xurb->tweak == YUSB_TWEAK_RESET_DEVICE) return -YUSB_EPIPE; /* ⚠ no port reset */
    w->urb = (yoke_usbh_urb_t){
        .endpoint = xurb->type == YUSB_XFER_CONTROL
                        ? (uint8_t)(xurb->setup.request_type & 0x80u)
                        : (uint8_t)(xurb->ep | (xurb->dir_in ? 0x80u : 0)),
        .tweak = xurb->tweak == YUSB_TWEAK_SET_INTERFACE ? YOKE_USBH_SET_INTERFACE
                 : xurb->tweak == YUSB_TWEAK_CLEAR_HALT  ? YOKE_USBH_CLEAR_HALT
                                                         : YOKE_USBH_PLAIN,
        .data = xurb->data,
        .length = xurb->length,
        .zero = xurb->flags & YUSB_URB_ZERO_PACKET,
        .owner = w,
    };
    yusb_setup_pack(&xurb->setup, w->urb.setup);
    w->xurb = xurb;
    w->owner = ctx;
    w->local = false;
    int err = yoke_usbh_submit(&w->urb);
    if (err) w->owner = NULL;
    return err;
}

static void op_cancel(void *ctx, unsigned dev, yusb_xurb_t *xurb)
{
    (void)ctx;
    (void)dev;
    for (size_t i = 0; i < YOKE_USBH_URBS; ++i)
        if (wrappers[i].owner && wrappers[i].xurb == xurb && !wrappers[i].local)
            yoke_usbh_cancel(&wrappers[i].urb);
}

void box_host_bind(box_export_t *e)
{
    e->ops = (yusb_hc_ops_t){
        .ctx = e, .claim = op_claim, .release = op_release, .endpoint_type = op_endpoint_type,
        .submit = op_submit, .cancel = op_cancel, .log = log_fn,
        .cancel_flushes_endpoint = true, /* yoke_usbh.h: cancel flushes the endpoint */
    };
}

bool box_host_busy(const box_export_t *e)
{
    for (size_t i = 0; i < YOKE_USBH_URBS; ++i)
        if (wrappers[i].owner == e) return true;
    return false;
}

void box_host_describe(yusb_export_dev_t *device)
{
    const uint8_t *dev = yoke_usbh_device_descriptor(), *cfg = yoke_usbh_config_descriptor();
    yusb_device_info_t *info = &device->info;
    *info = (yusb_device_info_t){
        .busnum = 1, .devnum = yoke_usbh_address(), .speed = yoke_usbh_speed(),
        .vendor = usb_le16(dev + 8), .product = usb_le16(dev + 10), .bcd_device = usb_le16(dev + 12),
        .device_class = dev[4], .device_subclass = dev[5], .device_protocol = dev[6],
        .configuration_value = cfg[5], .num_configurations = dev[17], .num_interfaces = cfg[4],
    };
    snprintf(info->path, sizeof(info->path), "/sys/devices/platform/yoke/usb1/%s", BOX_BUSID);
    snprintf(info->busid, sizeof(info->busid), "%s", BOX_BUSID);
    device->num_ifaces = 0;
    usb_walk_t w;
    usb_walk_init(&w, cfg);
    for (const uint8_t *p; (p = usb_walk_next(&w)) && device->num_ifaces < YUSB_EXPORT_MAX_IFACES;)
        if (p[1] == USB_DT_INTERFACE && p[0] >= 9 && p[3] == 0)
            device->ifaces[device->num_ifaces++] = (yusb_iface_info_t){p[5], p[6], p[7]};
}
