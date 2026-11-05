/* Raw Gadget ABI helpers (see rawgadget.h). */
#define _GNU_SOURCE
#include <errno.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "rawgadget.h"

int rg_pack_init(uint8_t out[RG_INIT_SIZE], const char *driver, const char *device, unsigned speed)
{
    size_t a = strlen(driver), b = strlen(device);
    if (!a || a >= RG_NAME_MAX || !b || b >= RG_NAME_MAX || speed > 0xff) return -EINVAL;
    memset(out, 0, RG_INIT_SIZE);
    memcpy(out, driver, a);
    memcpy(out + RG_NAME_MAX, device, b);
    out[2 * RG_NAME_MAX] = (uint8_t)speed;
    return 0;
}

void rg_pack_io(uint8_t *data, unsigned ep, size_t length, bool zero)
{
    uint8_t *io = data - YUSB_HEADROOM;
    uint16_t e = (uint16_t)ep, flags = zero ? RG_IO_FLAGS_ZERO : 0;
    uint32_t len = (uint32_t)length;
    memcpy(io, &e, 2);          /* native endianness, as the kernel reads it */
    memcpy(io + 2, &flags, 2);
    memcpy(io + 4, &len, 4);
}

size_t rg_unpack_eps(const uint8_t *raw, size_t count, yusb_udc_ep_t *out, size_t max)
{
    if (count > RG_EPS_MAX) count = RG_EPS_MAX;
    if (count > max) count = max;
    for (size_t i = 0; i < count; ++i) {
        const uint8_t *e = raw + i * RG_EP_INFO_SIZE;
        uint32_t addr, caps;
        uint16_t limit;
        memcpy(&addr, e + 16, 4);
        memcpy(&caps, e + 20, 4);  /* usb_raw_ep_caps bitfields from bit 0 */
        memcpy(&limit, e + 24, 2);
        out[i].number = addr == RG_EP_ADDR_ANY ? YUSB_UDC_EP_ANY : (uint8_t)addr;
        out[i].control = caps & 1u;
        out[i].iso = caps & 2u;
        out[i].bulk = caps & 4u;
        out[i].interrupt = caps & 8u;
        out[i].dir_in = caps & 16u;
        out[i].dir_out = caps & 32u;
        out[i].maxpacket_limit = limit;
    }
    return count;
}

int rg_result(long rc)
{
    if (rc >= 0) return (int)rc;
    switch (-rc) {
    case EINTR: return YUSB_GADGET_INTERRUPTED;
    case EPIPE: return YUSB_GADGET_HALT_CLEARED;
    case ESHUTDOWN:
    case ECONNRESET:
    case ENODEV: return YUSB_GADGET_SHUTDOWN;
    default: return YUSB_GADGET_ERROR;
    }
}

const char *rg_gadget_name_for(const char *platform_driver, const char *udc)
{
    static const char *const map[][2] = {
        {"dwc3", "dwc3-gadget"}, {"dummy_udc", "dummy_udc"}, {"musb-hdrc", "musb-hdrc"},
    };
    for (size_t i = 0; platform_driver && i < sizeof(map) / sizeof(map[0]); ++i)
        if (!strcmp(platform_driver, map[i][0])) return map[i][1];
    return udc;
}

void rg_gadget_name(const char *sysfs, const char *udc, char *out, size_t cap)
{
    char link[PATH_MAX], target[PATH_MAX];
    const char *driver = NULL;
    snprintf(link, sizeof(link), "%s/%s/device/driver", sysfs, udc);
    if (realpath(link, target)) {
        const char *slash = strrchr(target, '/');
        driver = slash ? slash + 1 : target;
    }
    snprintf(out, cap, "%s", rg_gadget_name_for(driver, udc));
}
