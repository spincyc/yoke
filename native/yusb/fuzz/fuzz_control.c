/* libFuzzer target for the ADR 0022 control schema (native/yusb/linux/
 * control.c) and the route-target validators (native/yusb/src/target.c).
 *
 * Each input is used three ways: as a request line (as given and with a
 * '\n' appended), as validator input, and as the strings of a status and
 * an error reply. Beyond the sanitizers it checks the contracts callers
 * rely on: only ASCII is ever accepted, a parsed route is canonical and
 * re-validates to itself, a refusal has a category and leaves the request
 * zeroed, and replies are ASCII, newline-ended, NUL-terminated, never
 * longer than cap or YCTL_MAX_MESSAGE and never written past cap.
 *
 * clang -g -O1 -fsanitize=fuzzer,address,undefined -I native/yusb/include \
 *     -I native/yusb/linux native/yusb/fuzz/fuzz_control.c \
 *     native/yusb/src/target.c native/yusb/linux/control.c -o fuzz_control
 */
#include <stdlib.h>
#include <string.h>
#include "control.h"

#define CHECK(cond)              \
    do {                         \
        if (!(cond)) abort();    \
    } while (0)

#define PIECES 16
#define OUT_MAX (YCTL_MAX_MESSAGE + 4096)

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size);

static void check_request(const char *line, size_t len)
{
    static const yctl_request_t zero;
    yctl_request_t req;
    yctl_error_t why = YCTL_E_NONE;
    memset(&req, 'Z', sizeof(req));
    int rc = yctl_parse_request(line, len, &req, &why);
    if (rc) {
        CHECK(rc == YUSB_ERR_PROTOCOL && why > YCTL_E_NONE && why <= YCTL_E_SOURCE);
        CHECK(!memcmp(&req, &zero, sizeof(req)) && yctl_error_text(why)[0]);
        return;
    }
    CHECK(why == YCTL_E_NONE);
    CHECK(len && len <= YCTL_MAX_MESSAGE && line[len - 1] == '\n');
    for (size_t i = 0; i < len; ++i) CHECK((unsigned char)line[i] > 0 && (unsigned char)line[i] < 0x80);
    if (!req.route) return;
    CHECK(req.generation >= 1 && req.generation <= INT64_MAX);
    if (!req.has_target) return;
    /* The parsed target is canonical: validating it again is the identity. */
    const yusb_target_t *t = &req.target;
    char host[YUSB_HOST_MAX + 1], source[16];
    CHECK(t->port);
    CHECK(yusb_validate_host(t->host, strlen(t->host), host) == YUSB_OK && !strcmp(host, t->host));
    CHECK(yusb_validate_busid(t->busid, strlen(t->busid)) == YUSB_OK);
    if (t->source[0])
        CHECK(yusb_validate_ipv4(t->source, strlen(t->source), source) == YUSB_OK &&
              !strcmp(source, t->source));
    CHECK(yusb_target_equal(t, t));
    char reply[1024];
    CHECK(yctl_format_error(t->host, reply, sizeof(reply)) > 0);
}

static void check_validators(const char *text, size_t len)
{
    char out[YUSB_HOST_MAX + 1], again[YUSB_HOST_MAX + 1], v4[16];
    if (yusb_validate_host(text, len, out) == YUSB_OK) {
        size_t n = strlen(out);
        CHECK(n >= 1 && n <= YUSB_HOST_MAX);
        CHECK(yusb_validate_host(out, n, again) == YUSB_OK && !strcmp(out, again));
    } else {
        CHECK(!out[0]);
    }
    if (yusb_validate_busid(text, len) == YUSB_OK) CHECK(len >= 3 && len <= YUSB_TARGET_BUSID_MAX);
    if (yusb_validate_ipv4(text, len, v4) == YUSB_OK) {
        CHECK(strlen(v4) == len && !memcmp(v4, text, len));  /* strict form is canonical */
        CHECK(yusb_validate_host(text, len, out) == YUSB_OK && !strcmp(out, v4));
    }
}

static void check_reply(const char *out, size_t cap, size_t n)
{
    for (size_t i = cap; i < OUT_MAX; ++i) CHECK(out[i] == 'Z');
    if (!n) {
        if (cap) CHECK(!out[0]);
        return;
    }
    CHECK(n < cap && n <= YCTL_MAX_MESSAGE && out[n] == '\0' && out[n - 1] == '\n');
    CHECK(strlen(out) == n);
    for (size_t i = 0; i < n; ++i) CHECK((unsigned char)out[i] < 0x80);  /* ensure_ascii */
    CHECK(memchr(out, '\n', n) == out + n - 1);
}

static void check_replies(const uint8_t *data, size_t size)
{
    static char copy[YCTL_MAX_MESSAGE * 2 + 1];
    static char out[OUT_MAX];
    static const size_t caps[] = { 0, 1, 64, 300, 1000, 2048, YCTL_MAX_MESSAGE + 1, OUT_MAX };
    const char *piece[PIECES];
    size_t count = 0;
    if (size > sizeof(copy) - 1) size = sizeof(copy) - 1;
    memcpy(copy, data, size);
    copy[size] = '\0';
    /* NUL and '\n' split the input into the strings of a status. */
    for (char *p = copy, *end = copy + size; count < PIECES; ++p) {
        piece[count++] = p;
        while (p < end && *p && *p != '\n') ++p;
        if (p >= end) break;
        *p = '\0';
    }
    while (count < PIECES) piece[count++] = "";
    size_t cap = caps[size ? data[0] % (sizeof(caps) / sizeof(caps[0])) : 0];
    yusb_target_t target;
    memset(&target, 0, sizeof(target));
    strncpy(target.host, piece[4], sizeof(target.host) - 1);
    strncpy(target.busid, piece[5], sizeof(target.busid) - 1);
    strncpy(target.source, piece[6], sizeof(target.source) - 1);
    target.port = (uint16_t)size;
    yctl_pair_t device[3] = { { piece[7], piece[8] }, { piece[9], piece[10] }, { piece[7], piece[11] } };
    yctl_counter_t counters[3] = { { piece[12], size }, { piece[13], UINT64_MAX }, { piece[12], 0 } };
    double updated = 0;
    memcpy(&updated, data, size < sizeof(updated) ? size : sizeof(updated));
    yctl_status_t st = {
        .generation = size * 0x9e3779b97f4a7c15u,
        .state = piece[0],
        .detail = piece[1],
        .target = size & 1 ? &target : NULL,
        .udc = piece[2],
        .link_speed = piece[3],
        .device = device,
        .device_count = size % 4,
        .counters = counters,
        .counter_count = size % 3 + 1,
        .reconnects = (uint32_t)size,
        .updated = updated,
    };
    memset(out, 'Z', sizeof(out));
    check_reply(out, cap, yctl_format_status(&st, out, cap));
    memset(out, 'Z', sizeof(out));
    check_reply(out, cap, yctl_format_error(piece[1], out, cap));
    memset(out, 'Z', sizeof(out));
    check_reply(out, OUT_MAX, yctl_format_error(copy, out, OUT_MAX));
}

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)
{
    static char line[YCTL_MAX_MESSAGE + 2];
    const char *text = (const char *)data;
    check_request(text, size);
    if (size < YCTL_MAX_MESSAGE) {
        memcpy(line, data, size);
        line[size] = '\n';
        check_request(line, size + 1);
    }
    check_validators(text, size);
    check_replies(data, size);
    return 0;
}
