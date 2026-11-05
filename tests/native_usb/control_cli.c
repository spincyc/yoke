/* Test driver for native/yusb/linux/control.c and native/yusb/src/target.c.
 * test_native_control.py feeds one case per stdin line and compares each
 * output line with control.py. Byte strings travel as hex ("x<hex>"; "!"
 * is a NULL pointer) so any byte, '\n' included, can be a case.
 *
 *   parse     x<request>          -> S | R <gen> - | R <gen> <host> <port> <busid> <source|->
 *                                    | E <category number> x<category text>
 *   validate  <host|busid|ipv4> x<text> -> OK [canonical] | ERR
 *   status    <cap> <gen> <reconnects> <updated as 16 hex digits of the double>
 *             <state> <detail> <udc> <link> <target|-> <device|-> <counters|->
 *             target: <host>,<port>,<busid>,<source>; device: <key>:<value>,...;
 *             counters: <name>:<decimal>,...      -> x<reply> | Z (returned 0)
 *   error     <cap> <error>                       -> x<reply> | Z
 *   selftest                                       -> ok (or aborts)
 */
#define _POSIX_C_SOURCE 200809L  /* strtok_r */
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "control.h"

#define LINE_MAX_BYTES (1u << 17)
#define ITEMS_MAX 64

static char line_buf[LINE_MAX_BYTES];

static int nibble(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

/* Decode "x<hex>" into a NUL-terminated buffer; NULL for "!". */
static char *unhex(const char *token, char *out, size_t cap, size_t *len)
{
    if (!strcmp(token, "!")) return NULL;
    if (token[0] != 'x') {
        fprintf(stderr, "bad token %s\n", token);
        exit(2);
    }
    size_t n = 0;
    for (const char *p = token + 1; p[0] && p[1]; p += 2) {
        if (n + 1 >= cap) exit(3);
        out[n++] = (char)(nibble(p[0]) << 4 | nibble(p[1]));
    }
    out[n] = '\0';
    if (len) *len = n;
    return out;
}

static void print_hex(const char *data, size_t len)
{
    putchar('x');
    for (size_t i = 0; i < len; ++i) printf("%02x", (unsigned char)data[i]);
}

static void do_parse(char *arg)
{
    static char request[LINE_MAX_BYTES / 2];
    size_t len;
    yctl_request_t req;
    yctl_error_t why = YCTL_E_NONE;
    unhex(arg, request, sizeof(request), &len);
    memset(&req, 'Z', sizeof(req));
    int rc = yctl_parse_request(request, len, &req, &why);
    if (rc) {
        assert(rc == YUSB_ERR_PROTOCOL && why != YCTL_E_NONE);
        const char *text = yctl_error_text(why);
        printf("E %d ", (int)why);
        print_hex(text, strlen(text));
    } else if (!req.route) {
        assert(why == YCTL_E_NONE);
        putchar('S');
    } else if (!req.has_target) {
        printf("R %llu -", (unsigned long long)req.generation);
    } else {
        const yusb_target_t *t = &req.target;
        printf("R %llu %s %u %s %s", (unsigned long long)req.generation, t->host, t->port, t->busid,
               t->source[0] ? t->source : "-");
    }
    putchar('\n');
}

static void do_validate(char *arg)
{
    char *kind = strtok(arg, " "), *token = strtok(NULL, " ");
    static char text[LINE_MAX_BYTES / 2];
    char out[YUSB_HOST_MAX + 1];
    size_t len;
    unhex(token, text, sizeof(text), &len);
    int rc;
    memset(out, 'Z', sizeof(out));
    if (!strcmp(kind, "host")) {
        rc = yusb_validate_host(text, len, out);
    } else if (!strcmp(kind, "busid")) {
        rc = yusb_validate_busid(text, len);
        out[0] = '\0';
    } else {
        rc = yusb_validate_ipv4(text, len, out);
        assert(!rc ? strlen(out) < 16 : !out[0]);
    }
    if (rc) {
        assert(rc == YUSB_ERR_RANGE);
        puts("ERR");
    } else {
        printf("OK%s%s\n", out[0] ? " " : "", out);
    }
}

static void check_reply(const char *out, size_t cap, size_t n)
{
    if (!n) {
        puts("Z");
        return;
    }
    assert(n < cap && n <= YCTL_MAX_MESSAGE);
    assert(strlen(out) == n && out[n - 1] == '\n');
    print_hex(out, n);
    putchar('\n');
}

static void do_status(char *arg)
{
    static char strings[8][LINE_MAX_BYTES / 8];
    static char keys[ITEMS_MAX][256], values[ITEMS_MAX][256], names[ITEMS_MAX][256];
    yctl_pair_t device[ITEMS_MAX];
    yctl_counter_t counters[ITEMS_MAX];
    yusb_target_t target;
    yctl_status_t st;
    char *fields[11], *save = NULL;
    for (int i = 0; i < 11; ++i) fields[i] = strtok_r(i ? NULL : arg, " ", &save);
    size_t cap = strtoull(fields[0], NULL, 10);
    unsigned long long bits = strtoull(fields[3], NULL, 16);
    memset(&st, 0, sizeof(st));
    st.generation = strtoull(fields[1], NULL, 10);
    st.reconnects = (uint32_t)strtoul(fields[2], NULL, 10);
    memcpy(&st.updated, &bits, sizeof(st.updated));
    st.state = unhex(fields[4], strings[0], sizeof(strings[0]), NULL);
    st.detail = unhex(fields[5], strings[1], sizeof(strings[1]), NULL);
    st.udc = unhex(fields[6], strings[2], sizeof(strings[2]), NULL);
    st.link_speed = unhex(fields[7], strings[3], sizeof(strings[3]), NULL);
    if (strcmp(fields[8], "-")) {
        char *parts[4], *inner = NULL;
        for (int i = 0; i < 4; ++i) parts[i] = strtok_r(i ? NULL : fields[8], ",", &inner);
        memset(&target, 0, sizeof(target));
        unhex(parts[0], target.host, sizeof(target.host), NULL);
        target.port = (uint16_t)strtoul(parts[1], NULL, 10);
        unhex(parts[2], target.busid, sizeof(target.busid), NULL);
        unhex(parts[3], target.source, sizeof(target.source), NULL);
        st.target = &target;
    }
    if (strcmp(fields[9], "-")) {
        char *item, *inner = NULL;
        for (item = strtok_r(fields[9], ",", &inner); item; item = strtok_r(NULL, ",", &inner)) {
            char *colon = strchr(item, ':');
            *colon = '\0';
            size_t k = st.device_count++;
            device[k].key = unhex(item, keys[k], sizeof(keys[k]), NULL);
            device[k].value = unhex(colon + 1, values[k], sizeof(values[k]), NULL);
        }
        st.device = device;
    }
    if (strcmp(fields[10], "-")) {
        char *item, *inner = NULL;
        for (item = strtok_r(fields[10], ",", &inner); item; item = strtok_r(NULL, ",", &inner)) {
            char *colon = strchr(item, ':');
            *colon = '\0';
            size_t k = st.counter_count++;
            counters[k].name = unhex(item, names[k], sizeof(names[k]), NULL);
            counters[k].value = strtoull(colon + 1, NULL, 10);
        }
        st.counters = counters;
    }
    static char out[4 * YCTL_MAX_MESSAGE];
    assert(cap <= sizeof(out));
    memset(out, 'Z', sizeof(out));
    size_t n = yctl_format_status(&st, out, cap);
    assert(!memchr(out + cap, 0, sizeof(out) - cap));  /* never writes past cap */
    check_reply(out, cap, n);
}

static void do_error(char *arg)
{
    static char text[LINE_MAX_BYTES / 2];
    static char out[4 * YCTL_MAX_MESSAGE];
    char *cap_text = strtok(arg, " "), *token = strtok(NULL, " ");
    size_t cap = strtoull(cap_text, NULL, 10);
    const char *error = unhex(token, text, sizeof(text), NULL);
    assert(cap <= sizeof(out));
    memset(out, 'Z', sizeof(out));
    size_t n = yctl_format_error(error, out, cap);
    assert(!memchr(out + cap, 0, sizeof(out) - cap));
    check_reply(out, cap, n);
}

static void parse_ok(const char *line, yctl_request_t *req)
{
    yctl_error_t why;
    int rc = yctl_parse_request(line, strlen(line), req, &why);
    if (rc) fprintf(stderr, "%s: %s\n", line, yctl_error_text(why));
    assert(!rc);
}

static void selftest(void)
{
    yctl_request_t a, b, c;
    yctl_error_t why;
    char small[3];
    parse_ok("{\"schema\":1,\"op\":\"route\",\"generation\":5,\"target\":{\"host\":\"Box.Example.\","
             "\"port\":3240,\"busid\":\"1-1\",\"source\":null}}\n", &a);
    parse_ok("{\"schema\":1,\"op\":\"route\",\"generation\":6,\"target\":{\"source\":null,"
             "\"busid\":\"1-1\",\"port\":3240,\"host\":\"box.example\"}}\n", &b);
    parse_ok("{\"schema\":1,\"op\":\"route\",\"generation\":6,\"target\":{\"source\":\"10.0.0.1\","
             "\"busid\":\"1-1\",\"port\":3240,\"host\":\"box.example\"}}\n", &c);
    assert(yusb_target_equal(&a.target, &b.target));
    assert(!yusb_target_equal(&a.target, &c.target));
    assert(yusb_target_equal(&a.target, &a.target) && !yusb_target_equal(&a.target, NULL));
    b.target.port = 3241;
    assert(!yusb_target_equal(&a.target, &b.target));
    /* Refusals carry a category; a refused request leaves *out zeroed. */
    const char *bad = "{\"schema\":2}\n";
    assert(yctl_parse_request(bad, strlen(bad), &a, &why) == YUSB_ERR_PROTOCOL);
    assert(why == YCTL_E_SCHEMA && !strcmp(yctl_error_text(why), "unknown request schema"));
    assert(!a.route && !a.generation && !a.target.host[0]);
    assert(yctl_parse_request(bad, strlen(bad), &a, NULL) == YUSB_ERR_PROTOCOL);
    assert(yctl_parse_request(NULL, 0, &a, &why) == YUSB_ERR_PROTOCOL && why == YCTL_E_LINE);
    assert(yctl_parse_request("{\"schema\":1,\"op\":\"status\"}\n", 27, NULL, &why) == YUSB_ERR_PROTOCOL);
    for (int k = YCTL_E_LINE; k <= YCTL_E_SOURCE; ++k) assert(yctl_error_text((yctl_error_t)k)[0]);
    /* Replies that do not fit return 0 and leave an empty string. */
    assert(yctl_format_error("x", small, sizeof(small)) == 0 && small[0] == '\0');
    assert(yctl_format_error("x", NULL, 100) == 0);
    assert(yctl_format_status(NULL, small, sizeof(small)) == 0);
    /* Validators accept a NULL output and refuse NULL input. */
    assert(yusb_validate_host("::1", 3, NULL) == YUSB_OK);
    assert(yusb_validate_ipv4("1.2.3.4", 7, NULL) == YUSB_OK);
    assert(yusb_validate_host(NULL, 3, NULL) == YUSB_ERR_RANGE);
    assert(yusb_validate_busid(NULL, 3) == YUSB_ERR_RANGE);
    assert(yusb_validate_ipv4(NULL, 3, NULL) == YUSB_ERR_RANGE);
    puts("ok");
}

int main(int argc, char **argv)
{
    if (argc != 2) return 2;
    if (!strcmp(argv[1], "selftest")) {
        selftest();
        return 0;
    }
    while (fgets(line_buf, sizeof(line_buf), stdin)) {
        size_t n = strlen(line_buf);
        if (n && line_buf[n - 1] == '\n') line_buf[--n] = '\0';
        if (!strcmp(argv[1], "parse")) {
            do_parse(line_buf);
        } else if (!strcmp(argv[1], "validate")) {
            do_validate(line_buf);
        } else if (!strcmp(argv[1], "status")) {
            do_status(line_buf);
        } else if (!strcmp(argv[1], "error")) {
            do_error(line_buf);
        } else {
            return 2;
        }
    }
    return 0;
}
