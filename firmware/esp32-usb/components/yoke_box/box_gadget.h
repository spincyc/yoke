#pragma once
/* The presentation core (native/yusb yusb_proxy) on the TinyUSB backend: the
 * core's gadget operations onto yoke_usbd, yoke_usbd's events and
 * completions into the core, and the presentation of the core's descriptor
 * cache. Pure C over yoke_usbd.h (host-tested); one session at a time, in
 * the USB task. */
#include "yoke_usbd.h"
#include "yusb_proxy.h"

/* service runs after every input fed to the core; buffers is the DMA
 * layout the core allocates with; log may be NULL. */
void box_gadget_init(const yusb_buffers_t *buffers, void (*service)(void),
                     uint64_t (*now_ms)(void), yusb_log_fn log);
const yusb_gadget_ops_t *box_gadget_ops(void);
/* The session reached YUSB_PHASE_READY: present its cache and start the
 * gadget with this controller's endpoints, so the core refuses what it
 * cannot carry. false (and why): yoke_usbd cannot present it. */
bool box_gadget_present(yusb_proxy_t *proxy, char *why, size_t cap);
/* Disconnect after the session stopped. */
void box_gadget_withdraw(void);
