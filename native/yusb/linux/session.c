/* One native worker session (see session.h).
 *
 * Threads, all serialized on s->lock around core calls:
 *  - io: connects (abortable), then receives straight into the buffers the
 *    core names, and binds Raw Gadget once the presentation is ready;
 *  - service: core timers, and repeating SIGUSR1 at threads whose blocked
 *    ioctl must end (a signal that arrives outside the ioctl is lost);
 *  - event: EVENT_FETCH;
 *  - one operation thread for ep0 and one per endpoint handle, because
 *    every Raw Gadget transfer ioctl blocks until the host moves the data.
 * Whichever thread leaves the core with bytes to send writes them at once
 * (one sendmsg per batch), so a completion's resubmits go out without a
 * thread handoff.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <netdb.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <arpa/inet.h>
#include <poll.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/uio.h>
#include <time.h>
#include <unistd.h>
#include "rawgadget.h"
#include "session.h"
#include "sys.h"

enum { OP_EP0_WRITE = 1, OP_EP0_READ, OP_EP0_STALL, OP_EP_WRITE, OP_EP_READ };
enum { LOG_DEBUG, LOG_INFO, LOG_WARNING, LOG_ERROR };
#define TX_SEGMENTS 64
#define KILL_PERIOD_MS 20u
/* A teardown that was already unconfirmed once is re-checked this long. */
#define STOP_RETRY_GRACE_MS 100u

typedef struct session session_t;

typedef struct {
    session_t *s;
    int handle;               /* -1: endpoint zero */
    pthread_t thread;
    pthread_cond_t cond;
    bool started, pending, running, cancel, exit;
    bool failed;              /* no thread: complete as YUSB_GADGET_ERROR */
    int kind;
    uint8_t *data;
    size_t len;
    bool zero;
} op_t;

/* What an operation thread performs, copied under the lock. */
typedef struct {
    int kind, handle, rg;
    uint8_t *data;
    size_t len;
    bool zero;
} job_t;

struct session {
    wk_config_t cfg;
    yusb_target_t target;
    wk_log_fn log;
    pthread_mutex_t lock;
    pthread_cond_t wake;
    yusb_proxy_t *proxy;
    int sock, rg, abort_fd[2];
    char gadget_name[128];
    bool stopping, tx_closed, in_send, gadget, built, io_done, exit;
    bool stop_tried;          /* session_stop ran (and was unconfirmed) before */
    uint64_t service_at;      /* when the waiting service thread wakes by itself; 0 awake */
    bool service_dirty;       /* core state changed while the service thread was awake
                                 (or with the lock dropped): recompute before waiting */
    char fatal[256];
    pthread_t io, service, event;
    bool io_started, service_started, event_started;
    op_t ep0;
    op_t eps[RG_EPS_MAX];
};

uint64_t wk_now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000u + (uint64_t)ts.tv_nsec / 1000000u;
}

static void logf_(session_t *s, int level, const char *fmt, ...)
{
    if (!s->log) return;
    char text[320];
    va_list args;
    va_start(args, fmt);
    vsnprintf(text, sizeof(text), fmt, args);
    va_end(args);
    s->log(level, text);
}

static void core_log(void *ctx, const char *message) { logf_(ctx, LOG_INFO, "%s", message); }

/* Every session thread: named for diagnostics, and deaf to SIGTERM and
 * SIGINT (the main thread takes them; see worker_main.c), so an EINTR here
 * is always a deliberate SIGUSR1 cancellation. The mask is inherited from
 * the main thread already; this keeps it true for any creator. */
static void thread_enter(const char *name)
{
    sigset_t set;
    sigemptyset(&set);
    sigaddset(&set, SIGTERM);
    sigaddset(&set, SIGINT);
    pthread_sigmask(SIG_BLOCK, &set, NULL);
    pthread_setname_np(pthread_self(), name);
}

static void fatal(session_t *s, const char *fmt, ...)
{
    if (s->fatal[0]) return;
    va_list args;
    va_start(args, fmt);
    vsnprintf(s->fatal, sizeof(s->fatal), fmt, args);
    va_end(args);
    logf_(s, LOG_WARNING, "session failed: %s", s->fatal);
}

/* Send what the core queued; the lock is released around sendmsg. */
static void flush_locked(session_t *s)
{
    while (s->proxy && !s->tx_closed && !s->in_send) {
        yusb_iov_t seg[TX_SEGMENTS];
        struct iovec iov[TX_SEGMENTS];
        size_t n = yusb_proxy_tx_iov(s->proxy, seg, TX_SEGMENTS);
        if (!n) return;
        for (size_t i = 0; i < n; ++i) {
            iov[i].iov_base = (void *)seg[i].base;
            iov[i].iov_len = seg[i].len;
        }
        struct msghdr msg = {.msg_iov = iov, .msg_iovlen = n};
        s->in_send = true;
        pthread_mutex_unlock(&s->lock);
        ssize_t w = sendmsg(s->sock, &msg, MSG_NOSIGNAL);
        int err = errno;
        pthread_mutex_lock(&s->lock);
        s->in_send = false;
        if (s->stopping) pthread_cond_broadcast(&s->wake);   /* session_stop waits */
        s->service_dirty = true;   /* the lock was dropped; tx_done may settle */
        if (w > 0) {
            yusb_proxy_tx_done(s->proxy, (size_t)w);
        } else if (w < 0 && err == EINTR) {
            continue;
        } else {
            if (!s->stopping) yusb_proxy_transport_lost(s->proxy, strerror(err), wk_now_ms());
            return;
        }
    }
}

/* Bytes queued that this thread may send now (none while another sends:
 * that sender's loop takes them). */
static bool tx_pending(session_t *s)
{
    yusb_iov_t seg;
    return s->proxy && !s->tx_closed && !s->in_send && yusb_proxy_tx_iov(s->proxy, &seg, 1);
}

/* Leaving the core: send what it queued, run due timers (and send what they
 * queue), and wake the service thread only when its timer must fire earlier
 * (a wakeup per transfer would put it on the lock in every transfer's
 * critical path). */
static void after_core(session_t *s)
{
    uint64_t next;
    do {
        flush_locked(s);
        if (!s->proxy) return;
        next = yusb_proxy_poll(s->proxy, wk_now_ms());
    } while (tx_pending(s));
    if (!s->service_at) s->service_dirty = true;   /* awake: it must not wait on a stale deadline */
    else if (s->stopping || next < s->service_at) pthread_cond_broadcast(&s->wake);
}

/* --- operation threads ---------------------------------------------------- */

static long perform(const job_t *j)
{
    const ysys_t *sys = ysys();
    switch (j->kind) {
    case OP_EP0_WRITE:
        rg_pack_io(j->data, 0, j->len, j->zero);
        return sys->ioctl(j->rg, RG_IOCTL_EP0_WRITE, j->data - YUSB_HEADROOM);
    case OP_EP0_READ:
        rg_pack_io(j->data, 0, j->len, false);
        return sys->ioctl(j->rg, RG_IOCTL_EP0_READ, j->data - YUSB_HEADROOM);
    case OP_EP0_STALL:
        return sys->ioctl(j->rg, RG_IOCTL_EP0_STALL, NULL);
    case OP_EP_WRITE:
        rg_pack_io(j->data, (unsigned)j->handle, j->len, j->zero);
        return sys->ioctl(j->rg, RG_IOCTL_EP_WRITE, j->data - YUSB_HEADROOM);
    case OP_EP_READ:
        rg_pack_io(j->data, (unsigned)j->handle, j->len, false);
        return sys->ioctl(j->rg, RG_IOCTL_EP_READ, j->data - YUSB_HEADROOM);
    default:
        return -EINVAL;
    }
}

static void op_done(session_t *s, op_t *o, int result)
{
    if (o->handle < 0) yusb_proxy_ep0_done(s->proxy, result, wk_now_ms());
    else yusb_proxy_ep_done(s->proxy, o->handle, result, wk_now_ms());
}

static void *op_main(void *arg)
{
    op_t *o = arg;
    session_t *s = o->s;
    char name[16];
    if (o->handle < 0) snprintf(name, sizeof(name), "yw-ep0");
    else snprintf(name, sizeof(name), "yw-ep%d", o->handle);
    thread_enter(name);
    pthread_mutex_lock(&s->lock);
    for (;;) {
        while (!o->pending && !o->exit) pthread_cond_wait(&o->cond, &s->lock);
        if (o->exit) break;
        o->pending = false;
        o->running = true;
        bool cancelled = o->cancel;
        job_t job = {o->kind, o->handle, s->rg, o->data, o->len, o->zero};
        pthread_mutex_unlock(&s->lock);
        /* Called once: EINTR is a cancellation, never retried here. */
        long rc = cancelled ? -EINTR : perform(&job);
        pthread_mutex_lock(&s->lock);
        o->running = false;
        o->cancel = false;
        op_done(s, o, rg_result(rc));
        after_core(s);
    }
    pthread_mutex_unlock(&s->lock);
    return NULL;
}

/* Operations posted without a thread end as YUSB_GADGET_ERROR, outside the
 * core call that posted them (service thread, under the lock). */
static void complete_failed(session_t *s)
{
    if (!s->proxy) return;
    for (int i = -1; i < (int)RG_EPS_MAX; ++i) {
        op_t *o = i < 0 ? &s->ep0 : &s->eps[i];
        if (!o->failed) continue;
        o->failed = false;
        op_done(s, o, YUSB_GADGET_ERROR);
        after_core(s);
    }
}

static void post(session_t *s, op_t *o, int kind, uint8_t *data, size_t len, bool zero)
{
    if (!o->started) {
        if (pthread_create(&o->thread, NULL, op_main, o)) {
            fatal(s, "cannot start an endpoint thread");
            o->failed = true;
            pthread_cond_broadcast(&s->wake);
            return;
        }
        o->started = true;
    }
    o->kind = kind;
    o->data = data;
    o->len = len;
    o->zero = zero;
    o->pending = true;
    o->cancel = false;
    pthread_cond_signal(&o->cond);
}

/* --- gadget operations (called by the core under s->lock) ---------------- */

static void g_ep0_write(void *ctx, uint8_t *data, size_t len, bool zero)
{
    session_t *s = ctx;
    post(s, &s->ep0, OP_EP0_WRITE, data, len, zero);
}

static void g_ep0_read(void *ctx, uint8_t *data, size_t len)
{
    session_t *s = ctx;
    post(s, &s->ep0, OP_EP0_READ, data, len, false);
}

static void g_ep0_stall(void *ctx)
{
    session_t *s = ctx;
    post(s, &s->ep0, OP_EP0_STALL, NULL, 0, false);
}

static int g_ep_enable(void *ctx, const yusb_endpoint_t *ep)
{
    session_t *s = ctx;
    uint8_t desc[9] = {0};
    memcpy(desc, ep->raw, ep->raw_len < sizeof(desc) ? ep->raw_len : sizeof(desc));
    long rc = ysys()->ioctl(s->rg, RG_IOCTL_EP_ENABLE, desc);
    if (rc >= 0 && rc >= (long)RG_EPS_MAX) return -EINVAL;
    return (int)rc;
}

static int g_ep_disable(void *ctx, int handle)
{
    session_t *s = ctx;
    return (int)ysys()->ioctl(s->rg, RG_IOCTL_EP_DISABLE, (void *)(uintptr_t)handle);
}

static int g_ep_set_halt(void *ctx, int handle)
{
    session_t *s = ctx;
    return (int)ysys()->ioctl(s->rg, RG_IOCTL_EP_SET_HALT, (void *)(uintptr_t)handle);
}

static int g_configure(void *ctx)
{
    session_t *s = ctx;
    long rc = ysys()->ioctl(s->rg, RG_IOCTL_CONFIGURE, NULL);
    return rc < 0 ? (int)rc : 0;
}

static int g_vbus_draw(void *ctx, unsigned ma)
{
    session_t *s = ctx;
    long rc = ysys()->ioctl(s->rg, RG_IOCTL_VBUS_DRAW, (void *)(uintptr_t)((ma + 1) / 2));
    return rc < 0 ? (int)rc : 0;
}

static yusb_speed_t g_link_speed(void *ctx)
{
    session_t *s = ctx;
    char path[512], text[64];
    snprintf(path, sizeof(path), "%s/%s/current_speed", s->cfg.udc_sysfs, s->cfg.udc);
    long n = ysys()->read_text(path, text, sizeof(text));
    return n < 0 ? YUSB_SPEED_UNKNOWN : yusb_speed_from_name(text, (size_t)n);
}

static void g_ep_write(void *ctx, int handle, uint8_t *data, size_t len, bool zero)
{
    session_t *s = ctx;
    if (handle >= 0 && handle < (int)RG_EPS_MAX) post(s, &s->eps[handle], OP_EP_WRITE, data, len, zero);
}

static void g_ep_read(void *ctx, int handle, uint8_t *data, size_t len)
{
    session_t *s = ctx;
    if (handle >= 0 && handle < (int)RG_EPS_MAX) post(s, &s->eps[handle], OP_EP_READ, data, len, false);
}

static void g_ep_cancel(void *ctx, int handle)
{
    session_t *s = ctx;
    if (handle < 0 || handle >= (int)RG_EPS_MAX) return;
    op_t *o = &s->eps[handle];
    o->cancel = true;          /* a queued op ends without its ioctl */
    if (o->running) pthread_kill(o->thread, SIGUSR1);
    pthread_cond_broadcast(&s->wake);
}

/* --- event thread ------------------------------------------------------- */

static void *event_main(void *arg)
{
    session_t *s = arg;
    thread_enter("yw-event");
    for (;;) {
        pthread_mutex_lock(&s->lock);
        bool stop = s->exit;
        int rg = s->rg;
        pthread_mutex_unlock(&s->lock);
        if (stop) break;
        uint8_t buf[8 + 8] = {0};
        uint32_t len = 8;
        memcpy(buf + 4, &len, 4);
        long rc = ysys()->ioctl(rg, RG_IOCTL_EVENT_FETCH, buf);
        if (rc == -EINTR) continue;
        pthread_mutex_lock(&s->lock);
        if (rc < 0) {
            if (!s->stopping) fatal(s, "gadget event error: %s", strerror((int)-rc));
            pthread_cond_broadcast(&s->wake);
            pthread_mutex_unlock(&s->lock);
            break;
        }
        uint32_t type, got;
        memcpy(&type, buf, 4);
        memcpy(&got, buf + 4, 4);
        if (type == YUSB_EV_CONTROL && got != 8) {
            if (!s->stopping) fatal(s, "gadget event error: control event without setup");
        } else if (type >= YUSB_EV_CONNECT && type <= YUSB_EV_DISCONNECT) {
            yusb_proxy_event(s->proxy, (yusb_event_t)type, buf + 8, wk_now_ms());
        }
        after_core(s);
        pthread_mutex_unlock(&s->lock);
    }
    return NULL;
}

/* --- service thread ----------------------------------------------------- */

static bool signal_cancels(session_t *s)
{
    bool any = false;
    if (s->ep0.running && s->stopping) { pthread_kill(s->ep0.thread, SIGUSR1); any = true; }
    for (unsigned i = 0; i < RG_EPS_MAX; ++i) {
        op_t *o = &s->eps[i];
        if (o->running && (o->cancel || s->stopping)) { pthread_kill(o->thread, SIGUSR1); any = true; }
    }
    return any;
}

static void *service_main(void *arg)
{
    session_t *s = arg;
    thread_enter("yw-service");
    pthread_mutex_lock(&s->lock);
    while (!s->exit) {
        /* Timers, then sends; repeated until nothing changed meanwhile: a
         * send drops the lock, and its tx_done or another thread's core call
         * can set an earlier deadline than the `next` computed here. */
        s->service_dirty = false;
        complete_failed(s);
        uint64_t now = wk_now_ms();
        uint64_t next = s->proxy ? yusb_proxy_poll(s->proxy, now) : UINT64_MAX;
        flush_locked(s);
        if (s->service_dirty) continue;
        if (signal_cancels(s) && next > now + KILL_PERIOD_MS) next = now + KILL_PERIOD_MS;
        if (next > now + 1000) next = now + 1000;
        struct timespec ts;
        clock_gettime(CLOCK_MONOTONIC, &ts);
        uint64_t wait = next > now ? next - now : 0;
        ts.tv_sec += (time_t)(wait / 1000);
        ts.tv_nsec += (long)(wait % 1000) * 1000000L;
        if (ts.tv_nsec >= 1000000000L) { ts.tv_sec++; ts.tv_nsec -= 1000000000L; }
        s->service_at = next;
        pthread_cond_timedwait(&s->wake, &s->lock, &ts);
        s->service_at = 0;      /* awake: it recomputes the deadline itself */
    }
    pthread_mutex_unlock(&s->lock);
    return NULL;
}

/* --- io thread ---------------------------------------------------------- */

static int connect_target(session_t *s, char *err, size_t cap)
{
    char port[8];
    snprintf(port, sizeof(port), "%u", s->target.port);
    struct addrinfo hints = {.ai_family = AF_INET, .ai_socktype = SOCK_STREAM}, *res = NULL;
    int rc = getaddrinfo(s->target.host, port, &hints, &res);
    if (rc || !res) {
        snprintf(err, cap, "gaierror: %s", gai_strerror(rc));
        return -1;
    }
    int fd = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
    if (fd < 0) {
        snprintf(err, cap, "socket: %s", strerror(errno));
        freeaddrinfo(res);
        return -1;
    }
    /* Producer admission accepts only the consumer's announced IPv4 (⚠). */
    if (s->target.source[0]) {
        struct sockaddr_in src = {.sin_family = AF_INET};
        if (inet_pton(AF_INET, s->target.source, &src.sin_addr) != 1 ||
            bind(fd, (struct sockaddr *)&src, sizeof(src))) {
            snprintf(err, cap, "bind %s: %s", s->target.source, strerror(errno));
            goto fail;
        }
    }
    if (connect(fd, res->ai_addr, res->ai_addrlen) && errno != EINPROGRESS) {
        snprintf(err, cap, "connect %.100s:%u: %s", s->target.host, s->target.port, strerror(errno));
        goto fail;
    }
    struct pollfd pfd[2] = {{fd, POLLOUT, 0}, {s->abort_fd[0], POLLIN, 0}};
    int n = poll(pfd, 2, (int)s->cfg.connect_timeout_ms);
    if (n == 0) { snprintf(err, cap, "TimeoutError: timed out"); goto fail; }
    if (n < 0 || pfd[1].revents) { snprintf(err, cap, "route changed during session setup"); goto fail; }
    int so = 0;
    socklen_t len = sizeof(so);
    getsockopt(fd, SOL_SOCKET, SO_ERROR, &so, &len);
    if (so) {
        snprintf(err, cap, "connect %.100s:%u: %s", s->target.host, s->target.port, strerror(so));
        goto fail;
    }
    fcntl(fd, F_SETFL, fcntl(fd, F_GETFL) & ~O_NONBLOCK);
    int one = 1, idle = 2, intvl = 1, cnt = 3;
    setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
    setsockopt(fd, SOL_SOCKET, SO_KEEPALIVE, &one, sizeof(one));
    setsockopt(fd, IPPROTO_TCP, TCP_KEEPIDLE, &idle, sizeof(idle));
    setsockopt(fd, IPPROTO_TCP, TCP_KEEPINTVL, &intvl, sizeof(intvl));
    setsockopt(fd, IPPROTO_TCP, TCP_KEEPCNT, &cnt, sizeof(cnt));
    freeaddrinfo(res);
    return fd;
fail:
    close(fd);
    freeaddrinfo(res);
    return -1;
}

/* Bind Raw Gadget at the presented speed (RawGadgetBackend.start). */
static void start_gadget(session_t *s, unsigned speed)
{
    const ysys_t *sys = ysys();
    uint8_t init[RG_INIT_SIZE];
    int fd = sys->open(s->cfg.raw_gadget);
    if (fd < 0) {
        pthread_mutex_lock(&s->lock);
        fatal(s, "GadgetError: open %s: %s", s->cfg.raw_gadget, strerror(-fd));
        pthread_mutex_unlock(&s->lock);
        return;
    }
    logf_(s, LOG_INFO, "Raw Gadget on UDC %s as gadget %s", s->cfg.udc, s->gadget_name);
    long rc = rg_pack_init(init, s->gadget_name, s->cfg.udc, speed);
    if (!rc) rc = sys->ioctl(fd, RG_IOCTL_INIT, init);
    const char *what = "INIT";
    if (!rc) {
        what = "RUN";
        rc = sys->ioctl(fd, RG_IOCTL_RUN, NULL);
    }
    if (rc < 0) {
        sys->close(fd);
        pthread_mutex_lock(&s->lock);
        if (!strcmp(what, "RUN"))
            fatal(s, "GadgetError: raw-gadget RUN on %s as %s (wrong gadget name, or another "
                     "gadget bound?): %s", s->cfg.udc, s->gadget_name, strerror((int)-rc));
        else
            fatal(s, "GadgetError: raw-gadget INIT: %s", strerror((int)-rc));
        pthread_mutex_unlock(&s->lock);
        return;
    }
    uint8_t raw[RG_EPS_MAX * RG_EP_INFO_SIZE] = {0};
    yusb_udc_ep_t eps[RG_EPS_MAX];
    size_t count = 0;
    rc = sys->ioctl(fd, RG_IOCTL_EPS_INFO, raw);
    if (rc >= 0) count = rg_unpack_eps(raw, (size_t)rc, eps, RG_EPS_MAX);
    else logf_(s, LOG_INFO, "EPS_INFO unavailable: %s", strerror((int)-rc));
    pthread_mutex_lock(&s->lock);
    s->rg = fd;
    s->gadget = true;
    yusb_proxy_gadget_started(s->proxy, count ? eps : NULL, count, wk_now_ms());
    if (yusb_proxy_phase(s->proxy) == YUSB_PHASE_PRESENTING) s->built = true;
    if (!pthread_create(&s->event, NULL, event_main, s)) s->event_started = true;
    else fatal(s, "cannot start the gadget event thread");
    after_core(s);
    pthread_mutex_unlock(&s->lock);
}

static void *io_main(void *arg)
{
    session_t *s = arg;
    char err[200];
    thread_enter("yw-io");
    int fd = connect_target(s, err, sizeof(err));
    pthread_mutex_lock(&s->lock);
    if (fd < 0) {
        fatal(s, "%s", err);
        goto done;
    }
    s->sock = fd;
    yusb_gadget_ops_t ops = {
        .ctx = s, .ep0_write = g_ep0_write, .ep0_read = g_ep0_read, .ep0_stall = g_ep0_stall,
        .ep_enable = g_ep_enable, .ep_disable = g_ep_disable, .ep_set_halt = g_ep_set_halt,
        .configure = g_configure, .vbus_draw = g_vbus_draw, .link_speed = g_link_speed,
        .ep_write = g_ep_write, .ep_read = g_ep_read, .ep_cancel = g_ep_cancel, .log = core_log,
    };
    s->proxy = yusb_proxy_create(&s->cfg.policy, &ops, s->target.busid, wk_now_ms());
    if (!s->proxy) {
        fatal(s, "cannot create the proxy (policy or busid invalid)");
        goto done;
    }
    if (pthread_create(&s->service, NULL, service_main, s)) {
        fatal(s, "cannot start the service thread");
        goto done;
    }
    s->service_started = true;
    after_core(s);
    while (!s->stopping) {
        uint8_t *buf;
        size_t n = yusb_proxy_rx_space(s->proxy, &buf);
        if (!n) break;
        /* A payload's own buffer: wait for all of it in one call. */
        int flags = yusb_proxy_rx_direct(s->proxy) ? MSG_WAITALL : 0;
        pthread_mutex_unlock(&s->lock);
        struct pollfd pfd[2] = {{fd, POLLIN, 0}, {s->abort_fd[0], POLLIN, 0}};
        int ready = poll(pfd, 2, -1);
        ssize_t r = -1;
        int e = 0;
        if (ready > 0 && !pfd[1].revents) {
            r = recv(fd, buf, n, flags);
            e = errno;
        }
        pthread_mutex_lock(&s->lock);
        if (s->stopping || pfd[1].revents) break;
        if (ready < 0 || (r < 0 && (e == EINTR || e == EAGAIN))) continue;
        uint64_t now = wk_now_ms();
        if (r > 0) yusb_proxy_rx_done(s->proxy, (size_t)r, now);
        else yusb_proxy_transport_lost(s->proxy, r == 0 ? "USB/IP peer closed the connection" : strerror(e), now);
        after_core(s);
        if (yusb_proxy_phase(s->proxy) == YUSB_PHASE_READY && !s->gadget) {
            unsigned speed = yusb_proxy_presentation(s->proxy)->speed;
            pthread_mutex_unlock(&s->lock);
            start_gadget(s, speed);
            pthread_mutex_lock(&s->lock);
            if (s->fatal[0]) break;
        }
    }
done:
    s->io_done = true;
    pthread_cond_broadcast(&s->wake);
    pthread_mutex_unlock(&s->lock);
    return NULL;
}

/* --- lifecycle ---------------------------------------------------------- */

session_t *session_start(const wk_config_t *cfg, const yusb_target_t *target, wk_log_fn log)
{
    session_t *s = calloc(1, sizeof(*s));
    if (!s) return NULL;
    s->cfg = *cfg;
    s->target = *target;
    s->log = log;
    s->sock = s->rg = -1;
    if (pipe2(s->abort_fd, O_CLOEXEC)) {
        free(s);
        return NULL;
    }
    pthread_condattr_t attr;
    pthread_condattr_init(&attr);
    pthread_condattr_setclock(&attr, CLOCK_MONOTONIC);
    pthread_cond_init(&s->wake, &attr);
    pthread_condattr_destroy(&attr);
    pthread_mutex_init(&s->lock, NULL);
    s->ep0.s = s;
    s->ep0.handle = -1;
    pthread_cond_init(&s->ep0.cond, NULL);
    for (int i = 0; i < (int)RG_EPS_MAX; ++i) {
        s->eps[i].s = s;
        s->eps[i].handle = i;
        pthread_cond_init(&s->eps[i].cond, NULL);
    }
    if (cfg->gadget_driver[0]) snprintf(s->gadget_name, sizeof(s->gadget_name), "%s", cfg->gadget_driver);
    else rg_gadget_name(cfg->udc_sysfs, cfg->udc, s->gadget_name, sizeof(s->gadget_name));
    if (pthread_create(&s->io, NULL, io_main, s)) {
        fatal(s, "cannot start the session thread");
        s->io_done = true;
    } else {
        s->io_started = true;
    }
    return s;
}

void session_view(session_t *s, session_view_t *v)
{
    memset(v, 0, sizeof(*v));
    pthread_mutex_lock(&s->lock);
    yusb_phase_t phase = s->proxy ? yusb_proxy_phase(s->proxy) : YUSB_PHASE_IMPORT;
    v->built = s->built;
    if (s->proxy) {
        yusb_proxy_status(s->proxy, &v->status);
        v->counters = *yusb_proxy_counters(s->proxy);
        snprintf(v->rejected_text, sizeof(v->rejected_text), "%s", v->status.rejected);
        snprintf(v->error, sizeof(v->error), "%s", v->status.error);
        snprintf(v->link_speed, sizeof(v->link_speed), "%s", yusb_speed_name(v->status.link_speed));
    }
    v->ended = s->fatal[0] || (s->proxy && phase >= YUSB_PHASE_ENDED) || (s->io_done && !s->proxy);
    if (s->fatal[0]) {
        snprintf(v->reason, sizeof(v->reason), "%s", s->fatal);
    } else if (s->proxy) {
        snprintf(v->reason, sizeof(v->reason), "%s", v->status.end_reason);
        v->rejected = v->status.end == YUSB_END_UNSUPPORTED || v->status.end == YUSB_END_REJECTED;
        v->yx_unavailable = v->status.end == YUSB_END_YX_UNAVAILABLE;
    }
    /* Strings in the status point into the core; the caller only uses the
     * copies above and the static state name. */
    v->status.end_reason = v->status.error = v->status.rejected = "";
    pthread_mutex_unlock(&s->lock);
}

static bool join_until(pthread_t thread, uint64_t deadline, pthread_t *kick)
{
    for (;;) {
        if (kick) pthread_kill(*kick, SIGUSR1);
        struct timespec ts;
        clock_gettime(CLOCK_REALTIME, &ts);
        ts.tv_nsec += (long)KILL_PERIOD_MS * 1000000L;
        if (ts.tv_nsec >= 1000000000L) { ts.tv_sec++; ts.tv_nsec -= 1000000000L; }
        if (!pthread_timedjoin_np(thread, NULL, &ts)) return true;
        if (wk_now_ms() >= deadline) return false;
    }
}

bool session_stop(session_t *s, char *why, size_t cap)
{
    /* A retry after an unconfirmed teardown only re-checks briefly, so the
     * worker's backoff, not this wait, paces the retries. */
    uint64_t deadline = wk_now_ms() + (s->stop_tried ? STOP_RETRY_GRACE_MS
                                                     : s->cfg.policy.stop_timeout_ms + 1000);
    pthread_mutex_lock(&s->lock);
    bool first = !s->stop_tried;
    s->stop_tried = s->stopping = true;
    int sock = s->sock;
    pthread_mutex_unlock(&s->lock);
    /* 1. Quiet the transport: no receive or send may touch core buffers. */
    if (sock >= 0) shutdown(sock, SHUT_RDWR);
    if (first && write(s->abort_fd[1], "x", 1) < 0) { /* the pipe is ours; cannot fail usefully */ }
    if (s->io_started) {
        if (!join_until(s->io, deadline, NULL)) {
            snprintf(why, cap, "teardown unconfirmed: session thread did not stop");
            return false;
        }
        s->io_started = false;
    }
    pthread_mutex_lock(&s->lock);
    s->tx_closed = true;
    while (s->in_send && wk_now_ms() < deadline) {
        struct timespec ts;
        clock_gettime(CLOCK_MONOTONIC, &ts);
        ts.tv_nsec += 10000000L;
        if (ts.tv_nsec >= 1000000000L) { ts.tv_sec++; ts.tv_nsec -= 1000000000L; }
        pthread_cond_timedwait(&s->wake, &s->lock, &ts);
    }
    /* 2. The core cancels gadget operations; the service thread repeats
     * SIGUSR1 at blocked ones until they return. */
    if (s->proxy) {
        yusb_proxy_stop(s->proxy, wk_now_ms());
        after_core(s);
        while (yusb_proxy_phase(s->proxy) != YUSB_PHASE_STOPPED) {
            uint64_t now = wk_now_ms();
            if (now >= deadline) {
                snprintf(why, cap, "teardown unconfirmed: gadget operations did not stop");
                pthread_mutex_unlock(&s->lock);
                return false;
            }
            struct timespec ts;
            clock_gettime(CLOCK_MONOTONIC, &ts);
            ts.tv_nsec += 10000000L;
            if (ts.tv_nsec >= 1000000000L) { ts.tv_sec++; ts.tv_nsec -= 1000000000L; }
            pthread_cond_timedwait(&s->wake, &s->lock, &ts);
            if (s->proxy) yusb_proxy_poll(s->proxy, wk_now_ms());
        }
    }
    /* 3. Every thread leaves; the event thread is blocked in EVENT_FETCH. */
    s->exit = true;
    s->ep0.exit = true;
    if (s->ep0.started) pthread_cond_signal(&s->ep0.cond);
    for (unsigned i = 0; i < RG_EPS_MAX; ++i) {
        s->eps[i].exit = true;
        if (s->eps[i].started) pthread_cond_signal(&s->eps[i].cond);
    }
    pthread_cond_broadcast(&s->wake);
    pthread_mutex_unlock(&s->lock);
    if (s->event_started && join_until(s->event, deadline, &s->event)) s->event_started = false;
    if (s->service_started && join_until(s->service, deadline, NULL)) s->service_started = false;
    if (s->ep0.started && join_until(s->ep0.thread, deadline, NULL)) s->ep0.started = false;
    for (unsigned i = 0; i < RG_EPS_MAX; ++i)
        if (s->eps[i].started && join_until(s->eps[i].thread, deadline, NULL)) s->eps[i].started = false;
    bool stuck = s->event_started || s->service_started || s->ep0.started;
    for (unsigned i = 0; i < RG_EPS_MAX; ++i) stuck = stuck || s->eps[i].started;
    if (stuck) {
        snprintf(why, cap, "teardown unconfirmed: gadget threads did not stop");
        return false;
    }
    /* 4. Close: the computer sees a disconnect when the gadget fd closes. */
    if (s->sock >= 0) close(s->sock);
    if (s->rg >= 0) ysys()->close(s->rg);
    s->sock = s->rg = -1;
    return true;
}

void session_free(session_t *s)
{
    if (!s) return;
    yusb_proxy_destroy(s->proxy);
    close(s->abort_fd[0]);
    close(s->abort_fd[1]);
    pthread_mutex_destroy(&s->lock);
    pthread_cond_destroy(&s->wake);
    pthread_cond_destroy(&s->ep0.cond);
    for (unsigned i = 0; i < RG_EPS_MAX; ++i) pthread_cond_destroy(&s->eps[i].cond);
    free(s);
}
