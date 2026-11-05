/* A POSIX host shell for the native YWP/1 node, for interop tests only.
 *
 * Real UDP multicast on one interface (bound to the group address, loopback
 * on, TTL 1, as yoke.net.mcast), wall and monotonic clocks, a durable state
 * blob (ywp_state.h) written atomically, and a simulated driver behind the
 * generation-tagged route queue (ywp_route.h). It prints one JSON line on
 * stdout whenever the node's state or pairing changes, and stops with
 * stopping/bye on SIGTERM. HOST SIMULATION: no USB, no ESP-IDF.
 *
 * host_node --id ID --role producer|consumer --kind usb|hdmi --group G
 *           --port P --interface ADDR (--psk-file F | --open) --state FILE
 *           [--commission] [--media JSON]
 */
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <netinet/in.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>
#include "ywp.h"
#include "ywp_route.h"
#include "ywp_state.h"

static volatile sig_atomic_t stop;
static int sock;
static struct sockaddr_in group_addr;
static const char *state_path;
static ywp_route_t route;
static char sim_attached[YWP_ID_MAX + 1];      /* simulated driver: attached producer */

static void on_signal(int sig)
{
    (void)sig;
    stop = 1;
}

static double mono(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

static double wall(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

static bool save_state(const ywp_saved_t *s)
{
    uint8_t blob[YWP_SAVED_MAX];
    size_t len = ywp_saved_encode(s, blob);
    char tmp[4096];
    snprintf(tmp, sizeof(tmp), "%s.tmp", state_path);
    int fd = open(tmp, O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (fd < 0 || !len) return false;
    bool ok = write(fd, blob, len) == (ssize_t)len && fsync(fd) == 0;
    close(fd);
    return ok && rename(tmp, state_path) == 0;
}

static void driver_status(ywp_node_t *node, ywp_state_t st, const char *detail);

static void perform(ywp_node_t *node)
{
    for (;;) {
        const ywp_effect_t *fx;
        size_t n = ywp_node_effects(node, &fx);
        bool again = false;
        for (size_t i = 0; i < n && !again; ++i) {
            uint8_t raw[YWP_MAX_FRAME];
            size_t len;
            switch (fx[i].kind) {
            case YWP_FX_SEND:
                len = ywp_node_encode(node, &fx[i], wall(), raw);
                if (len) sendto(sock, raw, len, 0, (struct sockaddr *)&group_addr, sizeof(group_addr));
                break;
            case YWP_FX_PERSIST:
                ywp_node_persisted(node, fx[i].persist.request_id, save_state(fx[i].persist.state),
                                   mono());
                again = true;             /* PERSIST is last; continue with its batch */
                break;
            case YWP_FX_ATTACH:
            case YWP_FX_DETACH:
                ywp_route_submit(&route, &fx[i]);
                break;
            case YWP_FX_ADMISSION:
                printf("{\"admission\":%zu}\n", fx[i].admission.count);
                break;
            case YWP_FX_LOG:
                fprintf(stderr, "ywp: %s\n", fx[i].log.text);
                break;
            }
        }
        if (!again) break;
    }
    /* Run queued driver work on the simulated device. */
    ywp_route_cmd_t cmd;
    while (ywp_route_next(&route, &cmd)) {
        ywp_state_t st = cmd.detach ? YWP_IDLE : YWP_ACTIVE;
        const char *detail = cmd.detach ? "" : "simulated import";
        size_t len = strlen(detail);
        if (cmd.detach) sim_attached[0] = 0;
        else snprintf(sim_attached, sizeof(sim_attached), "%s", cmd.producer);
        if (ywp_route_done(&route, true, &st, &detail, &len)) driver_status(node, st, detail);
    }
}

static void driver_status(ywp_node_t *node, ywp_state_t st, const char *detail)
{
    ywp_node_driver_status(node, st, detail, strlen(detail), mono());
    perform(node);
}

static void report(ywp_node_t *node)
{
    static char last[256];
    char line[256];
    ywp_status_t s;
    ywp_node_status(node, &s);
    snprintf(line, sizeof(line),
             "{\"state\":\"%s\",\"producer\":%s%s%s,\"version\":[%llu,\"%s\"],\"attached\":\"%s\"}",
             ywp_state_name(s.state), s.pairing.paired ? "\"" : "",
             s.pairing.paired ? s.pairing.producer : "null", s.pairing.paired ? "\"" : "",
             (unsigned long long)s.pairing.version.n, s.pairing.version.origin, sim_attached);
    if (strcmp(line, last) != 0) {
        strcpy(last, line);
        puts(line);
        fflush(stdout);
    }
}

static int open_socket(const char *group, int port, const char *iface)
{
    int fd = socket(AF_INET, SOCK_DGRAM, 0), one = 1, zero = 0;
    unsigned char ttl = 1, loop = 1;
    struct ip_mreq mreq;
    struct in_addr ifaddr;
    memset(&group_addr, 0, sizeof(group_addr));
    group_addr.sin_family = AF_INET;
    group_addr.sin_port = htons((uint16_t)port);
    inet_pton(AF_INET, group, &group_addr.sin_addr);
    inet_pton(AF_INET, iface, &ifaddr);
    mreq.imr_multiaddr = group_addr.sin_addr;
    mreq.imr_interface = ifaddr;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    setsockopt(fd, SOL_SOCKET, SO_REUSEPORT, &one, sizeof(one));
    setsockopt(fd, IPPROTO_IP, 49 /* IP_MULTICAST_ALL */, &zero, sizeof(zero));
    if (bind(fd, (struct sockaddr *)&group_addr, sizeof(group_addr)) < 0 ||
        setsockopt(fd, IPPROTO_IP, IP_ADD_MEMBERSHIP, &mreq, sizeof(mreq)) < 0 ||
        setsockopt(fd, IPPROTO_IP, IP_MULTICAST_IF, &ifaddr, sizeof(ifaddr)) < 0 ||
        setsockopt(fd, IPPROTO_IP, IP_MULTICAST_TTL, &ttl, sizeof(ttl)) < 0 ||
        setsockopt(fd, IPPROTO_IP, IP_MULTICAST_LOOP, &loop, sizeof(loop)) < 0) {
        perror("multicast socket");
        exit(3);
    }
    return fd;
}

static const char *arg(int argc, char **argv, const char *name)
{
    for (int i = 1; i + 1 < argc; ++i)
        if (strcmp(argv[i], name) == 0) return argv[i + 1];
    return NULL;
}

static bool flag(int argc, char **argv, const char *name)
{
    for (int i = 1; i < argc; ++i)
        if (strcmp(argv[i], name) == 0) return true;
    return false;
}

int main(int argc, char **argv)
{
    ywp_config_t cfg;
    ywp_config_default(&cfg);
    cfg.node_id = arg(argc, argv, "--id");
    cfg.name = cfg.node_id;
    cfg.role = strcmp(arg(argc, argv, "--role"), "producer") == 0 ? YWP_PRODUCER : YWP_CONSUMER;
    cfg.kind = strcmp(arg(argc, argv, "--kind"), "hdmi") == 0 ? YWP_HDMI : YWP_USB;
    cfg.media = arg(argc, argv, "--media");
    cfg.media_len = cfg.media ? strlen(cfg.media) : 0;
    cfg.seed = (uint64_t)getpid();
    state_path = arg(argc, argv, "--state");

    static uint8_t psk[4096];
    const char *psk_file = arg(argc, argv, "--psk-file");
    if (psk_file) {
        FILE *f = fopen(psk_file, "rb");
        size_t n = f ? fread(psk, 1, sizeof(psk), f) : 0;
        if (f) fclose(f);
        if (n && psk[n - 1] == '\n') --n;        /* one trailing newline, as read_psk */
        if (n < 32) { fprintf(stderr, "PSK must be at least 32 bytes\n"); return 2; }
        cfg.psk = psk;
        cfg.psk_len = n;
    } else if (!flag(argc, argv, "--open")) {
        fprintf(stderr, "--psk-file or explicit --open required\n");
        return 2;
    }

    ywp_saved_t saved;
    memset(&saved, 0, sizeof(saved));
    uint8_t blob[YWP_SAVED_MAX + 1];
    FILE *f = fopen(state_path, "rb");
    if (f) {
        size_t n = fread(blob, 1, sizeof(blob), f);
        fclose(f);
        if (!ywp_saved_decode(blob, n, &saved)) { fprintf(stderr, "corrupt state\n"); return 4; }
    } else if (!flag(argc, argv, "--commission")) {
        fprintf(stderr, "missing state requires --commission\n");
        return 4;
    }

    ywp_err_t err;
    ywp_node_t *node = ywp_node_create(&cfg, &ywp_crypto_portable, &saved, mono(), wall(), &err);
    if (!node) { fprintf(stderr, "create: %s\n", ywp_err_name(err)); return 2; }
    sock = open_socket(arg(argc, argv, "--group"), atoi(arg(argc, argv, "--port")),
                       arg(argc, argv, "--interface"));
    signal(SIGTERM, on_signal);
    signal(SIGINT, on_signal);
    ywp_route_init(&route);
    ywp_node_start(node, mono());
    perform(node);
    if (cfg.role == YWP_PRODUCER) driver_status(node, YWP_ACTIVE, "simulated export");
    double next_health = mono() + 1.0;
    while (!stop) {
        report(node);
        double wait = ywp_node_next_deadline(node) - mono();
        if (next_health - mono() < wait) wait = next_health - mono();
        struct pollfd pfd = {.fd = sock, .events = POLLIN};
        int ready = poll(&pfd, 1, wait > 0 ? (int)(wait * 1000) + 1 : 0);
        if (ready > 0) {
            uint8_t raw[2048];
            struct sockaddr_in from;
            socklen_t fromlen = sizeof(from);
            ssize_t n = recvfrom(sock, raw, sizeof(raw), 0, (struct sockaddr *)&from, &fromlen);
            if (n >= 0) {
                uint32_t source = ntohl(from.sin_addr.s_addr);
                ywp_node_datagram(node, raw, (size_t)n, mono(), wall(), &source);
                perform(node);
            }
        }
        if (mono() >= ywp_node_next_deadline(node)) {
            ywp_node_poll(node, mono(), wall());
            perform(node);
        }
        if (mono() >= next_health) {               /* serialized health poll */
            next_health = mono() + 1.0;
            uint32_t tag = ywp_route_poll_tag(&route);
            bool producer = cfg.role == YWP_PRODUCER;
            ywp_state_t st = producer || sim_attached[0] ? YWP_ACTIVE : YWP_IDLE;
            if (ywp_route_poll_current(&route, tag))
                driver_status(node, st, producer ? "simulated export" : sim_attached[0] ? "simulated import" : "");
        }
    }
    ywp_route_close(&route);
    ywp_node_shutdown(node, mono());
    perform(node);
    report(node);
    ywp_node_destroy(node);
    close(sock);
    return 0;
}
