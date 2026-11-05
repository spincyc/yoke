/* USB chapter 9 helpers and shared error text (reference: ch9.py). */
#include <string.h>
#include "yusb_ch9.h"

const char *yusb_strerror(int err)
{
    switch (err) {
    case YUSB_OK: return "ok";
    case YUSB_ERR_PROTOCOL: return "protocol error";
    case YUSB_ERR_RANGE: return "value out of range";
    case YUSB_ERR_DESCRIPTOR: return "malformed descriptor";
    case YUSB_ERR_UNSUPPORTED: return "unsupported device";
    case YUSB_ERR_NOMEM: return "bound exceeded";
    case YUSB_ERR_STATE: return "invalid state";
    default: return "unknown error";
    }
}

static const char *const speed_names[] = {
    "UNKNOWN", "low-speed", "full-speed", "high-speed",
    "wireless", "super-speed", "super-speed-plus",
};

const char *yusb_speed_name(unsigned speed)
{
    return speed < sizeof(speed_names) / sizeof(speed_names[0]) ? speed_names[speed] : "UNKNOWN";
}

yusb_speed_t yusb_speed_from_name(const char *text, size_t len)
{
    while (len && (*text == ' ' || *text == '\t' || *text == '\n' || *text == '\r')) { text++; len--; }
    while (len && (text[len - 1] == ' ' || text[len - 1] == '\t' || text[len - 1] == '\n' ||
                   text[len - 1] == '\r')) len--;
    for (unsigned i = 1; i < sizeof(speed_names) / sizeof(speed_names[0]); ++i)
        if (strlen(speed_names[i]) == len && !memcmp(speed_names[i], text, len))
            return (yusb_speed_t)i;
    return YUSB_SPEED_UNKNOWN;
}

uint32_t yusb_urb_interval(unsigned speed, unsigned transfer_type, unsigned b_interval)
{
    if (transfer_type != YUSB_XFER_INT && transfer_type != YUSB_XFER_ISOC) return 0;
    if (speed >= YUSB_SPEED_HIGH) {
        unsigned exp = b_interval < 1 ? 1 : b_interval > 16 ? 16 : b_interval;
        return 1u << (exp - 1);   /* microframes: 2**(bInterval-1) */
    }
    return b_interval < 1 ? 1 : b_interval;
}
