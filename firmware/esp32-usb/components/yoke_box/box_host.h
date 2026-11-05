#pragma once
/* The export core (native/yusb yusb_export) on the USB Host Library backend:
 * the core's host-controller operations onto yoke_usbh (with the Linux
 * stub's configuration and reset rules), completions back into the core,
 * and the exported device's USB/IP description. Pure C over yoke_usbh.h
 * (host-tested); everything runs in the USB host task. */
#include "yoke_usbh.h"
#include "yusb_export.h"

typedef struct box_export {
    yusb_export_t *x;          /* NULL: free */
    yusb_hc_ops_t ops;         /* ctx: this export */
    int fd;                    /* the platform's transport; -1: closed */
} box_export_t;

/* serviced runs after a completion was fed to an export's core; log may
 * be NULL. */
void box_host_init(void (*serviced)(box_export_t *e), uint64_t (*now_ms)(void),
                   yusb_log_fn log);
/* Fill e->ops (ctx = e) for yusb_export_create. */
void box_host_bind(box_export_t *e);
/* A URB of e is still with the backend (destroy e only when false). */
bool box_host_busy(const box_export_t *e);
/* yoke_usbh's completion callback. */
void box_host_complete(yoke_usbh_urb_t *urb, int status, uint32_t actual);
/* The attached device as USB/IP describes it (busid BOX_BUSID). */
void box_host_describe(yusb_export_dev_t *device);
