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

#define JOURNAL_MAGIC_V 0x4a544f59u      /* "YOTJ" little-endian */
#define JOURNAL_MAGIC_LEGACY 0x594f5401u
#define HEADER 12u

static void put32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); p[2] = (uint8_t)(v >> 16); p[3] = (uint8_t)(v >> 24);
}

static void put16(uint8_t *p, uint32_t v) { p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); }

static uint32_t get32(const uint8_t *p)
{
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}

static uint32_t get16(const uint8_t *p) { return (uint32_t)p[0] | (uint32_t)p[1] << 8; }

/* Fixed-width text fields must be NUL-terminated inside the field. */
static bool text(char *out, const uint8_t *in, size_t width)
{
    if (!memchr(in, 0, width)) return false;
    memcpy(out, in, width);
    return true;
}

static yoke_journal_result_t fields(const uint8_t *p, yoke_journal_t *j)
{
    yoke_journal_t value;
    value.target_address = get32(p);
    value.pending = get32(p + 4);
    p += 8;
    if (value.pending > 1 ||
        !text(value.digest, p, sizeof(value.digest)) ||
        !text(value.version, p + 65, sizeof(value.version)) ||
        !text(value.state, p + 98, sizeof(value.state)) ||
        !text(value.error, p + 122, sizeof(value.error))) return YOKE_JOURNAL_CORRUPT;
    *j = value;
    return YOKE_JOURNAL_OK;
}

size_t yoke_ota_journal_encode(const yoke_journal_t *j, uint8_t *blob, size_t cap)
{
    if (cap < YOKE_JOURNAL_V1_SIZE) return 0;
    memset(blob, 0, YOKE_JOURNAL_V1_SIZE);
    put32(blob, JOURNAL_MAGIC_V);
    put16(blob + 4, YOKE_JOURNAL_VERSION);
    put16(blob + 6, 1); /* oldest reader version that can read this prefix */
    put16(blob + 8, YOKE_JOURNAL_V1_SIZE);
    put32(blob + HEADER, j->target_address);
    put32(blob + HEADER + 4, j->pending);
    uint8_t *p = blob + HEADER + 8;
    memcpy(p, j->digest, sizeof(j->digest));
    memcpy(p + 65, j->version, sizeof(j->version));
    memcpy(p + 98, j->state, sizeof(j->state));
    memcpy(p + 122, j->error, sizeof(j->error));
    p[64] = p[65 + 32] = p[98 + 23] = p[122 + 79] = 0;
    return YOKE_JOURNAL_V1_SIZE;
}

yoke_journal_result_t yoke_ota_journal_decode(const uint8_t *blob, size_t size,
                                              yoke_journal_t *j)
{
    if (!blob || size < HEADER || size > YOKE_JOURNAL_BLOB_MAX ||
        get32(blob) != JOURNAL_MAGIC_V || get16(blob + 8) != size) return YOKE_JOURNAL_CORRUPT;
    uint32_t version = get16(blob + 4), compat = get16(blob + 6);
    if (!version || compat > version) return YOKE_JOURNAL_CORRUPT;
    if (compat > YOKE_JOURNAL_VERSION) return YOKE_JOURNAL_UNKNOWN_VERSION;
    /* Same or newer version that keeps the v1 prefix readable. */
    if (size < YOKE_JOURNAL_V1_SIZE) return YOKE_JOURNAL_CORRUPT;
    return fields(blob + HEADER, j);
}

yoke_journal_result_t yoke_ota_journal_decode_legacy(const uint8_t *blob, size_t size,
                                                     yoke_journal_t *j)
{
    /* 0.1.0 wrote its C struct: magic, target, pending, then text fields,
     * padded to 216 bytes on both ESP32-S3 and the host ABI. */
    if (!blob || size != YOKE_JOURNAL_LEGACY_SIZE || get32(blob) != JOURNAL_MAGIC_LEGACY)
        return YOKE_JOURNAL_CORRUPT;
    return fields(blob + 4, j);
}

yoke_health_t yoke_ota_health_decide(const yoke_health_inputs_t *in)
{
    if (!in->local_ok || !in->ir_ok) return YOKE_HEALTH_FAIL;
    if (!in->listeners_up) return YOKE_HEALTH_WAIT;
    bool link_down = !in->link_up &&
                     (in->link_down_seen || in->ms_since_eth_start >= in->link_settle_ms);
    return in->ip_acquired || link_down ? YOKE_HEALTH_CONFIRM : YOKE_HEALTH_WAIT;
}
