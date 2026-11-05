/* Route-target validators (ADR 0022; reference: src/yoke/usb/control.py
 * validate_host, validate_busid, validate_source and Target equality).
 *
 * Portable C11 for the Linux worker and ESP-IDF alike: the IPv4 and IPv6
 * parsers follow CPython 3.14's ipaddress module rule for rule instead of
 * inet_pton, whose leniency (leading zeros, short forms) varies by libc.
 *
 * Known difference from control.py: a scoped IPv6 literal ("fe80::1%eth0")
 * is rejected. Python accepts it, but the worker resolves and connects over
 * IPv4 only (worker.connect_usbip uses AF_INET), and a zone name is
 * arbitrary text that does not belong in a fixed-size host field.
 */
#include <string.h>
#include "yusb_fence.h"

static bool is_digit(char c)
{
    return c >= '0' && c <= '9';
}

static bool is_alnum(char c)
{
    return is_digit(c) || (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z');
}

static int hex_digit(char c)
{
    if (is_digit(c)) return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

/* ipaddress.IPv4Address: exactly four dot-separated octets of 1..3 ASCII
 * digits, no leading zero (bpo-36384), each at most 255. */
static bool parse_ipv4(const char *s, size_t len, uint32_t *addr)
{
    uint32_t value = 0;
    size_t i = 0;
    for (int octet = 0; octet < 4; ++octet) {
        if (octet) {
            if (i >= len || s[i] != '.') return false;
            ++i;
        }
        size_t start = i;
        uint32_t part = 0;
        while (i < len && i - start < 3 && is_digit(s[i])) part = part * 10 + (uint32_t)(s[i++] - '0');
        size_t digits = i - start;
        if (!digits || (digits > 1 && s[start] == '0') || part > 255) return false;
        value = value << 8 | part;
    }
    if (i != len) return false;
    *addr = value;
    return true;
}

static size_t put_decimal(char *out, uint32_t value)
{
    char tmp[10];
    size_t n = 0;
    do {
        tmp[n++] = (char)('0' + value % 10);
        value /= 10;
    } while (value);
    for (size_t i = 0; i < n; ++i) out[i] = tmp[n - 1 - i];
    return n;
}

static size_t format_ipv4(uint32_t addr, char *out)
{
    size_t n = 0;
    for (int shift = 24; shift >= 0; shift -= 8) {
        if (shift != 24) out[n++] = '.';
        n += put_decimal(out + n, (addr >> shift) & 0xffu);
    }
    out[n] = '\0';
    return n;
}

/* A part of an IPv6 literal: a text span, or a hextet already computed
 * from a dotted IPv4 tail. */
typedef struct {
    size_t start, len;
    bool computed;
    uint16_t value;
} part_t;

static bool part_empty(const part_t *p)
{
    return !p->computed && !p->len;
}

/* ipaddress._parse_hextet: 1..4 ASCII hex digits. */
static bool parse_hextet(const char *s, const part_t *p, uint16_t *out)
{
    if (p->computed) {
        *out = p->value;
        return true;
    }
    if (p->len < 1 || p->len > 4) return false;
    uint32_t value = 0;
    for (size_t i = 0; i < p->len; ++i) {
        int digit = hex_digit(s[p->start + i]);
        if (digit < 0) return false;
        value = value << 4 | (uint32_t)digit;
    }
    *out = (uint16_t)value;
    return true;
}

/* ipaddress.IPv6Address._ip_int_from_string, step for step: an optional
 * dotted IPv4 tail, at most one "::", eight hextets in total. */
static bool parse_ipv6(const char *s, size_t len, uint16_t h[8])
{
    part_t parts[10];
    size_t n = 0, start = 0;
    if (!len || len > 45) return false;
    for (size_t i = 0; i <= len; ++i) {
        if (i < len && s[i] != ':') continue;
        if (n == 9) return false;  /* nine or more colons never parse */
        parts[n++] = (part_t){ .start = start, .len = i - start };
        start = i + 1;
    }
    if (n < 3) return false;
    part_t *last = &parts[n - 1];
    if (memchr(s + last->start, '.', last->len)) {
        uint32_t v4;
        if (!parse_ipv4(s + last->start, last->len, &v4)) return false;
        *last = (part_t){ .computed = true, .value = (uint16_t)(v4 >> 16) };
        parts[n++] = (part_t){ .computed = true, .value = (uint16_t)(v4 & 0xffffu) };
    }
    if (n > 9) return false;
    size_t skip = 0;  /* index 0 is never the "::" gap */
    for (size_t i = 1; i + 1 < n; ++i) {
        if (!part_empty(&parts[i])) continue;
        if (skip) return false;
        skip = i;
    }
    size_t hi, lo;
    if (skip) {
        hi = skip;
        lo = n - skip - 1;
        if (part_empty(&parts[0]) && --hi) return false;
        if (part_empty(&parts[n - 1]) && --lo) return false;
        if (hi + lo > 7) return false;
    } else {
        if (n != 8 || part_empty(&parts[0]) || part_empty(&parts[n - 1])) return false;
        hi = n;
        lo = 0;
    }
    memset(h, 0, 8 * sizeof(h[0]));
    for (size_t i = 0; i < hi; ++i)
        if (!parse_hextet(s, &parts[i], &h[i])) return false;
    for (size_t i = 0; i < lo; ++i)
        if (!parse_hextet(s, &parts[n - lo + i], &h[8 - lo + i])) return false;
    return true;
}

static size_t put_hextet(char *out, uint16_t value)
{
    static const char digits[] = "0123456789abcdef";
    size_t n = 0;
    bool started = false;
    for (int shift = 12; shift >= 0; shift -= 4) {
        unsigned d = (value >> shift) & 0xfu;
        if (!d && !started && shift) continue;
        started = true;
        out[n++] = digits[d];
    }
    return n;
}

/* str(ipaddress.IPv6Address): IPv4-mapped as "::ffff:a.b.c.d" (CPython
 * 3.13+), otherwise RFC 5952: lowercase, no leading zeros, and the first
 * longest run of two or more zero hextets written as "::". */
static void format_ipv6(const uint16_t h[8], char *out)
{
    if (!h[0] && !h[1] && !h[2] && !h[3] && !h[4] && h[5] == 0xffffu) {
        memcpy(out, "::ffff:", 7);
        format_ipv4((uint32_t)h[6] << 16 | h[7], out + 7);
        return;
    }
    int best = -1, best_len = 1;
    for (int i = 0; i < 8;) {
        if (h[i]) {
            ++i;
            continue;
        }
        int j = i;
        while (j < 8 && !h[j]) ++j;
        if (j - i > best_len) {
            best = i;
            best_len = j - i;
        }
        i = j;
    }
    size_t n = 0;
    for (int i = 0; i < 8; ++i) {
        if (i == best) {
            out[n++] = ':';
            out[n++] = ':';
            i += best_len - 1;
            continue;
        }
        if (n && out[n - 1] != ':') out[n++] = ':';
        n += put_hextet(out + n, h[i]);
    }
    out[n] = '\0';
}

int yusb_validate_host(const char *text, size_t len, char out[YUSB_HOST_MAX + 1])
{
    char scratch[YUSB_HOST_MAX + 1];
    char *dst = out ? out : scratch;
    uint32_t v4;
    uint16_t v6[8];
    dst[0] = '\0';
    if (!text || !len || len > YUSB_HOST_MAX) return YUSB_ERR_RANGE;
    if (parse_ipv4(text, len, &v4)) {
        format_ipv4(v4, dst);
        return YUSB_OK;
    }
    if (parse_ipv6(text, len, v6)) {
        format_ipv6(v6, dst);
        return YUSB_OK;
    }
    /* control._HOST plus the label rules: alphanumeric at both ends (before
     * one optional trailing dot), [A-Za-z0-9.-] between, labels 1..63 with
     * no edge hyphen. Anything non-ASCII fails here, as in Python. */
    size_t core = text[len - 1] == '.' ? len - 1 : len;
    if (!core || !is_alnum(text[0]) || !is_alnum(text[core - 1])) return YUSB_ERR_RANGE;
    size_t label = 0;
    for (size_t i = 0; i < core; ++i) {
        char c = text[i];
        if (c == '.') {
            if (!label || text[i - 1] == '-') return YUSB_ERR_RANGE;
            label = 0;
            continue;
        }
        if (c != '-' && !is_alnum(c)) return YUSB_ERR_RANGE;
        if (c == '-' && !label) return YUSB_ERR_RANGE;
        if (++label > 63) return YUSB_ERR_RANGE;
    }
    for (size_t i = 0; i < core; ++i) {
        char c = text[i];
        dst[i] = c >= 'A' && c <= 'Z' ? (char)(c - 'A' + 'a') : c;
    }
    dst[core] = '\0';
    return YUSB_OK;
}

int yusb_validate_busid(const char *text, size_t len)
{
    /* control._BUSID: [0-9]+-[0-9]+(\.[0-9]+)*, at most 31 characters. */
    if (!text || len > YUSB_TARGET_BUSID_MAX) return YUSB_ERR_RANGE;
    size_t i = 0;
    while (i < len && is_digit(text[i])) ++i;
    if (!i || i >= len || text[i] != '-') return YUSB_ERR_RANGE;
    for (++i;; ++i) {
        size_t start = i;
        while (i < len && is_digit(text[i])) ++i;
        if (i == start) return YUSB_ERR_RANGE;
        if (i == len) return YUSB_OK;
        if (text[i] != '.') return YUSB_ERR_RANGE;
    }
}

int yusb_validate_ipv4(const char *text, size_t len, char out[16])
{
    uint32_t v4;
    if (out) out[0] = '\0';
    if (!text || !parse_ipv4(text, len, &v4)) return YUSB_ERR_RANGE;
    if (out) format_ipv4(v4, out);
    return YUSB_OK;
}

/* strcmp bounded by the field size: a field the owner forgot to terminate
 * is compared in full rather than read past. */
static bool field_equal(const char *a, const char *b, size_t size)
{
    for (size_t i = 0; i < size; ++i) {
        if (a[i] != b[i]) return false;
        if (!a[i]) return true;
    }
    return true;
}

bool yusb_target_equal(const yusb_target_t *a, const yusb_target_t *b)
{
    if (a == b) return true;
    if (!a || !b) return false;
    return a->port == b->port && field_equal(a->host, b->host, sizeof(a->host)) &&
           field_equal(a->busid, b->busid, sizeof(a->busid)) &&
           field_equal(a->source, b->source, sizeof(a->source));
}
