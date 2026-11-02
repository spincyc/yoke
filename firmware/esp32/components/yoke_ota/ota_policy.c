#include "ota_policy.h"
#include <string.h>

bool yoke_ota_parse_digest(const char *hex, uint8_t out[32])
{
    if (!hex || strlen(hex) != 64) return false;
    for (size_t i = 0; i < 32; ++i) {
        unsigned value = 0;
        for (size_t j = 0; j < 2; ++j) {
            unsigned char c = hex[i * 2 + j];
            if (c >= '0' && c <= '9') value = value * 16 + c - '0';
            else if (c >= 'a' && c <= 'f') value = value * 16 + c - 'a' + 10;
            else return false;
        }
        out[i] = (uint8_t)value;
    }
    return true;
}

bool yoke_ota_length_allowed(size_t length, size_t slot_size)
{
    /* ESP image header + segment header + app description, then content. */
    return length >= 288 && length <= slot_size;
}

bool yoke_ota_parse_length(const char *text, size_t limit, size_t *out)
{
    if (!text || !*text) return false;
    size_t value = 0;
    for (const unsigned char *p = (const unsigned char *)text; *p; ++p) {
        if (*p < '0' || *p > '9') return false;
        size_t digit = *p - '0';
        if (digit > limit || value > (limit - digit) / 10) return false;
        value = value * 10 + digit;
    }
    if (!value) return false;
    *out = value;
    return true;
}

const char *yoke_ota_boot_state(bool pending_verify, bool journal_pending,
                              bool running_is_target)
{
    if (pending_verify) return "pending-verify";
    if (journal_pending && !running_is_target) return "rolled-back";
    return "idle";
}
