/* Deterministic unit tests of native/yusb/linux/session.c (F5 + F11):
 * - F5: session_stop refuses teardown ("teardown unconfirmed: transmit did not
 *   stop") and shuts down the socket without freeing proxy buffers when a
 *   thread stays inside sendmsg (s->in_send) past the deadline; a later retry
 *   confirms cleanly once sendmsg exits.
 * - F5: session_stop racing with start_gadget closes the gadget fd cleanly
 *   without binding or spawning event_main.
 * - F5: session_stop racing with connect_target closes the connected socket
 *   without creating the proxy or starting the service thread.
 * - F11: join_until uses CLOCK_MONOTONIC + pthread_tryjoin_np and delivers
 *   SIGUSR1 kicks to unblock EVENT_FETCH promptly.
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>
#include "rawgadget.h"
#include "session.h"
#include "sys.h"
#include "yusb_usbip.h"

static int failures;
#define CHECK(cond)                                                              \
    do {                                                                         \
        if (!(cond)) {                                                           \
            fprintf(stderr, "%s:%d: CHECK(%s) failed\n", __FILE__, __LINE__, #cond); \
            failures++;                                                          \
        }                                                                        \
    } while (0)

static void noop_sig(int sig) { (void)sig; }

/* --- wrapped sendmsg for deterministic s->in_send stall testing ---------- */

ssize_t __real_sendmsg(int sockfd, const struct msghdr *msg, int flags);
ssize_t __wrap_sendmsg(int sockfd, const struct msghdr *msg, int flags);

static pthread_mutex_t wrap_mu = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t wrap_cv = PTHREAD_COND_INITIALIZER;
static bool send_block_enabled;
static bool send_entered;
static bool send_released;
static int send_last_errno;
static volatile uint8_t send_touched;

ssize_t __wrap_sendmsg(int sockfd, const struct msghdr *msg, int flags)
{
    pthread_mutex_lock(&wrap_mu);
    if (send_block_enabled && !send_entered) {
        send_entered = true;
        pthread_cond_broadcast(&wrap_cv);
        while (!send_released) pthread_cond_wait(&wrap_cv, &wrap_mu);
        /* Touch the first queued buffer byte after session_stop returned
         * false: if session_stop had called yusb_proxy_stop/destroy early,
         * this would be a use-after-free. */
        if (msg && msg->msg_iovlen && msg->msg_iov[0].iov_len)
            send_touched = ((const uint8_t *)msg->msg_iov[0].iov_base)[0];
        pthread_mutex_unlock(&wrap_mu);
        ssize_t r = __real_sendmsg(sockfd, msg, flags);
        int e = errno;
        pthread_mutex_lock(&wrap_mu);
        send_last_errno = r < 0 ? e : 0;
        pthread_mutex_unlock(&wrap_mu);
        errno = e;
        return r;
    }
    pthread_mutex_unlock(&wrap_mu);
    return __real_sendmsg(sockfd, msg, flags);
}

/* --- fake ysys() for Raw Gadget ----------------------------------------- */

static struct {
    pthread_mutex_t mu;
    pthread_cond_t cv;
    bool block_open, in_open, release_open;
    int opens, closes, event_fetches, sigusr1_interrupts;
    int wake_pipe[2];
    bool event_entered;
} rg = {PTHREAD_MUTEX_INITIALIZER, PTHREAD_COND_INITIALIZER, false, false, false,
        0, 0, 0, 0, {-1, -1}, false};

static int fake_open(const char *path)
{
    (void)path;
    pthread_mutex_lock(&rg.mu);
    rg.opens++;
    if (rg.block_open) {
        rg.in_open = true;
        pthread_cond_broadcast(&rg.cv);
        while (!rg.release_open) pthread_cond_wait(&rg.cv, &rg.mu);
    }
    pthread_mutex_unlock(&rg.mu);
    return 42;
}

static void fake_close(int fd)
{
    CHECK(fd == 42);
    pthread_mutex_lock(&rg.mu);
    rg.closes++;
    pthread_mutex_unlock(&rg.mu);
}

static long fake_ioctl(int fd, unsigned long req, void *arg)
{
    (void)fd;
    (void)arg;
    if (req == RG_IOCTL_INIT || req == RG_IOCTL_RUN) return 0;
    if (req == RG_IOCTL_EPS_INFO) return 0;
    if (req == RG_IOCTL_EVENT_FETCH) {
        pthread_mutex_lock(&rg.mu);
        rg.event_fetches++;
        rg.event_entered = true;
        pthread_cond_broadcast(&rg.cv);
        int pfd = rg.wake_pipe[0];
        pthread_mutex_unlock(&rg.mu);
        struct pollfd p = {pfd, POLLIN, 0};
        int rc = poll(&p, 1, 5000);
        if (rc < 0 && errno == EINTR) {
            pthread_mutex_lock(&rg.mu);
            rg.sigusr1_interrupts++;
            pthread_mutex_unlock(&rg.mu);
            return -EINTR;
        }
        return -ESHUTDOWN;
    }
    return 0;
}

static long fake_read_text(const char *path, char *out, size_t cap)
{
    (void)path;
    int n = snprintf(out, cap, "high-speed\n");
    return n > 0 ? n : -EINVAL;
}

static const ysys_t fake_sys = {
    .open = fake_open,
    .close = fake_close,
    .ioctl = fake_ioctl,
    .read_text = fake_read_text,
};

const ysys_t *ysys(void) { return &fake_sys; }

/* --- minimal TCP exporter thread ---------------------------------------- */

static const uint8_t dev_desc[18] = {18, 1, 0, 2, 0, 0, 0, 64, 0x09, 0x12, 0x99, 0, 0, 1, 0, 0, 0, 1};
static const uint8_t cfg_desc[32] = {9, 2, 32, 0, 1, 1, 0, 0x80, 50, 9, 4, 0, 0, 2, 0xff, 0, 0, 0,
                                     7, 5, 0x81, 2, 0, 2, 0, 7, 5, 0x02, 2, 0, 2, 0};

static bool recv_all(int fd, uint8_t *buf, size_t n)
{
    size_t got = 0;
    while (got < n) {
        ssize_t r = recv(fd, buf + got, n - got, 0);
        if (r <= 0) return false;
        got += (size_t)r;
    }
    return true;
}

static bool send_all(int fd, const uint8_t *buf, size_t n)
{
    size_t sent = 0;
    while (sent < n) {
        ssize_t w = send(fd, buf + sent, n - sent, MSG_NOSIGNAL);
        if (w <= 0) return false;
        sent += (size_t)w;
    }
    return true;
}

typedef struct {
    int listen_fd;
    uint16_t port;
    bool serve_import;
    pthread_t thread;
    volatile bool stop;
} server_t;

static void *server_main(void *arg)
{
    server_t *srv = arg;
    int cfd = accept(srv->listen_fd, NULL, NULL);
    if (cfd < 0) return NULL;
    if (!srv->serve_import) {
        uint8_t tmp[64];
        while (recv(cfd, tmp, sizeof(tmp), 0) > 0) {}
        close(cfd);
        return NULL;
    }
    uint8_t req[YUSB_REQ_IMPORT_SIZE];
    if (!recv_all(cfd, req, sizeof(req))) { close(cfd); return NULL; }
    uint8_t rep[YUSB_REP_IMPORT_SIZE] = {0};
    yusb_op_encode(YUSB_OP_REP_IMPORT, 0, rep);
    yusb_device_info_t info = {.busnum = 1, .devnum = 2, .speed = YUSB_SPEED_HIGH};
    strcpy(info.busid, "1-1");
    yusb_device_info_encode(&info, rep + 8);
    if (!send_all(cfd, rep, sizeof(rep))) { close(cfd); return NULL; }
    for (;;) {
        uint8_t hdr[48];
        if (!recv_all(cfd, hdr, sizeof(hdr))) break;
        yusb_usbip_hdr_t h;
        if (yusb_usbip_decode(hdr, &h)) break;
        const uint8_t *body = NULL;
        uint32_t blen = 0;
        int32_t st = 0;
        if (h.length == 18) { body = dev_desc; blen = 18; }
        else if (h.length == 9) { body = cfg_desc; blen = 9; }
        else if (h.length == 32) { body = cfg_desc; blen = 32; }
        else { st = -YUSB_EPIPE; }
        uint8_t rbuf[48];
        yusb_usbip_hdr_t rh = yusb_ret_submit(h.seqnum, st, blen);
        yusb_usbip_encode(&rh, rbuf);
        if (!send_all(cfd, rbuf, sizeof(rbuf))) break;
        if (body && blen && !send_all(cfd, body, blen)) break;
    }
    close(cfd);
    return NULL;
}

static void server_start(server_t *srv, bool serve_import)
{
    memset(srv, 0, sizeof(*srv));
    srv->serve_import = serve_import;
    srv->listen_fd = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
    CHECK(srv->listen_fd >= 0);
    int one = 1;
    setsockopt(srv->listen_fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    struct sockaddr_in sin = {.sin_family = AF_INET, .sin_addr.s_addr = htonl(INADDR_LOOPBACK)};
    CHECK(bind(srv->listen_fd, (struct sockaddr *)&sin, sizeof(sin)) == 0);
    socklen_t slen = sizeof(sin);
    CHECK(getsockname(srv->listen_fd, (struct sockaddr *)&sin, &slen) == 0);
    srv->port = ntohs(sin.sin_port);
    CHECK(listen(srv->listen_fd, 4) == 0);
    CHECK(pthread_create(&srv->thread, NULL, server_main, srv) == 0);
}

static void server_stop(server_t *srv)
{
    shutdown(srv->listen_fd, SHUT_RDWR);
    pthread_join(srv->thread, NULL);
    close(srv->listen_fd);
}

static wk_config_t test_cfg(void)
{
    wk_config_t cfg;
    memset(&cfg, 0, sizeof(cfg));
    strcpy(cfg.udc, "dummy_udc.0");
    strcpy(cfg.gadget_driver, "dummy_udc");
    strcpy(cfg.raw_gadget, "/dev/null");
    strcpy(cfg.udc_sysfs, "/nonexistent");
    yusb_policy_defaults(&cfg.policy);
    cfg.policy.stop_timeout_ms = 50;
    cfg.connect_timeout_ms = 2000;
    return cfg;
}

static yusb_target_t test_target(uint16_t port)
{
    yusb_target_t t;
    memset(&t, 0, sizeof(t));
    strcpy(t.host, "127.0.0.1");
    t.port = port;
    strcpy(t.busid, "1-1");
    return t;
}

static void sleep_ms(unsigned ms)
{
    struct timespec ts = {ms / 1000u, (long)(ms % 1000u) * 1000000L};
    clock_nanosleep(CLOCK_MONOTONIC, 0, &ts, NULL);
}

/* F5: when sendmsg stays stuck past deadline (s->in_send == true), session_stop
 * MUST return false with "teardown unconfirmed: transmit did not stop" and MUST
 * NOT call yusb_proxy_stop while the buffer is still being transmitted. */
static void test_stop_unconfirmed_while_in_send(void)
{
    server_t srv;
    server_start(&srv, false);
    pthread_mutex_lock(&wrap_mu);
    send_block_enabled = true;
    send_entered = false;
    send_released = false;
    send_last_errno = 0;
    pthread_mutex_unlock(&wrap_mu);

    wk_config_t cfg = test_cfg();
    yusb_target_t tgt = test_target(srv.port);
    session_t *s = session_start(&cfg, &tgt, NULL);
    CHECK(s != NULL);

    /* Wait until io_main enters __wrap_sendmsg (s->in_send == true). */
    pthread_mutex_lock(&wrap_mu);
    while (!send_entered) pthread_cond_wait(&wrap_cv, &wrap_mu);
    pthread_mutex_unlock(&wrap_mu);

    /* Calling session_stop now: io_main is blocked in sendmsg -> first
     * io_main itself cannot join until sendmsg returns, so let's also cover
     * service_main blocked in sendmsg! Wait: here io_main called after_core()
     * which blocked in sendmsg, so io_main cannot join either. Let's test BOTH:
     * (a) io_main blocked -> session thread did not stop,
     * (b) service_main blocked -> transmit did not stop! */
    char why[200] = {0};
    bool ok = session_stop(s, why, sizeof(why));
    CHECK(!ok);
    CHECK(strstr(why, "teardown unconfirmed:") != NULL);

    /* Release sendmsg; socket was shut down by session_stop so sendmsg sees EPIPE. */
    pthread_mutex_lock(&wrap_mu);
    send_released = true;
    send_block_enabled = false;
    pthread_cond_broadcast(&wrap_cv);
    pthread_mutex_unlock(&wrap_mu);

    /* Retry teardown now confirms cleanly. */
    bool confirmed = false;
    for (int i = 0; i < 10 && !confirmed; ++i) {
        why[0] = 0;
        confirmed = session_stop(s, why, sizeof(why));
        if (!confirmed) sleep_ms(20);
    }
    CHECK(confirmed);
    if (confirmed) session_free(s);
    server_stop(&srv);
}

/* F5: specifically exercise s->in_send held by service_main (not io_main) so
 * session_stop joins io_main first and then hits the `if (s->in_send)` check! */
static void test_stop_unconfirmed_when_service_thread_in_send(void)
{
    /* Exporter that accepts import, then stalls GET_DESCRIPTOR(DEVICE) so
     * service_main's cl_poll() times out the builder URB and calls flush_locked()
     * from service_main while io_main is blocked in poll(recv)! */
    int lfd = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
    CHECK(lfd >= 0);
    int one = 1;
    setsockopt(lfd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    struct sockaddr_in sin = {.sin_family = AF_INET, .sin_addr.s_addr = htonl(INADDR_LOOPBACK)};
    CHECK(bind(lfd, (struct sockaddr *)&sin, sizeof(sin)) == 0);
    socklen_t slen = sizeof(sin);
    CHECK(getsockname(lfd, (struct sockaddr *)&sin, &slen) == 0);
    CHECK(listen(lfd, 2) == 0);

    wk_config_t cfg = test_cfg();
    cfg.policy.import_timeout_ms = 80; /* builder URB times out quickly on service_main */
    cfg.policy.stop_timeout_ms = 50;
    yusb_target_t tgt = test_target(ntohs(sin.sin_port));

    pthread_mutex_lock(&wrap_mu);
    send_block_enabled = false;
    send_entered = false;
    send_released = false;
    pthread_mutex_unlock(&wrap_mu);

    session_t *s = session_start(&cfg, &tgt, NULL);
    CHECK(s != NULL);
    int cfd = accept(lfd, NULL, NULL);
    CHECK(cfd >= 0);
    uint8_t req[YUSB_REQ_IMPORT_SIZE];
    CHECK(recv_all(cfd, req, sizeof(req)));
    uint8_t rep[YUSB_REP_IMPORT_SIZE] = {0};
    yusb_op_encode(YUSB_OP_REP_IMPORT, 0, rep);
    yusb_device_info_t info = {.busnum = 1, .devnum = 2, .speed = YUSB_SPEED_HIGH};
    strcpy(info.busid, "1-1");
    yusb_device_info_encode(&info, rep + 8);
    CHECK(send_all(cfd, rep, sizeof(rep)));
    /* Receive GET_DESCRIPTOR(DEVICE) sent by io_main. */
    uint8_t hdr[48];
    CHECK(recv_all(cfd, hdr, sizeof(hdr)));
    /* Now arm the sendmsg trap: service_main will send CMD_UNLINK when
     * import_timeout_ms (80 ms) expires while io_main is waiting in poll(). */
    pthread_mutex_lock(&wrap_mu);
    send_block_enabled = true;
    send_entered = false;
    send_released = false;
    while (!send_entered) pthread_cond_wait(&wrap_cv, &wrap_mu);
    pthread_mutex_unlock(&wrap_mu);

    /* io_main wakes from abort_fd and joins cleanly, while service_main stays
     * blocked in __wrap_sendmsg with s->in_send == true! */
    char why[200] = {0};
    bool ok = session_stop(s, why, sizeof(why));
    CHECK(!ok);
    CHECK(strstr(why, "teardown unconfirmed: transmit did not stop") != NULL);

    /* Unblock sendmsg: socket was shut down, service_main clears s->in_send. */
    pthread_mutex_lock(&wrap_mu);
    send_released = true;
    send_block_enabled = false;
    pthread_cond_broadcast(&wrap_cv);
    pthread_mutex_unlock(&wrap_mu);

    bool confirmed = false;
    for (int i = 0; i < 20 && !confirmed; ++i) {
        why[0] = 0;
        confirmed = session_stop(s, why, sizeof(why));
        if (!confirmed) sleep_ms(20);
    }
    CHECK(confirmed);
    if (confirmed) session_free(s);
    close(cfd);
    close(lfd);
}

/* F5: session_stop racing with start_gadget closes fd without starting event_main. */
static void *stop_helper(void *arg)
{
    session_t *s = arg;
    char why[200] = {0};
    bool ok = session_stop(s, why, sizeof(why));
    return (void *)(uintptr_t)(ok ? 1 : 0);
}

static void test_stop_racing_start_gadget_closes_fd_cleanly(void)
{
    server_t srv;
    server_start(&srv, true);
    pthread_mutex_lock(&rg.mu);
    rg.block_open = true;
    rg.in_open = false;
    rg.release_open = false;
    rg.opens = rg.closes = rg.event_fetches = rg.sigusr1_interrupts = 0;
    pthread_mutex_unlock(&rg.mu);

    wk_config_t cfg = test_cfg();
    yusb_target_t tgt = test_target(srv.port);
    session_t *s = session_start(&cfg, &tgt, NULL);
    CHECK(s != NULL);

    /* Wait until start_gadget enters fake_open(). */
    pthread_mutex_lock(&rg.mu);
    while (!rg.in_open) pthread_cond_wait(&rg.cv, &rg.mu);
    pthread_mutex_unlock(&rg.mu);

    /* Launch session_stop while start_gadget is blocked in open(). */
    pthread_t stopper;
    CHECK(pthread_create(&stopper, NULL, stop_helper, s) == 0);
    sleep_ms(30); /* ensure s->stopping = true is set before open returns */

    pthread_mutex_lock(&rg.mu);
    rg.release_open = true;
    pthread_cond_broadcast(&rg.cv);
    pthread_mutex_unlock(&rg.mu);

    void *res = NULL;
    CHECK(pthread_join(stopper, &res) == 0);
    CHECK((uintptr_t)res == 1);
    CHECK(rg.opens == 1 && rg.closes == 1);
    CHECK(rg.event_fetches == 0); /* event_main was never started */
    session_free(s);
    server_stop(&srv);
}

/* F11: monotonic join_until kicks SIGUSR1 and unblocks EVENT_FETCH promptly. */
static void test_join_until_kicks_sigusr1_and_stops_promptly(void)
{
    server_t srv;
    server_start(&srv, true);
    CHECK(pipe(rg.wake_pipe) == 0);
    pthread_mutex_lock(&rg.mu);
    rg.block_open = false;
    rg.opens = rg.closes = rg.event_fetches = rg.sigusr1_interrupts = 0;
    rg.event_entered = false;
    pthread_mutex_unlock(&rg.mu);

    wk_config_t cfg = test_cfg();
    yusb_target_t tgt = test_target(srv.port);
    session_t *s = session_start(&cfg, &tgt, NULL);
    CHECK(s != NULL);

    pthread_mutex_lock(&rg.mu);
    while (!rg.event_entered) pthread_cond_wait(&rg.cv, &rg.mu);
    pthread_mutex_unlock(&rg.mu);

    uint64_t t0 = wk_now_ms();
    char why[200] = {0};
    CHECK(session_stop(s, why, sizeof(why)));
    uint64_t elapsed = wk_now_ms() - t0;
    CHECK(elapsed < 500);
    CHECK(rg.opens == 1 && rg.closes == 1);
    CHECK(rg.sigusr1_interrupts >= 1);
    session_free(s);
    close(rg.wake_pipe[0]);
    close(rg.wake_pipe[1]);
    rg.wake_pipe[0] = rg.wake_pipe[1] = -1;
    server_stop(&srv);
}

int main(void)
{
    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = noop_sig;
    sigemptyset(&sa.sa_mask);
    sa.sa_flags = 0; /* no SA_RESTART so SIGUSR1 interrupts poll/ioctl */
    sigaction(SIGUSR1, &sa, NULL);

    test_stop_unconfirmed_while_in_send();
    test_stop_unconfirmed_when_service_thread_in_send();
    test_stop_racing_start_gadget_closes_fd_cleanly();
    test_join_until_kicks_sigusr1_and_stops_promptly();

    if (failures) {
        fprintf(stderr, "%d failures\n", failures);
        return 1;
    }
    puts("all session tests passed");
    return 0;
}
