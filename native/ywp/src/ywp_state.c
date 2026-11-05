/* Durable state blob and sequence floors (ywp_state.h, ADRs 0012, 0023). */
#include <string.h>
#include "ywp_internal.h"
#include "ywp_state.h"

#define MAGIC0 'Y'
#define MAGIC1 'W'
#define MAGIC2 'S'
#define FORMAT 1u

static uint32_t crc32(const uint8_t *p, size_t len)
{
    uint32_t crc = 0xffffffffu;
    for (size_t i = 0; i < len; ++i) {
        crc ^= p[i];
        for (unsigned bit = 0; bit < 8; ++bit)
            crc = (crc >> 1) ^ (0xedb88320u & (0u - (crc & 1u)));
    }
    return ~crc;
}

static void put_u64(uint8_t *p, uint64_t v)
{
    for (unsigned i = 0; i < 8; ++i) p[i] = (uint8_t)(v >> (8 * i));
}

static uint64_t get_u64(const uint8_t *p)
{
    uint64_t v = 0;
    for (unsigned i = 0; i < 8; ++i) v |= (uint64_t)p[i] << (8 * i);
    return v;
}

/* A NUL-terminated id that fits its field: length, or SIZE_MAX. */
static size_t id_len(const char *id)
{
    for (size_t len = 0; len <= YWP_ID_MAX; ++len)
        if (!id[len]) return len;
    return SIZE_MAX;
}

bool ywp_saved_valid(const ywp_saved_t *s)
{
    const ywp_pairing_t *p = &s->pairing;
    size_t olen = id_len(p->version.origin), plen = id_len(p->producer);
    if (s->seq > YWP_MAX_SEQUENCE || p->version.n > YWP_MAX_SEQUENCE || olen == SIZE_MAX)
        return false;
    if (p->paired && (plen == SIZE_MAX || !ywp_id_valid(p->producer, plen))) return false;
    if (p->version.n == 0) return olen == 0 && !p->paired;
    return ywp_id_valid(p->version.origin, olen);
}

size_t ywp_saved_encode(const ywp_saved_t *s, uint8_t out[YWP_SAVED_MAX])
{
    if (!ywp_saved_valid(s)) return 0;
    size_t plen = s->pairing.paired ? strlen(s->pairing.producer) : 0;
    size_t olen = strlen(s->pairing.version.origin), at = 0;
    out[at++] = MAGIC0;
    out[at++] = MAGIC1;
    out[at++] = MAGIC2;
    out[at++] = FORMAT;
    put_u64(out + at, s->seq);
    at += 8;
    put_u64(out + at, s->pairing.version.n);
    at += 8;
    out[at++] = (uint8_t)plen;
    memcpy(out + at, s->pairing.producer, plen);
    at += plen;
    out[at++] = (uint8_t)olen;
    memcpy(out + at, s->pairing.version.origin, olen);
    at += olen;
    uint32_t crc = crc32(out, at);
    for (unsigned i = 0; i < 4; ++i) out[at++] = (uint8_t)(crc >> (8 * i));
    return at;
}

bool ywp_saved_decode(const uint8_t *data, size_t len, ywp_saved_t *s)
{
    if (len < 4 + 8 + 8 + 1 + 1 + 4 || len > YWP_SAVED_MAX) return false;
    if (data[0] != MAGIC0 || data[1] != MAGIC1 || data[2] != MAGIC2 || data[3] != FORMAT)
        return false;
    uint32_t crc = 0;
    for (unsigned i = 0; i < 4; ++i) crc |= (uint32_t)data[len - 4 + i] << (8 * i);
    if (crc != crc32(data, len - 4)) return false;
    ywp_saved_t out;
    memset(&out, 0, sizeof(out));
    size_t at = 4;
    out.seq = get_u64(data + at);
    at += 8;
    out.pairing.version.n = get_u64(data + at);
    at += 8;
    size_t plen = data[at++];
    if (plen > YWP_ID_MAX || at + plen + 1 + 4 > len) return false;
    memcpy(out.pairing.producer, data + at, plen);
    out.pairing.paired = plen > 0;
    at += plen;
    size_t olen = data[at++];
    if (olen > YWP_ID_MAX || at + olen + 4 != len) return false;
    memcpy(out.pairing.version.origin, data + at, olen);
    if (memchr(out.pairing.producer, 0, plen) || memchr(out.pairing.version.origin, 0, olen))
        return false;
    if (!ywp_saved_valid(&out)) return false;
    *s = out;
    return true;
}

uint64_t ywp_whole_seconds(double wall)
{
    if (!(wall >= 0.0)) return 0;
    if (wall >= 18446744073709551616.0) return UINT64_MAX;
    return (uint64_t)wall;
}

static uint64_t sat_add(uint64_t a, uint64_t b)
{
    return a > UINT64_MAX - b ? UINT64_MAX : a + b;
}

static uint64_t sat_ns(uint64_t seconds)
{
    return seconds > UINT64_MAX / YWP_NS_PER_S ? UINT64_MAX : seconds * YWP_NS_PER_S;
}

uint64_t ywp_restart_floor(uint64_t saved, double wall)
{
    uint64_t wall_ns = sat_ns(ywp_whole_seconds(wall));
    if (saved == 0) return wall_ns;
    uint64_t jump = sat_add(saved, (uint64_t)YWP_SEQ_FLOOR_JUMP_S * YWP_NS_PER_S);
    uint64_t capped = wall_ns < jump ? wall_ns : jump;
    return capped > saved ? capped : saved;
}

uint64_t ywp_admission_bound(double wall)
{
    return sat_ns(sat_add(ywp_whole_seconds(wall), YWP_SEQ_FUTURE_MARGIN_S));
}
