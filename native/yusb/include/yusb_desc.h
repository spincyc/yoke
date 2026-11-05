#pragma once
/* Descriptor parsing, supported-topology checks, presentation rewrites and
 * the import-time descriptor cache (reference: src/yoke/usb/descriptors.py).
 *
 * The proxy fetches the remote device's descriptors once at import and
 * answers standard GET_DESCRIPTOR requests from rewritten copies, so they
 * meet the USB 2.0 §9.2.6.4 deadlines without a network round trip. Every
 * rewrite is listed in doc/design/USB-DATAPATH.md:
 *  - bMaxPacketSize0 (device, qualifier) becomes the UDC's ep0 size;
 *  - configuration bmAttributes: bus powered, no remote wakeup; bMaxPower
 *    declares the computer-side box's own draw;
 *  - BOS USB 2.0 Extension: the LPM bit is cleared.
 * Low-speed devices are presented at full speed with unchanged endpoints.
 *
 * Bounds below are compile-time; a device beyond them is refused with
 * YUSB_ERR_NOMEM ("descriptors exceed ...") rather than truncated.
 */
#include "yusb_ch9.h"

#ifndef YUSB_MAX_CONFIGS
#define YUSB_MAX_CONFIGS 8          /* bNumConfigurations limit (as Python) */
#endif
#ifndef YUSB_MAX_ALTS
#define YUSB_MAX_ALTS 64            /* interface descriptors per configuration */
#endif
#ifndef YUSB_MAX_CONFIG_EPS
#define YUSB_MAX_CONFIG_EPS 128     /* endpoint descriptors per configuration */
#endif
#ifndef YUSB_MAX_HID_REPORTS
#define YUSB_MAX_HID_REPORTS 4      /* cached report descriptors per alt; more are forwarded */
#endif
#ifndef YUSB_MAX_STRINGS
#define YUSB_MAX_STRINGS 96         /* cached (index, language) entries */
#endif
#ifndef YUSB_MAX_CLASS_DESCS
#define YUSB_MAX_CLASS_DESCS 16     /* cached HID report descriptors */
#endif
#ifndef YUSB_PRESENTATION_ARENA
#define YUSB_PRESENTATION_ARENA 49152u /* bytes of cached descriptor data */
#endif
#define YUSB_MAX_STRING_LANGS 4
#define YUSB_MAX_DESCRIPTOR 0xffffu

typedef struct {
    uint8_t address;    /* bEndpointAddress */
    uint8_t attributes; /* bmAttributes */
    uint8_t interval;   /* bInterval */
    uint16_t max_packet;
    uint8_t raw[9];     /* as received, zero padded: Raw Gadget EP_ENABLE copies 9 */
    uint8_t raw_len;
} yusb_endpoint_t;

static inline unsigned yusb_ep_number(const yusb_endpoint_t *e) { return e->address & 0x0fu; }
static inline bool yusb_ep_is_in(const yusb_endpoint_t *e) { return e->address & 0x80u; }
static inline unsigned yusb_ep_type(const yusb_endpoint_t *e) { return e->attributes & 3u; }

typedef struct {
    uint8_t number;
    uint8_t alternate;
    uint8_t cls;
    uint8_t subclass;
    uint8_t protocol;
    uint8_t string_index;
    uint8_t num_endpoints;
    uint8_t num_hid_reports;
    uint16_t first_endpoint; /* index into yusb_config_t.endpoints */
    uint16_t hid_report_length[YUSB_MAX_HID_REPORTS];
} yusb_alt_t;

typedef struct {
    uint8_t value;          /* bConfigurationValue */
    uint8_t attributes;     /* as the device reports them (before rewrite) */
    uint8_t max_power;
    uint8_t string_index;
    uint8_t num_interfaces; /* distinct interface numbers (== bNumInterfaces) */
    uint16_t num_alts;      /* in descriptor order */
    uint16_t num_endpoints;
    yusb_alt_t alts[YUSB_MAX_ALTS];
    yusb_endpoint_t endpoints[YUSB_MAX_CONFIG_EPS];
} yusb_config_t;

static inline const yusb_endpoint_t *yusb_alt_endpoint(const yusb_config_t *c,
                                                        const yusb_alt_t *a, unsigned i)
{
    return &c->endpoints[a->first_endpoint + i];
}
const yusb_alt_t *yusb_config_alt(const yusb_config_t *c, unsigned number, unsigned alternate);
bool yusb_config_has_interface(const yusb_config_t *c, unsigned number);

typedef struct {
    uint16_t bcd_usb;
    uint8_t cls;
    uint8_t subclass;
    uint8_t protocol;
    uint8_t max_packet0;
    uint16_t vendor;
    uint16_t product;
    uint16_t bcd_device;
    uint8_t i_manufacturer;
    uint8_t i_product;
    uint8_t i_serial;
    uint8_t num_configurations;
} yusb_device_desc_t;

/* Parsers write a short reason into `why` (may be NULL) on failure. */
int yusb_parse_device(const uint8_t *raw, size_t len, yusb_device_desc_t *out,
                      char *why, size_t cap);
/* A full configuration or other-speed descriptor set (wTotalLength bytes). */
int yusb_parse_config(const uint8_t *raw, size_t len, yusb_config_t *out,
                      char *why, size_t cap);
/* Validate a sequence of TLV descriptors (bLength >= 2, in bounds). */
int yusb_desc_check_tlv(const uint8_t *raw, size_t len);

/* In-place presentation rewrites. */
void yusb_rewrite_device(uint8_t *raw, size_t len, unsigned ep0_size);
int yusb_rewrite_config(uint8_t *raw, size_t len, unsigned power_ma);
int yusb_rewrite_bos(uint8_t *raw, size_t len);

/* Non-control endpoints the UDC exposes at once (⚠ RK3566 unmeasured).
 * max_number bounds endpoint numbers: Raw Gadget maps a descriptor to a
 * fixed-function UDC endpoint (dwc3 epNin/epNout). */
typedef struct {
    uint8_t max_in;
    uint8_t max_out;
    uint8_t max_number;
} yusb_budget_t;

/* One UDC endpoint as Raw Gadget EPS_INFO reports it. */
typedef struct {
    uint8_t number;           /* YUSB_UDC_EP_ANY: configurable address */
    bool control, iso, bulk, interrupt, dir_in, dir_out;
    uint16_t maxpacket_limit;
} yusb_udc_ep_t;
#define YUSB_UDC_EP_ANY 0xffu
bool yusb_udc_ep_supports(const yusb_udc_ep_t *ep, unsigned address, unsigned type,
                          unsigned packet_size);

/* Low/full speed present at full speed, high at high; else UNKNOWN. */
yusb_speed_t yusb_presented_speed(unsigned remote_speed);
/* YUSB_OK or YUSB_ERR_UNSUPPORTED with the first refused property in
 * `why` (descriptors.check_supported wording). */
int yusb_check_speed(unsigned speed, char *why, size_t cap);
int yusb_check_supported(const yusb_device_desc_t *device, const yusb_config_t *configs,
                         size_t count, unsigned speed, const yusb_budget_t *budget,
                         char *why, size_t cap);

/* A slice of the presentation arena. present == 0 means STALL/absent. */
typedef struct {
    uint32_t offset;
    uint16_t len;
    uint8_t present;
} yusb_blob_t;

/* Everything needed to answer standard requests without the network. */
typedef struct {
    yusb_speed_t remote_speed;
    yusb_speed_t speed;               /* presented */
    yusb_device_desc_t device;
    uint8_t num_configs;
    yusb_config_t configs[YUSB_MAX_CONFIGS];
    yusb_blob_t device_raw;           /* rewritten */
    yusb_blob_t config_raw[YUSB_MAX_CONFIGS];
    yusb_blob_t qualifier, other_speed, bos;
    uint16_t num_strings;
    struct {
        uint8_t index;
        uint16_t lang;
        yusb_blob_t data;             /* present == 0: the device STALLed */
    } strings[YUSB_MAX_STRINGS];
    uint16_t num_class;
    struct {
        uint8_t interface, type, index;
        yusb_blob_t data;
    } class_descs[YUSB_MAX_CLASS_DESCS];
    uint32_t arena_used;
    uint8_t arena[YUSB_PRESENTATION_ARENA];
} yusb_presentation_t;

static inline const uint8_t *yusb_blob(const yusb_presentation_t *p, yusb_blob_t b)
{
    return b.present ? p->arena + b.offset : NULL;
}
const yusb_config_t *yusb_presentation_config(const yusb_presentation_t *p, unsigned value);
/* Answer a device-recipient GET_DESCRIPTOR from the cache. Returns 1 when
 * known (*data NULL means STALL), 0 when it must be forwarded. */
int yusb_presentation_lookup(const yusb_presentation_t *p, const yusb_setup_t *s,
                             const uint8_t **data, size_t *len);
/* Cached interface-recipient class descriptor (HID report), or NULL. */
const uint8_t *yusb_presentation_class(const yusb_presentation_t *p, unsigned interface,
                                       unsigned type, unsigned index, size_t *len);
/* Endpoints of any configuration no UDC endpoint can carry, formatted as
 * "0x03, 0x84" (sorted, unique). Returns the number found. */
size_t yusb_missing_endpoints(const yusb_presentation_t *p, const yusb_udc_ep_t *eps,
                              size_t count, char *out, size_t cap);

/* Fetch, validate and rewrite as an explicit step machine
 * (descriptors.build_presentation). The caller performs each control IN
 * request on the remote device and feeds the answer back:
 *
 *   while ((rc = yusb_builder_next(&b, &req)) == 1)
 *       yusb_builder_feed(&b, data, len, stalled);
 *   rc == 0: done; rc < 0: YUSB_ERR_DESCRIPTOR/UNSUPPORTED/NOMEM, b.why.
 *
 * Fields are private. */
typedef struct {
    yusb_presentation_t *p;
    unsigned power_ma, ep0_size;
    yusb_budget_t budget;
    int step, error;
    yusb_setup_t pending;
    unsigned config, lang, cursor, alt, report;
    uint16_t langs[YUSB_MAX_STRING_LANGS];
    uint8_t num_langs;
    uint8_t wanted[32];               /* string index bitmap */
    yusb_config_t scratch;            /* other-speed validation */
    char why[200];
    bool prefetch_os;                 /* set after init: also cache string 0xEE */
    uint16_t max_request;             /* set after init: longest fetch (65535) */
} yusb_builder_t;

void yusb_builder_init(yusb_builder_t *b, yusb_presentation_t *p, unsigned remote_speed,
                       unsigned power_ma, unsigned ep0_size, const yusb_budget_t *budget);
int yusb_builder_next(yusb_builder_t *b, yusb_setup_t *request);
/* stalled: the device answered STALL (RET_SUBMIT -EPIPE). */
int yusb_builder_feed(yusb_builder_t *b, const uint8_t *data, size_t len, bool stalled);
