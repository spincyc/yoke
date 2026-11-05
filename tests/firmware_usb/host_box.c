/* The USB box's configuration rules (box_config.c), the USB/IP media
 * description (box_media.c), the socket transport (box_pump.c) and the
 * authentication-failure throttle (box_limit.c), with the native cores'
 * validators and JSON (native/yusb, native/ywp). Real loopback sockets; no
 * device. */
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>
#include "box_config.h"
#include "box_limit.h"
#include "box_media.h"
#include "box_pump.h"
#include "box_time.h"

/* The reference agent's recommended PSK file: 64 hex characters, used as
 * 64 bytes of key. */
static const char *KEY64 = "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f";

static int mode_config(void)
{
    box_config_t c = {.role = BOX_ROLE_COMPUTER};
    assert(!box_config_bridging(&c));
    assert(!box_config_set_text(&c, "node_id", "desk-usb") && !strcmp(c.node_id, "desk-usb"));
    assert(box_config_set_text(&c, "node_id", "Desk"));      /* ywp_id_valid */
    assert(box_config_set_text(&c, "node_id", "-desk"));
    assert(!box_config_bridging(&c));                         /* no key, not open */
    /* A PSK of at least 32 bytes, used byte for byte like the PSK file. */
    assert(!strcmp(box_config_set_text(&c, "psk", "too short"), "psk-must-be-32-to-256-bytes"));
    char huge[BOX_PSK_MAX + 2];
    memset(huge, 'k', sizeof(huge) - 1);
    huge[sizeof(huge) - 1] = 0;
    assert(box_config_set_text(&c, "psk", huge));
    assert(!box_config_set_text(&c, "psk", KEY64) && c.psk_len == 64 && c.psk[63] == 'f');
    assert(box_config_bridging(&c));
    /* Open mode is explicit and excludes a key. */
    assert(!box_config_set_flag(&c, "open", true));
    assert(!strcmp(box_config_check(&c), "open-mode-must-not-have-a-psk"));
    assert(!box_config_set_text(&c, "psk", "") && !c.psk_len && !box_config_check(&c));
    assert(box_config_bridging(&c));
    assert(!strcmp(box_config_set_text(&c, "open", "true"), "open-must-be-boolean"));
    assert(!strcmp(box_config_set_flag(&c, "role", true), "unknown-or-mistyped-field"));
    assert(!strcmp(box_config_set_text(&c, "colour", "x"), "unknown-field"));
    assert(!box_config_set_text(&c, "role", "device") && c.role == BOX_ROLE_DEVICE);
    assert(!strcmp(box_config_set_text(&c, "role", "host"), "role-must-be-device-or-computer"));
    /* The node's name rule at PUT: UTF-8 of at most 64 code points. */
    static const char *const too_long = "name-must-be-utf8-of-at-most-64-characters";
    char name[BOX_NAME_MAX + 2];
    memset(name, 'a', 64);
    name[64] = 0;
    assert(!box_config_set_text(&c, "name", name) && strlen(c.name) == 64);
    name[64] = 'a';
    name[65] = 0;
    assert(!strcmp(box_config_set_text(&c, "name", name), too_long));
    for (int i = 0; i < 64; ++i) memcpy(name + 3 * i, "\xe2\x82\xac", 3); /* 64 euro signs */
    name[192] = 0;
    assert(!box_config_set_text(&c, "name", name) && strlen(c.name) == 192);
    assert(!strcmp(box_config_set_text(&c, "name", "bad \xc3"), too_long)); /* cut UTF-8 */
    memset(name, 'a', sizeof(name) - 1);
    name[sizeof(name) - 1] = 0;
    assert(!strcmp(box_config_set_text(&c, "name", name), too_long));
    assert(!box_config_set_text(&c, "node_id", "") && !box_config_bridging(&c));
    return 0;
}

static int mode_media(void)
{
    char media[96];
    size_t len = box_media_producer(media, sizeof(media), 0xc0a81433u);
    assert(len && !strcmp(media, "{\"busids\":[\"1-1\"],\"host\":\"192.168.20.51\",\"port\":3240}"));
    assert(!box_media_producer(media, 20, 0xc0a81433u));
    box_target_t t;
    assert(!box_media_parse(media, len, &t));
    assert(t.ipv4 == 0xc0a81433u && t.port == 3240 && !strcmp(t.busid, "1-1"));
    static const struct { const char *media, *why; } bad[] = {
        {"[]", "media-not-a-json-object"},
        {"{\"host\":\"usb-01.local\",\"port\":3240,\"busids\":[\"1-1\"]}", "media-host-must-be-ipv4"},
        {"{\"host\":\"10.0.0.01\",\"port\":3240,\"busids\":[\"1-1\"]}", "media-host-must-be-ipv4"},
        {"{\"host\":\"10.0.0.1\",\"port\":0,\"busids\":[\"1-1\"]}", "media-port-invalid"},
        {"{\"host\":\"10.0.0.1\",\"port\":3240.0,\"busids\":[\"1-1\"]}", "media-port-invalid"},
        {"{\"host\":\"10.0.0.1\",\"port\":3240,\"busids\":[]}", "media-busids-invalid"},
        {"{\"host\":\"10.0.0.1\",\"port\":3240,\"busids\":[\"usb1\"]}", "media-busids-invalid"},
        {"{\"port\":3240,\"busids\":[\"1-1\"]}", "media-host-missing"},
    };
    for (size_t i = 0; i < sizeof(bad) / sizeof(bad[0]); ++i) {
        const char *why = box_media_parse(bad[i].media, strlen(bad[i].media), &t);
        if (!why || strcmp(why, bad[i].why)) {
            fprintf(stderr, "%s: %s\n", bad[i].media, why ? why : "accepted");
            assert(0);
        }
    }
    /* A second busid is ignored: the box imports one device. */
    const char *two = "{\"busids\":[\"1-1.2\",\"1-1\"],\"host\":\"10.0.0.1\",\"port\":3241}";
    assert(!box_media_parse(two, strlen(two), &t) && !strcmp(t.busid, "1-1.2") && t.port == 3241);
    return 0;
}

/* A core that echoes what it receives, through bounded buffers. */
static struct {
    uint8_t in[100], out[100];
    size_t out_len, sent;
    int refuse_after;
} core;

static size_t rx_space(void *c, uint8_t **buf)
{
    (void)c;
    *buf = core.in;
    return core.out_len ? 0 : sizeof(core.in);  /* backpressure while echoing */
}
static int rx_done(void *c, size_t n, uint64_t now)
{
    (void)c; (void)now;
    if (core.refuse_after && --core.refuse_after == 0) return -1;
    memcpy(core.out, core.in, n);
    core.out_len = n;
    core.sent = 0;
    return 0;
}
static size_t tx_iov(const void *c, yusb_iov_t *iov, size_t max)
{
    (void)c;
    if (!max || core.sent == core.out_len) return 0;
    size_t half = (core.out_len - core.sent) / 2;
    iov[0] = (yusb_iov_t){core.out + core.sent, half};
    if (max < 2 || !half) {
        iov[0].len = core.out_len - core.sent;
        return 1;
    }
    iov[1] = (yusb_iov_t){core.out + core.sent + half, core.out_len - core.sent - half};
    return 2;
}
static void tx_done(void *c, size_t n)
{
    (void)c;
    core.sent += n;
    if (core.sent == core.out_len) core.out_len = core.sent = 0;
}
static const box_core_io_t io = {rx_space, rx_done, tx_iov, tx_done};

static void pair(int fds[2])
{
    int listener = socket(AF_INET, SOCK_STREAM, 0);
    struct sockaddr_in at = {.sin_family = AF_INET, .sin_addr.s_addr = htonl(INADDR_LOOPBACK)};
    socklen_t size = sizeof(at);
    assert(!bind(listener, (struct sockaddr *)&at, size) && !listen(listener, 1));
    assert(!getsockname(listener, (struct sockaddr *)&at, &size));
    fds[0] = socket(AF_INET, SOCK_STREAM, 0);
    assert(!connect(fds[0], (struct sockaddr *)&at, size));
    fds[1] = accept(listener, NULL, NULL);
    close(listener);
    assert(!box_pump_prepare(fds[1]));
    int nodelay = 0;
    socklen_t len = sizeof(nodelay);
    assert(!getsockopt(fds[1], IPPROTO_TCP, 1 /* TCP_NODELAY */, &nodelay, &len) && nodelay);
    assert(fcntl(fds[1], F_GETFL) & O_NONBLOCK);
}

static int mode_pump(void)
{
    int fds[2];
    pair(fds);
    memset(&core, 0, sizeof(core));
    assert(box_pump(fds[1], &io, NULL, 0) == BOX_PUMP_READ);   /* nothing yet */
    assert(write(fds[0], "hello", 5) == 5);
    usleep(10000);
    assert(box_pump(fds[1], &io, NULL, 0) == BOX_PUMP_READ);   /* echoed in two segments */
    char back[8] = {0};
    assert(read(fds[0], back, sizeof(back)) == 5 && !strcmp(back, "hello"));
    /* The core refuses bytes: a protocol failure. */
    core.refuse_after = 1;
    assert(write(fds[0], "x", 1) == 1);
    usleep(10000);
    assert(box_pump(fds[1], &io, NULL, 0) == BOX_PUMP_FAILED);
    /* The peer closes. */
    memset(&core, 0, sizeof(core));
    close(fds[0]);
    usleep(10000);
    assert(box_pump(fds[1], &io, NULL, 0) == BOX_PUMP_CLOSED);
    close(fds[1]);
    return 0;
}

static int mode_pump_backpressure(void)
{
    int fds[2];
    pair(fds);
    memset(&core, 0, sizeof(core));
    int small = 2048;
    assert(fcntl(fds[0], F_SETFL, O_NONBLOCK) == 0);
    assert(!setsockopt(fds[0], SOL_SOCKET, SO_RCVBUF, &small, sizeof(small)));
    assert(!setsockopt(fds[1], SOL_SOCKET, SO_SNDBUF, &small, sizeof(small)));
    /* The peer never reads: echoing stalls with bytes pending (WRITE), and
     * the core stops taking input meanwhile: its readable socket is not to
     * be watched (no READ), or the watch would spin. */
    static char blob[100];
    int r = BOX_PUMP_READ;
    for (int i = 0; i < 20000 && !(r & BOX_PUMP_WRITE); ++i) {
        if (write(fds[0], blob, sizeof(blob)) < 0) assert(errno == EAGAIN);
        r = box_pump(fds[1], &io, NULL, 0);
    }
    assert(r == BOX_PUMP_WRITE && core.out_len);
    close(fds[0]);
    close(fds[1]);
    return 0;
}

/* A source that keeps failing authentication is ignored until its bucket
 * refills; others are not; a new source replaces the most forgiven. */
static int mode_auth_limit(void)
{
    static box_limit_t limit;
    const uint32_t forger = 0x0a000005u, peer = 0x0a000006u;
    double t = 100.0;
    for (int i = 0; i < (int)BOX_LIMIT_BURST; ++i) {
        assert(box_limit_allow(&limit, forger, t));
        box_limit_failed(&limit, forger, t);
    }
    assert(!box_limit_allow(&limit, forger, t) && limit.throttled == 1);
    assert(box_limit_allow(&limit, peer, t));             /* never failed */
    assert(!box_limit_allow(&limit, forger, t + 0.5));    /* half a token */
    assert(box_limit_allow(&limit, forger, t + 1.0));     /* one more try a second */
    box_limit_failed(&limit, forger, t + 1.0);
    assert(!box_limit_allow(&limit, forger, t + 1.0));
    /* Eight other failing sources fill the table; the forger, still
     * throttled, keeps its entry while the most forgiven one is replaced. */
    for (uint32_t s = 1; s <= BOX_LIMIT_SOURCES; ++s) box_limit_failed(&limit, 0x0b000000u + s, t + 1.0);
    assert(!box_limit_allow(&limit, forger, t + 1.0));
    assert(box_limit_allow(&limit, forger, t + 1.0 + 1.0 / BOX_LIMIT_RATE));
    return 0;
}

/* The admin step is forward only and at most a year unless forced; a kept
 * reading survives only resets that keep RTC memory, and only if plausible
 * against the restored floor. */
static int mode_clock_rules(void)
{
    const int64_t now = 1800000000;
    assert(box_time_step(now, now - 1, true) == BOX_TIME_BACKWARDS);
    assert(box_time_step(now, now + BOX_TIME_STEP_MAX, false) == BOX_TIME_OK);
    assert(box_time_step(now, now + BOX_TIME_STEP_MAX + 1, false) == BOX_TIME_TOO_FAR);
    assert(box_time_step(now, now + 10 * BOX_TIME_STEP_MAX, true) == BOX_TIME_OK);
    box_time_kept_t kept;
    memset(&kept, 0xa5, sizeof(kept));             /* RTC memory after power-on */
    assert(box_time_restore(&kept, true, now) == -1);
    box_time_keep(&kept, now + 1800);              /* watchdog reset: floor an hour old */
    assert(box_time_restore(&kept, false, now) == -1); /* power-on: never trusted */
    box_time_keep(&kept, now + 1800);
    assert(box_time_restore(&kept, true, now) == now + 1800);
    assert(box_time_restore(&kept, true, now) == -1); /* consumed */
    box_time_keep(&kept, now - 1);                 /* planned restart: floor saved after */
    assert(box_time_restore(&kept, true, now) == now);
    box_time_keep(&kept, now + BOX_TIME_KEPT_AHEAD + 1);
    assert(box_time_restore(&kept, true, now) == -1);
    box_time_keep(&kept, now - BOX_TIME_KEPT_BEHIND - 1);
    assert(box_time_restore(&kept, true, now) == -1);
    box_time_keep(&kept, now);
    kept.unix_s += 1;                              /* corrupt */
    assert(box_time_restore(&kept, true, now) == -1);
    return 0;
}

int main(int argc, char **argv)
{
    assert(argc == 2);
    if (!strcmp(argv[1], "clock-rules")) return mode_clock_rules();
    if (!strcmp(argv[1], "auth-limit")) return mode_auth_limit();
    if (!strcmp(argv[1], "config")) return mode_config();
    if (!strcmp(argv[1], "media")) return mode_media();
    if (!strcmp(argv[1], "pump")) return mode_pump();
    if (!strcmp(argv[1], "pump-backpressure")) return mode_pump_backpressure();
    return 2;
}
