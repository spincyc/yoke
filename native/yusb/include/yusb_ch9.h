#pragma once
/* USB 2.0 chapter 9 vocabulary (reference: src/yoke/usb/ch9.py).
 *
 * Speeds follow the Linux enum usb_device_speed, which USB/IP and Raw
 * Gadget both carry on the wire. Pure data and arithmetic only.
 */
#include "yusb.h"

typedef enum {
    YUSB_SPEED_UNKNOWN = 0,
    YUSB_SPEED_LOW = 1,
    YUSB_SPEED_FULL = 2,
    YUSB_SPEED_HIGH = 3,
    YUSB_SPEED_WIRELESS = 4,
    YUSB_SPEED_SUPER = 5,
    YUSB_SPEED_SUPER_PLUS = 6,
} yusb_speed_t;

/* Linux usb_speed_string() spellings ("high-speed"), as in UDC sysfs
 * current_speed; "UNKNOWN" for anything else. */
const char *yusb_speed_name(unsigned speed);
/* Inverse of yusb_speed_name; surrounding whitespace is ignored. */
yusb_speed_t yusb_speed_from_name(const char *text, size_t len);

/* bmRequestType */
#define YUSB_DIR_IN 0x80u
#define YUSB_TYPE_MASK 0x60u
#define YUSB_TYPE_STANDARD 0x00u
#define YUSB_TYPE_CLASS 0x20u
#define YUSB_TYPE_VENDOR 0x40u
#define YUSB_RECIP_MASK 0x1fu
#define YUSB_RECIP_DEVICE 0x00u
#define YUSB_RECIP_INTERFACE 0x01u
#define YUSB_RECIP_ENDPOINT 0x02u
#define YUSB_RECIP_OTHER 0x03u

/* Standard requests */
#define YUSB_REQ_GET_STATUS 0u
#define YUSB_REQ_CLEAR_FEATURE 1u
#define YUSB_REQ_SET_FEATURE 3u
#define YUSB_REQ_SET_ADDRESS 5u
#define YUSB_REQ_GET_DESCRIPTOR 6u
#define YUSB_REQ_SET_DESCRIPTOR 7u
#define YUSB_REQ_GET_CONFIGURATION 8u
#define YUSB_REQ_SET_CONFIGURATION 9u
#define YUSB_REQ_GET_INTERFACE 10u
#define YUSB_REQ_SET_INTERFACE 11u
#define YUSB_REQ_SYNCH_FRAME 12u

/* Feature selectors and the hub port reset the USB/IP stub interprets */
#define YUSB_FEATURE_ENDPOINT_HALT 0u
#define YUSB_FEATURE_REMOTE_WAKEUP 1u
#define YUSB_PORT_FEAT_RESET 4u
#define YUSB_RT_PORT 0x23u

/* Descriptor types */
#define YUSB_DT_DEVICE 1u
#define YUSB_DT_CONFIG 2u
#define YUSB_DT_STRING 3u
#define YUSB_DT_INTERFACE 4u
#define YUSB_DT_ENDPOINT 5u
#define YUSB_DT_DEVICE_QUALIFIER 6u
#define YUSB_DT_OTHER_SPEED_CONFIG 7u
#define YUSB_DT_INTERFACE_ASSOCIATION 11u
#define YUSB_DT_BOS 15u
#define YUSB_DT_DEVICE_CAPABILITY 16u
#define YUSB_DT_HID 0x21u
#define YUSB_DT_HID_REPORT 0x22u

/* Endpoint transfer types (bmAttributes & 3) */
#define YUSB_XFER_CONTROL 0u
#define YUSB_XFER_ISOC 1u
#define YUSB_XFER_BULK 2u
#define YUSB_XFER_INT 3u

/* Classes */
#define YUSB_CLASS_HID 0x03u
#define YUSB_CLASS_MASS_STORAGE 0x08u
#define YUSB_CLASS_HUB 0x09u
#define YUSB_PROTOCOL_BOT 0x50u

/* An 8-byte SETUP packet (struct usb_ctrlrequest, little endian). */
typedef struct {
    uint8_t request_type;
    uint8_t request;
    uint16_t value;
    uint16_t index;
    uint16_t length;
} yusb_setup_t;

static inline yusb_setup_t yusb_setup_unpack(const uint8_t raw[8])
{
    yusb_setup_t s;
    s.request_type = raw[0];
    s.request = raw[1];
    s.value = (uint16_t)(raw[2] | raw[3] << 8);
    s.index = (uint16_t)(raw[4] | raw[5] << 8);
    s.length = (uint16_t)(raw[6] | raw[7] << 8);
    return s;
}

static inline void yusb_setup_pack(const yusb_setup_t *s, uint8_t raw[8])
{
    raw[0] = s->request_type;
    raw[1] = s->request;
    raw[2] = (uint8_t)s->value;
    raw[3] = (uint8_t)(s->value >> 8);
    raw[4] = (uint8_t)s->index;
    raw[5] = (uint8_t)(s->index >> 8);
    raw[6] = (uint8_t)s->length;
    raw[7] = (uint8_t)(s->length >> 8);
}

static inline yusb_setup_t yusb_setup(uint8_t type, uint8_t request, uint16_t value,
                                      uint16_t index, uint16_t length)
{
    yusb_setup_t s = {type, request, value, index, length};
    return s;
}

static inline bool yusb_setup_is_in(const yusb_setup_t *s) { return s->request_type & YUSB_DIR_IN; }
/* Raw Gadget answers these with EP0_WRITE; everything else with EP0_READ.
 * USB/IP carries every other request as an OUT URB (Linux rule). */
static inline bool yusb_setup_has_in_data(const yusb_setup_t *s)
{
    return (s->request_type & YUSB_DIR_IN) && s->length > 0;
}
static inline unsigned yusb_setup_kind(const yusb_setup_t *s) { return s->request_type & YUSB_TYPE_MASK; }
static inline unsigned yusb_setup_recipient(const yusb_setup_t *s) { return s->request_type & YUSB_RECIP_MASK; }

/* A standard GET_DESCRIPTOR request (ch9.get_descriptor). */
static inline yusb_setup_t yusb_get_descriptor(unsigned type, unsigned index, unsigned length,
                                               unsigned lang, unsigned recip)
{
    return yusb_setup((uint8_t)(YUSB_DIR_IN | recip), YUSB_REQ_GET_DESCRIPTOR,
                      (uint16_t)(type << 8 | (index & 0xffu)), (uint16_t)lang,
                      (uint16_t)length);
}

/* wMaxPacketSize bits 10..0, and bytes per (micro)frame with the
 * high-bandwidth multiplier (bits 12..11). */
static inline unsigned yusb_packet_size(unsigned max_packet) { return max_packet & 0x7ffu; }
static inline unsigned yusb_transfer_size(unsigned max_packet)
{
    return (max_packet & 0x7ffu) * (1u + ((max_packet >> 11) & 3u));
}

/* The positive urb->interval Linux usb_fill_int_urb would use; 0 for bulk
 * and control. The stock stub drops the connection on a non-positive
 * interrupt interval, so this is a wire requirement. */
uint32_t yusb_urb_interval(unsigned speed, unsigned transfer_type, unsigned b_interval);
