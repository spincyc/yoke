#pragma once
/* Linux Raw Gadget ABI (⚠ bench-unverified on the RK3566; reference:
 * src/yoke/usb/rawgadget.py). Request numbers and layouts follow
 * include/uapi/linux/usb/raw_gadget.h (asm-generic _IOC encoding, as on
 * arm64 and x86); tests/native_usb checks them against that header when
 * the host has it. Every hardware assumption is isolated here:
 *  - usb_raw_init.driver_name is the controller's gadget name
 *    (dwc3-gadget), not the UDC device name; a wrong name makes RUN fail
 *    with EBUSY (rg_gadget_name infers it from sysfs);
 *  - blocked EVENT_FETCH, EP0 and EP transfer calls return EINTR on a signal, and are
 *    never retried, so a SIGUSR1 aimed at the thread cancels them;
 *  - dwc3 gives started requests back with -EPIPE when the host clears the
 *    halt (YUSB_GADGET_HALT_CLEARED);
 *  - halting or disabling an endpoint fails while a request is queued.
 */
#include <stdint.h>
#include "yusb_proxy.h"

#define RG_DEVICE "/dev/raw-gadget"
#define RG_NAME_MAX 128u
#define RG_EPS_MAX 30u
#define RG_EP_NAME_MAX 16u
#define RG_EP_ADDR_ANY 0xffu
#define RG_IO_FLAGS_ZERO 0x0001u
#define RG_INIT_SIZE (2u * RG_NAME_MAX + 1u)
#define RG_EP_INFO_SIZE 32u

#define RG_IOC(dir, nr, size) \
    ((unsigned long)(dir) << 30 | (unsigned long)(size) << 16 | (unsigned long)'U' << 8 | (nr))
#define RG_IOC_NONE 0u
#define RG_IOC_WRITE 1u
#define RG_IOC_READ 2u

#define RG_IOCTL_INIT RG_IOC(RG_IOC_WRITE, 0, RG_INIT_SIZE)
#define RG_IOCTL_RUN RG_IOC(RG_IOC_NONE, 1, 0)
#define RG_IOCTL_EVENT_FETCH RG_IOC(RG_IOC_READ, 2, 8)
#define RG_IOCTL_EP0_WRITE RG_IOC(RG_IOC_WRITE, 3, 8)
#define RG_IOCTL_EP0_READ RG_IOC(RG_IOC_READ | RG_IOC_WRITE, 4, 8)
#define RG_IOCTL_EP_ENABLE RG_IOC(RG_IOC_WRITE, 5, 9)
#define RG_IOCTL_EP_DISABLE RG_IOC(RG_IOC_WRITE, 6, 4)
#define RG_IOCTL_EP_WRITE RG_IOC(RG_IOC_WRITE, 7, 8)
#define RG_IOCTL_EP_READ RG_IOC(RG_IOC_READ | RG_IOC_WRITE, 8, 8)
#define RG_IOCTL_CONFIGURE RG_IOC(RG_IOC_NONE, 9, 0)
#define RG_IOCTL_VBUS_DRAW RG_IOC(RG_IOC_WRITE, 10, 4)
#define RG_IOCTL_EPS_INFO RG_IOC(RG_IOC_READ, 11, RG_EPS_MAX * RG_EP_INFO_SIZE)
#define RG_IOCTL_EP0_STALL RG_IOC(RG_IOC_NONE, 12, 0)
#define RG_IOCTL_EP_SET_HALT RG_IOC(RG_IOC_WRITE, 13, 4)

/* struct usb_raw_init: two NUL-terminated names and the speed. 0 or -EINVAL. */
int rg_pack_init(uint8_t out[RG_INIT_SIZE], const char *driver, const char *device, unsigned speed);
/* struct usb_raw_ep_io written into the YUSB_HEADROOM bytes before data. */
void rg_pack_io(uint8_t *data, unsigned ep, size_t length, bool zero);
/* Parse EPS_INFO; returns the count used (<= max). */
size_t rg_unpack_eps(const uint8_t *raw, size_t count, yusb_udc_ep_t *out, size_t max);
/* -errno of a failed call to a yusb_gadget_result_t. */
int rg_result(long rc);

/* UDC platform driver -> usb_gadget.name Raw Gadget compares against.
 * ⚠ Only dummy_udc is checked against a running kernel; dwc3's name is
 * from its source. Unknown drivers use the UDC device name. */
const char *rg_gadget_name_for(const char *platform_driver, const char *udc);
/* Infer from <sysfs>/<udc>/device/driver (a symlink); writes into out. */
void rg_gadget_name(const char *sysfs, const char *udc, char *out, size_t cap);
