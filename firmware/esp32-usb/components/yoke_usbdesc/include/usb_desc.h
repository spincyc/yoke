#pragma once
/* USB descriptor helpers shared by the USB backends and the self-tests:
 * a bounds-checked configuration walk and one-line descriptions for the
 * serial log. Pure C, run on the host by tests/firmware_usb. */
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define USB_DT_DEVICE 1u
#define USB_DT_CONFIG 2u
#define USB_DT_STRING 3u
#define USB_DT_INTERFACE 4u
#define USB_DT_ENDPOINT 5u
#define USB_DT_QUALIFIER 6u
#define USB_DT_OTHER_SPEED 7u
#define USB_DT_IAD 11u
#define USB_XFER_CONTROL 0u
#define USB_XFER_ISOC 1u
#define USB_XFER_BULK 2u
#define USB_XFER_INT 3u

static inline uint16_t usb_le16(const uint8_t *p) { return (uint16_t)(p[0] | p[1] << 8); }

typedef struct {
    const uint8_t *cfg;
    uint16_t total;
    uint16_t offset;
    uint8_t interface, alternate;  /* of the latest interface descriptor */
    bool in_interface;             /* an interface descriptor was seen */
} usb_walk_t;

void usb_walk_init(usb_walk_t *w, const uint8_t *cfg);
/* The next descriptor after the configuration header; NULL at the end or
 * on a bLength that is short or runs past wTotalLength. */
const uint8_t *usb_walk_next(usb_walk_t *w);
/* The walk ended exactly at wTotalLength (no malformed descriptor). */
bool usb_walk_complete(const usb_walk_t *w);

/* "USB 2.00 046d:c31c class 00/00/00 ep0 8, 1 configuration" */
void usb_desc_device(const uint8_t *dev, char *out, size_t cap);
/* Configuration header, interface, association and endpoint lines; false
 * (out untouched) for any other descriptor. */
bool usb_desc_line(const uint8_t *desc, char *out, size_t cap);
/* String descriptor to printable ASCII ('?' for anything else). */
void usb_desc_string(const uint8_t *desc, char *out, size_t cap);
