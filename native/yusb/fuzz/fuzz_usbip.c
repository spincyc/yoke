/* libFuzzer: USB/IP codec (headers, device records, op headers, busid and
 * the ADR 0028 offer/accept fields). Decode -> encode -> decode must agree. */
#include <stdlib.h>
#include <string.h>
#include "yusb_usbip.h"
#include "yusb_yx.h"

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size);

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)
{
    if (size >= YUSB_USBIP_HEADER_SIZE) {
        yusb_usbip_hdr_t a, b;
        if (!yusb_usbip_decode(data, &a)) {
            uint8_t raw[YUSB_USBIP_HEADER_SIZE];
            yusb_usbip_encode(&a, raw);
            if (yusb_usbip_decode(raw, &b) || memcmp(&a, &b, sizeof(a))) abort();
        }
    }
    if (size >= YUSB_DEVICE_INFO_SIZE) {
        yusb_device_info_t a, b;
        if (!yusb_device_info_decode(data, &a)) {
            uint8_t raw[YUSB_DEVICE_INFO_SIZE];
            if (yusb_device_info_encode(&a, raw) || yusb_device_info_decode(raw, &b) ||
                memcmp(&a, &b, sizeof(a)))
                abort();
        }
        yusb_yx_params_t p;
        if (yusb_yx_accept_get(data, &p) && p.levers & ~YUSB_YX_LEVERS) abort();
    }
    if (size >= YUSB_OP_HEADER_SIZE) {
        uint16_t code;
        uint32_t status;
        yusb_op_decode(data, &code, &status);
    }
    if (size >= YUSB_BUSID_SIZE) {
        char busid[YUSB_BUSID_SIZE];
        if (!yusb_busid_decode(data, busid) && strlen(busid) >= YUSB_BUSID_SIZE) abort();
        yusb_yx_params_t p;
        if (yusb_yx_offer_get(data, &p) && p.levers & ~YUSB_YX_LEVERS) abort();
    }
    return 0;
}
