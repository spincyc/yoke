/* yusb-export-sim: the device-side export core serving a simulated BOT
 * disk over real TCP (host tests, the vhci-hcd real-kernel test and the
 * YX benchmarks). Single-threaded poll loop; the disk is a shared file
 * mapping, so a test can check what landed in it. Prints the bound port.
 *
 *   yusb-export-sim --disk FILE [--disk-mb N] [--host IP] [--port N]
 *                   [--busid 1-1] [--yx fill,lz4,cache] [--yx-cache N]
 *                   [--max-urbs N] [--max-urb-bytes N] [--pool-bytes N] [--once]
 *
 * Single-threaded: every export call, the sends included, happens on the
 * poll thread, so no export state is read without the (implicit) lock.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <arpa/inet.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/uio.h>
#include <time.h>
#include <unistd.h>
#include "simdev.h"

#define CONNS 4

typedef struct {
    int fd;
    yusb_export_t *x;
    bool imported;
} conn_t;

static volatile sig_atomic_t done;
static void on_term(int sig) { (void)sig; done = 1; }

static uint64_t now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000u + (uint64_t)ts.tv_nsec / 1000000u;
}

static void log_line(void *ctx, const char *message)
{
    (void)ctx;
    fprintf(stderr, "yusb-export-sim: %s\n", message);
}

static int parse_levers(const char *text, uint8_t *levers)
{
    char copy[64];
    snprintf(copy, sizeof(copy), "%s", text);
    *levers = 0;
    for (char *save = NULL, *t = strtok_r(copy, ",", &save); t; t = strtok_r(NULL, ",", &save)) {
        if (!strcmp(t, "fill")) *levers |= YUSB_YX_FILL;
        else if (!strcmp(t, "lz4")) *levers |= YUSB_YX_LZ4;
        else if (!strcmp(t, "cache")) *levers |= YUSB_YX_CACHE;
        else if (strcmp(t, "none")) return -1;
    }
    return 0;
}

static void drop(conn_t *c, simdev_t *dev)
{
    yusb_export_close(c->x);
    simdev_drain(dev, c->x, now_ms());
    yusb_export_destroy(c->x);
    close(c->fd);
    c->fd = -1;
    c->x = NULL;
}

/* Send pending replies; false when the peer is gone. */
static bool flush(conn_t *c, simdev_t *dev)
{
    for (;;) {
        yusb_iov_t seg[64];
        struct iovec iov[64];
        size_t n = yusb_export_tx_iov(c->x, seg, 64);
        if (!n) return true;
        for (size_t i = 0; i < n; ++i) {
            iov[i].iov_base = (void *)seg[i].base;
            iov[i].iov_len = seg[i].len;
        }
        struct msghdr msg = {.msg_iov = iov, .msg_iovlen = n};
        ssize_t w = sendmsg(c->fd, &msg, MSG_NOSIGNAL | MSG_DONTWAIT);
        if (w < 0 && (errno == EAGAIN || errno == EINTR)) return true;
        if (w <= 0) return false;
        yusb_export_tx_done(c->x, (size_t)w);
        simdev_drain(dev, c->x, now_ms());
    }
}

int main(int argc, char **argv)
{
    static const struct option options[] = {
        {"disk", required_argument, 0, 'd'}, {"disk-mb", required_argument, 0, 'm'},
        {"host", required_argument, 0, 'H'}, {"port", required_argument, 0, 'p'},
        {"busid", required_argument, 0, 'b'}, {"yx", required_argument, 0, 'y'},
        {"yx-cache", required_argument, 0, 'c'}, {"max-urbs", required_argument, 0, 'u'},
        {"max-urb-bytes", required_argument, 0, 'B'}, {"pool-bytes", required_argument, 0, 'P'},
        {"once", no_argument, 0, 'o'}, {0, 0, 0, 0},
    };
    const char *disk_path = NULL, *host = "127.0.0.1", *busid = "1-1";
    long disk_mb = 0, port = 0;
    bool once = false;
    yusb_export_cfg_t cfg;
    yusb_export_defaults(&cfg);
    cfg.yx.version = YUSB_YX_VERSION;
    int opt;
    while ((opt = getopt_long(argc, argv, "", options, NULL)) != -1) {
        switch (opt) {
        case 'd': disk_path = optarg; break;
        case 'm': disk_mb = strtol(optarg, NULL, 10); break;
        case 'H': host = optarg; break;
        case 'p': port = strtol(optarg, NULL, 10); break;
        case 'b': busid = optarg; break;
        case 'y': if (parse_levers(optarg, &cfg.yx.levers)) return 2; break;
        case 'c': cfg.yx.cache_blocks = (uint16_t)strtol(optarg, NULL, 10); break;
        case 'u': cfg.max_urbs = (uint16_t)strtol(optarg, NULL, 10); break;
        case 'B': cfg.max_urb_bytes = (uint32_t)strtoul(optarg, NULL, 10); break;
        case 'P': cfg.pool_bytes = (size_t)strtoull(optarg, NULL, 10); break;
        case 'o': once = true; break;
        default: return 2;
        }
    }
    if (!disk_path) {
        fprintf(stderr, "yusb-export-sim: --disk FILE is required\n");
        return 2;
    }
    if (!yusb_export_footprint(&cfg, NULL)) {
        fprintf(stderr, "yusb-export-sim: invalid --max-urbs or --max-urb-bytes\n");
        return 2;
    }
    int dfd = open(disk_path, O_RDWR | O_CREAT | O_CLOEXEC, 0644);
    struct stat st;
    if (dfd < 0 || fstat(dfd, &st) || (disk_mb && ftruncate(dfd, disk_mb << 20))) {
        perror("yusb-export-sim: disk");
        return 1;
    }
    size_t size = disk_mb ? (size_t)disk_mb << 20 : (size_t)st.st_size;
    if (size < 512 * 64) {
        fprintf(stderr, "yusb-export-sim: disk too small\n");
        return 1;
    }
    uint8_t *disk = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, dfd, 0);
    simdev_t *dev = disk == MAP_FAILED ? NULL : simdev_create(disk, size);
    if (!dev) {
        perror("yusb-export-sim: mmap");
        return 1;
    }
    yusb_hc_ops_t ops = *simdev_ops(dev);
    ops.log = log_line;
    yusb_export_dev_t desc;
    simdev_describe(dev, &desc, busid);
    desc.info.configuration_value = 1;
    int lfd = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0), one = 1;
    struct sockaddr_in addr = {.sin_family = AF_INET, .sin_port = htons((uint16_t)port)};
    inet_pton(AF_INET, host, &addr.sin_addr);
    setsockopt(lfd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    socklen_t alen = sizeof(addr);
    if (bind(lfd, (struct sockaddr *)&addr, sizeof(addr)) || listen(lfd, 8) ||
        getsockname(lfd, (struct sockaddr *)&addr, &alen)) {
        perror("yusb-export-sim: listen");
        return 1;
    }
    fprintf(stderr, "yusb-export-sim: %zu bytes per connection\n", yusb_export_footprint(&cfg, ops.buffers));
    printf("%u\n", ntohs(addr.sin_port));
    fflush(stdout);
    signal(SIGTERM, on_term);
    signal(SIGINT, on_term);
    signal(SIGPIPE, SIG_IGN);
    conn_t conns[CONNS];
    for (int i = 0; i < CONNS; ++i) conns[i].fd = -1;
    bool served = false;
    while (!done && !(once && served)) {
        struct pollfd pfd[CONNS + 1];
        pfd[0] = (struct pollfd){lfd, POLLIN, 0};
        for (int i = 0; i < CONNS; ++i) {
            uint8_t *unused;
            short ev = 0;
            if (conns[i].fd >= 0) {
                if (yusb_export_rx_space(conns[i].x, &unused)) ev |= POLLIN;
                yusb_iov_t seg;
                if (yusb_export_tx_iov(conns[i].x, &seg, 1)) ev |= POLLOUT;
            }
            pfd[i + 1] = (struct pollfd){conns[i].fd, ev, 0};
        }
        if (poll(pfd, CONNS + 1, 200) < 0) continue;
        if (pfd[0].revents & POLLIN) {
            int fd = accept4(lfd, NULL, NULL, SOCK_CLOEXEC);
            int slot = -1;
            for (int i = 0; i < CONNS && fd >= 0; ++i) if (conns[i].fd < 0) { slot = i; break; }
            if (slot < 0) {
                if (fd >= 0) close(fd);
            } else {
                setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));  /* as usbipd */
                conns[slot] = (conn_t){fd, yusb_export_create(&cfg, &ops, &desc, 1), false};
                if (!conns[slot].x) { close(fd); conns[slot].fd = -1; }
            }
        }
        for (int i = 0; i < CONNS; ++i) {
            conn_t *c = &conns[i];
            if (c->fd < 0) continue;
            bool alive = true;
            if (pfd[i + 1].revents & (POLLIN | POLLHUP | POLLERR)) {
                uint8_t *buf;
                size_t n = yusb_export_rx_space(c->x, &buf);
                ssize_t r = n ? recv(c->fd, buf, n, MSG_DONTWAIT) : 0;
                if (r > 0) {
                    if (yusb_export_rx_done(c->x, (size_t)r, now_ms()) < 0) alive = false;
                    simdev_drain(dev, c->x, now_ms());
                } else if (n && (r == 0 || (errno != EAGAIN && errno != EINTR))) {
                    alive = false;
                }
            }
            if (yusb_export_device(c->x) >= 0) c->imported = true;
            if (alive) alive = flush(c, dev);
            yusb_iov_t seg;
            if (alive && yusb_export_finished(c->x) && !yusb_export_tx_iov(c->x, &seg, 1)) alive = false;
            if (!alive) {
                if (c->imported) served = true;
                drop(c, dev);
            }
        }
    }
    for (int i = 0; i < CONNS; ++i) if (conns[i].fd >= 0) drop(&conns[i], dev);
    msync(disk, size, MS_SYNC);
    simdev_destroy(dev);
    return 0;
}
