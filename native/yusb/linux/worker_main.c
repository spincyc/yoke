/* yoke-gadget-worker: the native tier-2 gadget worker (ADR 0027).
 *
 * A drop-in for `python -m yoke.usb.worker` (reference: worker.py): the
 * same command line, the same ADR 0022 control socket and status schema,
 * so the agent's usb-gadget driver supervises either. Routes are adopted
 * only after confirmed teardown of every older session (generation fence);
 * remote loss reconnects with capped backoff. Payload never leaves this
 * process. Native-only options are marked below.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/file.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>
#include "control.h"
#include "session.h"

/* Message levels (session.c uses the first four); a threshold of
 * LOG_CRITICAL silences everything the worker logs. */
enum { LOG_DEBUG, LOG_INFO, LOG_WARNING, LOG_ERROR, LOG_CRITICAL };
static const char *const level_names[] = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"};
static int log_level = LOG_INFO;
static pthread_mutex_t log_lock = PTHREAD_MUTEX_INITIALIZER;
static volatile sig_atomic_t stop_requested;
static int wake_fd[2] = {-1, -1};

static void wlog(int level, const char *message)
{
    if (level < log_level) return;
    pthread_mutex_lock(&log_lock);
    fprintf(stderr, "%s yoke.usb.worker: %s\n", level_names[level], message);
    pthread_mutex_unlock(&log_lock);
}

static void wlogf(int level, const char *fmt, ...)
{
    char text[400];
    va_list args;
    va_start(args, fmt);
    vsnprintf(text, sizeof(text), fmt, args);
    va_end(args);
    wlog(level, text);
}

/* Unconfirmed teardown is retried with this backoff (each retry is short,
 * see session_stop), so a route request never waits behind retries. */
#define STOP_RETRY_MIN_MS 100u
#define STOP_RETRY_MAX_MS 2000u

typedef struct {
    wk_config_t cfg;              /* cfg.policy.yx_levers: what the next session offers */
    uint8_t yx_levers;            /* --yx as configured */
    pthread_mutex_t apply_lock;   /* serializes route changes and sessions; guards cfg
                                     and the stop retry fields */
    pthread_mutex_t lock;         /* fence, session pointer, last view */
    yusb_fence_t fence;
    session_t *session;
    bool built;
    bool have_view;
    session_view_t view;          /* worker._last */
    uint64_t stop_retry_at;
    uint32_t stop_backoff_ms;
} worker_t;

/* Python logging's level names (logging._nameToLevel), case-insensitively. */
static bool parse_level(const char *text, int *level)
{
    static const struct {
        const char *name;
        int level;
    } names[] = {
        {"NOTSET", LOG_DEBUG}, {"DEBUG", LOG_DEBUG},     {"INFO", LOG_INFO},
        {"WARN", LOG_WARNING}, {"WARNING", LOG_WARNING}, {"ERROR", LOG_ERROR},
        {"FATAL", LOG_CRITICAL}, {"CRITICAL", LOG_CRITICAL},
    };
    for (size_t i = 0; i < sizeof(names) / sizeof(names[0]); ++i) {
        if (!strcasecmp(text, names[i].name)) {
            *level = names[i].level;
            return true;
        }
    }
    return false;
}

/* SIGTERM and SIGINT reach the main thread only (inside ppoll): a stray
 * EINTR in another thread could repeat an IN or lose an OUT transfer.
 * SIGUSR1 stays deliverable everywhere; it is the cancellation signal. */
static void block_termination(sigset_t *previous)
{
    sigset_t set;
    sigemptyset(&set);
    sigaddset(&set, SIGTERM);
    sigaddset(&set, SIGINT);
    pthread_sigmask(SIG_BLOCK, &set, previous);
}

static void wake(void)
{
    if (wake_fd[1] >= 0 && write(wake_fd[1], "w", 1) < 0) { /* full pipe: already awake */ }
}

/* --- status (worker.status / WorkerStatus.to_json) ------------------------ */

static double wall_seconds(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

static size_t format_status(worker_t *w, char *out, size_t cap)
{
    pthread_mutex_lock(&w->lock);
    char vendor[8], product[8];
    yctl_pair_t device[4];
    yctl_counter_t counters[64];
    size_t ndev = 0, ncount = 0;
    const session_view_t *v = &w->view;
    if (w->have_view) {
        snprintf(vendor, sizeof(vendor), "%04x", v->status.vendor);
        snprintf(product, sizeof(product), "%04x", v->status.product);
        device[ndev++] = (yctl_pair_t){"vendor", vendor};
        device[ndev++] = (yctl_pair_t){"product", product};
        device[ndev++] = (yctl_pair_t){"remote_speed", yusb_speed_name(v->status.remote_speed)};
        device[ndev++] = (yctl_pair_t){"presented_speed", yusb_speed_name(v->status.presented_speed)};
#define COUNTER(name) counters[ncount++] = (yctl_counter_t){#name, v->counters.name};
        YUSB_PROXY_COUNTERS(COUNTER)
#undef COUNTER
    }
    yctl_status_t status = {
        .generation = w->fence.generation,
        .state = yusb_wstate_name(w->fence.state),
        .detail = w->fence.detail,
        .target = w->fence.has_target ? &w->fence.target : NULL,
        .udc = w->cfg.udc,
        .link_speed = w->have_view ? v->link_speed : "",
        .device = device,
        .device_count = ndev,
        .counters = counters,
        .counter_count = ncount,
        .reconnects = w->fence.reconnects,
        .updated = wall_seconds(),
    };
    size_t n = yctl_format_status(&status, out, cap);
    pthread_mutex_unlock(&w->lock);
    return n;
}

/* --- route changes (worker.apply) ----------------------------------------- */

/* Confirmed stop of the current session; caller holds apply_lock. When
 * it is unconfirmed the session is kept, the fence holds the previous
 * generation (ADR 0022) and step() retries later. */
static bool stop_session(worker_t *w)
{
    pthread_mutex_lock(&w->lock);
    session_t *s = w->session;
    pthread_mutex_unlock(&w->lock);
    if (!s) return true;
    char why[256];
    if (!session_stop(s, why, sizeof(why))) {
        pthread_mutex_lock(&w->lock);
        yusb_fence_hold(&w->fence, why);
        pthread_mutex_unlock(&w->lock);
        w->stop_retry_at = wk_now_ms() + w->stop_backoff_ms;
        w->stop_backoff_ms = w->stop_backoff_ms * 2 > STOP_RETRY_MAX_MS ? STOP_RETRY_MAX_MS
                                                                          : w->stop_backoff_ms * 2;
        wlogf(LOG_ERROR, "%s", why);
        return false;
    }
    session_free(s);
    pthread_mutex_lock(&w->lock);
    w->session = NULL;
    w->built = false;
    w->have_view = false;
    pthread_mutex_unlock(&w->lock);
    w->stop_retry_at = 0;
    w->stop_backoff_ms = STOP_RETRY_MIN_MS;
    return true;
}

static void apply(worker_t *w, const yctl_request_t *route)
{
    pthread_mutex_lock(&w->apply_lock);
    pthread_mutex_lock(&w->lock);
    bool newer = yusb_fence_newer(&w->fence, route->generation);
    pthread_mutex_unlock(&w->lock);
    if (newer && stop_session(w)) {
        pthread_mutex_lock(&w->lock);
        yusb_fence_adopt(&w->fence, route->generation, route->has_target ? &route->target : NULL);
        pthread_mutex_unlock(&w->lock);
        w->cfg.policy.yx_levers = w->yx_levers;   /* a new route offers YX again */
    }
    pthread_mutex_unlock(&w->apply_lock);
    wake();
}

/* --- supervision (worker.step) ---------------------------------------------- */

static void step(worker_t *w)
{
    if (pthread_mutex_trylock(&w->apply_lock)) return;   /* a route change is in progress */
    uint64_t now = wk_now_ms();
    pthread_mutex_lock(&w->lock);
    session_t *s = w->session;
    if (!s) {
        if (yusb_fence_due(&w->fence, false, now)) {
            w->session = session_start(&w->cfg, &w->fence.target, wlog);
            w->built = false;
            if (!w->session) yusb_fence_build_failed(&w->fence, YUSB_BUILD_RETRY, "out of memory", now);
        }
        pthread_mutex_unlock(&w->lock);
        pthread_mutex_unlock(&w->apply_lock);
        return;
    }
    pthread_mutex_unlock(&w->lock);
    session_view_t view;
    session_view(s, &view);
    pthread_mutex_lock(&w->lock);
    if (view.built && !w->built) {
        w->built = true;
        yusb_fence_built(&w->fence);
    }
    if (w->built) {
        w->view = view;
        w->have_view = true;
    }
    bool built = w->built;
    pthread_mutex_unlock(&w->lock);
    if (!view.ended) {
        if (built) {
            pthread_mutex_lock(&w->lock);
            yusb_fence_observe(&w->fence, yusb_wstate_from_name(view.status.state),
                               view.rejected_text, view.error);
            pthread_mutex_unlock(&w->lock);
        }
        pthread_mutex_unlock(&w->apply_lock);
        return;
    }
    if (wk_now_ms() < w->stop_retry_at) {      /* unconfirmed teardown: backing off */
        pthread_mutex_unlock(&w->apply_lock);
        return;
    }
    bool stopped = stop_session(w);
    if (stopped && view.yx_unavailable && w->cfg.policy.yx_levers) {
        /* The agreed YX codec could not be allocated: reconnect without the
         * offer until the next route change. */
        w->cfg.policy.yx_levers = 0;
        wlog(LOG_WARNING, "YX unavailable: reconnecting without the YX offer");
    }
    pthread_mutex_lock(&w->lock);
    now = wk_now_ms();
    if (!stopped) {
        /* fence held and logged by stop_session */
    } else if (built) {
        yusb_fence_ended(&w->fence, view.rejected ? view.rejected_text : NULL, view.reason, now);
        wlogf(LOG_INFO, "session ended: %s", view.reason);
    } else {
        yusb_fence_build_failed(&w->fence, view.rejected ? YUSB_BUILD_REJECTED : YUSB_BUILD_RETRY,
                                view.reason, now);
        wlogf(LOG_WARNING, "session attempt failed: %s", view.reason);
    }
    pthread_mutex_unlock(&w->lock);
    pthread_mutex_unlock(&w->apply_lock);
}

/* --- control socket (worker.ControlServer) -------------------------------- */

/* Up to CTL_CONNS connections are served at once from one thread with poll.
 * Each has one deadline from accept to its complete request line, and when
 * every slot is taken a new connection replaces the oldest, so slow or idle
 * clients delay neither another client nor shutdown. A route change blocks
 * the thread while it tears down (bounded by the stop timeout), as the
 * reference's apply. */
#define CTL_CONNS 8
#define CTL_DEADLINE_MS 2000u
#define ACCEPT_PAUSE_MS 100u

typedef struct {
    int fd;                       /* -1: free */
    uint64_t deadline;
    size_t len;                   /* request bytes received */
    size_t out, sent;             /* reply length (0: still reading) and bytes sent */
    char in[YCTL_MAX_MESSAGE + 1];
    char reply[YCTL_MAX_MESSAGE + 1];
} conn_t;

typedef struct {
    worker_t *w;
    int fd;
    int stop[2];                  /* written at shutdown */
    int lock_fd;                  /* flock on PATH.lock, held while serving */
    dev_t dev;                    /* the socket file this worker bound */
    ino_t ino;
    char path[sizeof(((struct sockaddr_un *)0)->sun_path)];
    conn_t conns[CTL_CONNS];
} server_t;

static void conn_close(conn_t *c)
{
    close(c->fd);
    c->fd = -1;
}

static void conn_write(conn_t *c)
{
    while (c->sent < c->out) {
        ssize_t n = send(c->fd, c->reply + c->sent, c->out - c->sent, MSG_NOSIGNAL | MSG_DONTWAIT);
        if (n < 0 && errno == EINTR) continue;
        if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) return;   /* poll for POLLOUT */
        if (n <= 0) break;
        c->sent += (size_t)n;
    }
    conn_close(c);
}

/* A complete line, or `error`: reply as worker.ControlServer._handle. */
static void respond(worker_t *w, conn_t *c, const char *error)
{
    yctl_request_t request;
    yctl_error_t why;
    if (!error && yctl_parse_request(c->in, c->len, &request, &why)) error = yctl_error_text(why);
    if (error) {
        c->out = yctl_format_error(error, c->reply, sizeof(c->reply));
    } else {
        if (request.route) apply(w, &request);
        c->out = format_status(w, c->reply, sizeof(c->reply));
    }
    c->sent = 0;
    if (c->out) conn_write(c);
    else conn_close(c);
}

static void conn_read(worker_t *w, conn_t *c)
{
    ssize_t n = recv(c->fd, c->in + c->len, sizeof(c->in) - 1 - c->len, MSG_DONTWAIT);
    if (n < 0) {
        if (errno != EINTR && errno != EAGAIN && errno != EWOULDBLOCK) conn_close(c);
        return;
    }
    if (n == 0) {
        respond(w, c, "control peer closed before a full line");
        return;
    }
    char *nl = memchr(c->in + c->len, '\n', (size_t)n);
    c->len += (size_t)n;
    if (nl) {
        c->len = (size_t)(nl - c->in) + 1;
        respond(w, c, NULL);
    } else if (c->len >= sizeof(c->in) - 1) {
        respond(w, c, "control message too large");
    }
}

static void conn_ready(worker_t *w, conn_t *c)
{
    if (c->out) conn_write(c);
    else conn_read(w, c);
}

/* A free slot, or else the oldest connection's, closed unanswered. */
static conn_t *slot(server_t *srv)
{
    conn_t *oldest = &srv->conns[0];
    for (int k = 0; k < CTL_CONNS; ++k) {
        conn_t *c = &srv->conns[k];
        if (c->fd < 0) return c;
        if (c->deadline < oldest->deadline) oldest = c;
    }
    conn_close(oldest);
    return oldest;
}

/* Accept every pending connection. Running out of descriptors (EMFILE,
 * ENFILE) or memory pauses accepting briefly, logged once per streak;
 * pending clients wait in the backlog and open connections are served. */
static void accept_all(server_t *srv, uint64_t *accept_after, int *accept_errno)
{
    for (;;) {
        int fd = accept4(srv->fd, NULL, NULL, SOCK_CLOEXEC | SOCK_NONBLOCK);
        if (fd >= 0) {
            conn_t *c = slot(srv);
            c->fd = fd;
            c->deadline = wk_now_ms() + CTL_DEADLINE_MS;
            c->len = c->out = c->sent = 0;
            *accept_errno = 0;
            continue;
        }
        int err = errno;
        if (err == EINTR || err == ECONNABORTED) continue;
        if (err == EAGAIN || err == EWOULDBLOCK) return;
        if (err != *accept_errno)
            wlogf(LOG_ERROR, "control socket accept: %s (retrying every %u ms)", strerror(err),
                  ACCEPT_PAUSE_MS);
        *accept_errno = err;
        *accept_after = wk_now_ms() + ACCEPT_PAUSE_MS;
        return;
    }
}

static void *serve(void *arg)
{
    server_t *srv = arg;
    uint64_t accept_after = 0;
    int accept_errno = 0;
    block_termination(NULL);
    pthread_setname_np(pthread_self(), "yw-control");
    for (;;) {
        struct pollfd pfd[2 + CTL_CONNS];
        uint64_t now = wk_now_ms(), next = UINT64_MAX;
        bool accepting = now >= accept_after;
        if (!accepting) next = accept_after;
        pfd[0] = (struct pollfd){srv->stop[0], POLLIN, 0};
        pfd[1] = (struct pollfd){accepting ? srv->fd : -1, POLLIN, 0};
        for (int k = 0; k < CTL_CONNS; ++k) {
            conn_t *c = &srv->conns[k];
            pfd[2 + k] = (struct pollfd){c->fd, (short)(c->out ? POLLOUT : POLLIN), 0};
            if (c->fd >= 0 && c->deadline < next) next = c->deadline;
        }
        int timeout = next == UINT64_MAX ? -1 : next <= now ? 0 : (int)(next - now);
        if (poll(pfd, 2 + CTL_CONNS, timeout) < 0 && errno != EINTR) {
            wlogf(LOG_ERROR, "control socket poll: %s", strerror(errno));
            usleep(ACCEPT_PAUSE_MS * 1000u);
            continue;
        }
        if (pfd[0].revents) break;
        for (int k = 0; k < CTL_CONNS; ++k)
            if (srv->conns[k].fd >= 0 && pfd[2 + k].revents) conn_ready(srv->w, &srv->conns[k]);
        /* Past its deadline: one last look (a request may have arrived while
         * a route change blocked this thread), then drop it unanswered, as
         * the reference's socket timeout does. */
        now = wk_now_ms();
        for (int k = 0; k < CTL_CONNS; ++k) {
            conn_t *c = &srv->conns[k];
            if (c->fd < 0 || now < c->deadline) continue;
            conn_ready(srv->w, c);
            if (c->fd >= 0) conn_close(c);
        }
        if (pfd[1].revents) accept_all(srv, &accept_after, &accept_errno);
    }
    for (int k = 0; k < CTL_CONNS; ++k)
        if (srv->conns[k].fd >= 0) conn_close(&srv->conns[k]);
    return NULL;
}

/* Bind the control socket. Starts on one path are serialized by an flock on
 * PATH.lock, held for the process lifetime, so a second worker can never
 * see a socket between bind and listen as stale. Then a socket file left by
 * a dead worker is replaced; one a live worker answers on is not
 * (-EADDRINUSE), so the first keeps serving the agent; anything else at the
 * path is refused (-ENOTSOCK) and left alone. */
static int listen_control(server_t *srv, const char *path)
{
    struct sockaddr_un addr = {.sun_family = AF_UNIX};
    if (strlen(path) >= sizeof(addr.sun_path)) return -ENAMETOOLONG;
    snprintf(addr.sun_path, sizeof(addr.sun_path), "%s", path);
    snprintf(srv->path, sizeof(srv->path), "%s", path);
    for (int k = 0; k < CTL_CONNS; ++k) srv->conns[k].fd = -1;
    char lock[sizeof(srv->path) + 8];
    snprintf(lock, sizeof(lock), "%s.lock", path);
    srv->lock_fd = open(lock, O_RDWR | O_CREAT | O_CLOEXEC | O_NOFOLLOW, 0660);
    if (srv->lock_fd < 0) return -errno;
    if (flock(srv->lock_fd, LOCK_EX | LOCK_NB)) {
        int err = errno;
        close(srv->lock_fd);
        return err == EWOULDBLOCK ? -EADDRINUSE : -err;
    }
    struct stat st;
    if (!lstat(path, &st)) {
        if (!S_ISSOCK(st.st_mode)) return -ENOTSOCK;
        int probe = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
        if (probe < 0) return -errno;
        int rc = connect(probe, (struct sockaddr *)&addr, sizeof(addr)), err = errno;
        close(probe);
        if (!rc || err == EAGAIN || err == EINPROGRESS) return -EADDRINUSE;
        if (err != ECONNREFUSED && err != ENOENT) return -err;
        unlink(path);
    }
    if (pipe2(srv->stop, O_CLOEXEC)) return -errno;
    int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
    if (fd < 0 || bind(fd, (struct sockaddr *)&addr, sizeof(addr)) || chmod(path, 0660) ||
        listen(fd, 16) || lstat(path, &st)) {
        int err = errno;
        if (fd >= 0) close(fd);
        return -err;
    }
    srv->dev = st.st_dev;
    srv->ino = st.st_ino;
    srv->fd = fd;
    return 0;
}

/* Stop serving: wake the thread, join it, then close (no descriptor reuse
 * while it runs). Unlink the path only while it is still our socket. */
static void close_control(server_t *srv, pthread_t thread)
{
    if (write(srv->stop[1], "x", 1) < 0) { /* the pipe is ours: cannot fail usefully */ }
    shutdown(srv->fd, SHUT_RDWR);
    pthread_join(thread, NULL);
    close(srv->fd);
    close(srv->stop[0]);
    close(srv->stop[1]);
    struct stat st;
    if (!lstat(srv->path, &st) && st.st_dev == srv->dev && st.st_ino == srv->ino) unlink(srv->path);
    close(srv->lock_fd);   /* the lock file stays: unlinking it would race a starting worker */
}

/* --- command line ------------------------------------------------------------ */

static void on_usr1(int sig) { (void)sig; /* only interrupts blocked Raw Gadget ioctls (EINTR) */ }

static void on_term(int sig)
{
    (void)sig;
    stop_requested = 1;
    wake();
}

static int install(int sig, void (*fn)(int))
{
    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = fn;
    sigemptyset(&sa.sa_mask);
    sa.sa_flags = 0;            /* no SA_RESTART: a blocked ioctl returns EINTR */
    return sigaction(sig, &sa, NULL);
}

static void usage(FILE *f)
{
    fputs("usage: yoke-gadget-worker --udc UDC [--socket PATH] [--raw-gadget PATH]\n"
          "         [--gadget-driver NAME] [--power-ma N] [--max-in-endpoints N]\n"
          "         [--max-out-endpoints N] [--in-depth N] [--out-window N]\n"
          "         [--target HOST:PORT/BUSID] [--source IPV4] [--log-level LEVEL]\n"
          "  LEVEL: DEBUG, INFO, WARN(ING), ERROR, CRITICAL, FATAL or NOTSET\n"
          "  native only: [--bot-write-behind N] [--yx fill,lz4,cache] [--yx-cache N]\n"
          "               [--max-transfer N] [--udc-sysfs DIR]\n"
          "Present one remote USB/IP device through the local UDC (ADRs 0021, 0022, 0027).\n",
          f);
}

static bool number(const char *text, long lo, long hi, long *out)
{
    char *end;
    errno = 0;
    long v = strtol(text, &end, 10);
    if (errno || end == text || *end || v < lo || v > hi) return false;
    *out = v;
    return true;
}

static int parse_levers(const char *text, uint8_t *levers)
{
    *levers = 0;
    char copy[64];
    snprintf(copy, sizeof(copy), "%s", text);
    for (char *save = NULL, *tok = strtok_r(copy, ",", &save); tok; tok = strtok_r(NULL, ",", &save)) {
        if (!strcmp(tok, "fill")) *levers |= YUSB_YX_FILL;
        else if (!strcmp(tok, "lz4")) *levers |= YUSB_YX_LZ4;
        else if (!strcmp(tok, "cache")) *levers |= YUSB_YX_CACHE;
        else if (!strcmp(tok, "none")) *levers = 0;
        else return -1;
    }
    return 0;
}

/* worker._parse_target: HOST:PORT/BUSID, host may be [bracketed]. */
static bool parse_target(const char *text, const char *source, yusb_target_t *t)
{
    memset(t, 0, sizeof(*t));
    const char *slash = strrchr(text, '/');
    if (!slash) return false;
    const char *colon = slash;
    while (colon > text && *colon != ':') colon--;
    if (*colon != ':') return false;
    const char *host = text;
    size_t hlen = (size_t)(colon - text);
    if (hlen >= 2 && host[0] == '[' && host[hlen - 1] == ']') { host++; hlen -= 2; }
    char port[8];
    size_t plen = (size_t)(slash - colon - 1);
    if (!plen || plen >= sizeof(port)) return false;
    memcpy(port, colon + 1, plen);
    port[plen] = 0;
    long p;
    if (!number(port, 1, 65535, &p)) return false;
    t->port = (uint16_t)p;
    const char *busid = slash + 1;
    if (yusb_validate_host(host, hlen, t->host) || yusb_validate_busid(busid, strlen(busid)) ||
        strlen(busid) >= sizeof(t->busid))
        return false;
    snprintf(t->busid, sizeof(t->busid), "%s", busid);
    if (source && yusb_validate_ipv4(source, strlen(source), t->source)) return false;
    return true;
}

int main(int argc, char **argv)
{
    enum { O_UDC = 256, O_SOCKET, O_RAW, O_DRIVER, O_POWER, O_MAXIN, O_MAXOUT, O_DEPTH,
           O_WINDOW, O_TARGET, O_SOURCE, O_LEVEL, O_BOTWB, O_YX, O_YXCACHE, O_MAXXFER, O_SYSFS };
    static const struct option options[] = {
        {"udc", required_argument, 0, O_UDC}, {"socket", required_argument, 0, O_SOCKET},
        {"raw-gadget", required_argument, 0, O_RAW}, {"gadget-driver", required_argument, 0, O_DRIVER},
        {"power-ma", required_argument, 0, O_POWER}, {"max-in-endpoints", required_argument, 0, O_MAXIN},
        {"max-out-endpoints", required_argument, 0, O_MAXOUT}, {"in-depth", required_argument, 0, O_DEPTH},
        {"out-window", required_argument, 0, O_WINDOW}, {"target", required_argument, 0, O_TARGET},
        {"source", required_argument, 0, O_SOURCE}, {"log-level", required_argument, 0, O_LEVEL},
        {"bot-write-behind", required_argument, 0, O_BOTWB}, {"yx", required_argument, 0, O_YX},
        {"yx-cache", required_argument, 0, O_YXCACHE}, {"max-transfer", required_argument, 0, O_MAXXFER},
        {"udc-sysfs", required_argument, 0, O_SYSFS}, {"help", no_argument, 0, 'h'}, {0, 0, 0, 0},
    };
    worker_t w;
    memset(&w, 0, sizeof(w));
    yusb_policy_defaults(&w.cfg.policy);
    /* Measured (doc/design/USB-DATAPATH.md): 32 KiB BOT URBs overlap device,
     * network and gadget transfers better than the reference's 64 KiB, at
     * slightly more CPU per MiB. --max-transfer 65536 restores parity. */
    w.cfg.policy.max_transfer = 32768;
    snprintf(w.cfg.raw_gadget, sizeof(w.cfg.raw_gadget), "/dev/raw-gadget");
    snprintf(w.cfg.udc_sysfs, sizeof(w.cfg.udc_sysfs), "/sys/class/udc");
    w.cfg.connect_timeout_ms = 3000;   /* min(import_timeout, 3 s) */
    const char *socket_path = "/run/yoke-gadget/control.sock", *target = NULL, *source = NULL;
    int opt;
    long v;
    while ((opt = getopt_long(argc, argv, "h", options, NULL)) != -1) {
        bool ok = true;
        switch (opt) {
        case O_UDC: ok = strlen(optarg) < sizeof(w.cfg.udc); snprintf(w.cfg.udc, sizeof(w.cfg.udc), "%s", optarg); break;
        case O_SOCKET: socket_path = optarg; break;
        case O_RAW: snprintf(w.cfg.raw_gadget, sizeof(w.cfg.raw_gadget), "%s", optarg); break;
        case O_DRIVER: snprintf(w.cfg.gadget_driver, sizeof(w.cfg.gadget_driver), "%s", optarg); break;
        case O_POWER: if ((ok = number(optarg, 0, 500, &v))) w.cfg.policy.power_ma = (uint16_t)v; break;
        case O_MAXIN: if ((ok = number(optarg, 0, 15, &v))) w.cfg.policy.budget.max_in = (uint8_t)v; break;
        case O_MAXOUT: if ((ok = number(optarg, 0, 15, &v))) w.cfg.policy.budget.max_out = (uint8_t)v; break;
        case O_DEPTH: if ((ok = number(optarg, 1, 64, &v))) w.cfg.policy.in_depth_bulk = (uint8_t)v; break;
        case O_WINDOW: if ((ok = number(optarg, 1, 64, &v))) w.cfg.policy.out_window = (uint8_t)v; break;
        case O_TARGET: target = optarg; break;
        case O_SOURCE: source = optarg; break;
        case O_LEVEL: ok = parse_level(optarg, &log_level); break;
        case O_BOTWB: if ((ok = number(optarg, 1, 64, &v))) w.cfg.policy.bot_out_window = (uint8_t)v; break;
        case O_YX: ok = !parse_levers(optarg, &w.cfg.policy.yx_levers); break;
        case O_YXCACHE: if ((ok = number(optarg, 0, YUSB_YX_MAX_CACHE_BLOCKS, &v))) w.cfg.policy.yx_cache_blocks = (uint16_t)v; break;
        case O_MAXXFER: if ((ok = number(optarg, 512, 1 << 20, &v))) w.cfg.policy.max_transfer = (uint32_t)v; break;
        case O_SYSFS: snprintf(w.cfg.udc_sysfs, sizeof(w.cfg.udc_sysfs), "%s", optarg); break;
        case 'h': usage(stdout); return 0;
        default: usage(stderr); return 2;
        }
        if (!ok) {
            fprintf(stderr, "yoke-gadget-worker: invalid value for --%s: %s\n", options[opt - O_UDC].name, optarg);
            return 2;
        }
    }
    char why[200];
    if (!w.cfg.udc[0] || optind != argc) { usage(stderr); return 2; }
    if (yusb_policy_check(&w.cfg.policy, why, sizeof(why))) {
        fprintf(stderr, "yoke-gadget-worker: %s\n", why);
        return 2;
    }
    w.yx_levers = w.cfg.policy.yx_levers;
    w.stop_backoff_ms = STOP_RETRY_MIN_MS;
    /* Before any thread exists, so every thread inherits the mask; the main
     * loop takes the signals in ppoll with `waiting`. */
    sigset_t waiting;
    block_termination(&waiting);
    sigdelset(&waiting, SIGTERM);
    sigdelset(&waiting, SIGINT);
    if (pipe2(wake_fd, O_CLOEXEC | O_NONBLOCK) || install(SIGUSR1, on_usr1) ||
        install(SIGTERM, on_term) || install(SIGINT, on_term) || signal(SIGPIPE, SIG_IGN) == SIG_ERR) {
        perror("yoke-gadget-worker: setup");
        return 1;
    }
    pthread_mutex_init(&w.apply_lock, NULL);
    pthread_mutex_init(&w.lock, NULL);
    yusb_fence_cfg_t fcfg = {250, 2000, 30000};
    yusb_fence_init(&w.fence, &fcfg);
    static server_t srv;            /* 260 KiB of connection buffers */
    srv.w = &w;
    srv.fd = -1;
    pthread_t server_thread;
    bool serving = false;
    if (target) {
        yctl_request_t route = {.route = true, .generation = 1, .has_target = true};
        if (!parse_target(target, source, &route.target)) {
            fprintf(stderr, "yoke-gadget-worker: bad --target %s\n", target);
            return 2;
        }
        apply(&w, &route);
    } else {
        int rc = listen_control(&srv, socket_path);
        if (rc == -EADDRINUSE) {
            fprintf(stderr, "yoke-gadget-worker: control socket %s: another worker is serving it\n",
                    socket_path);
            return 1;
        }
        if (rc == -ENOTSOCK) {
            fprintf(stderr, "yoke-gadget-worker: control socket %s: the path exists and is not a "
                            "socket; remove it\n", socket_path);
            return 1;
        }
        if (rc < 0 || pthread_create(&server_thread, NULL, serve, &srv)) {
            fprintf(stderr, "yoke-gadget-worker: control socket %s: %s\n", socket_path, strerror(rc < 0 ? -rc : EAGAIN));
            return 1;
        }
        serving = true;
    }
    wlogf(LOG_INFO, "native worker on UDC %s (%s)", w.cfg.udc, target ? "standalone" : socket_path);
    while (!stop_requested) {
        step(&w);
        struct pollfd pfd = {wake_fd[0], POLLIN, 0};
        struct timespec period = {0, 100 * 1000000L};
        if (ppoll(&pfd, 1, &period, &waiting) > 0) {
            char drain[64];
            while (read(wake_fd[0], drain, sizeof(drain)) > 0) {}
        }
    }
    if (serving) close_control(&srv, server_thread);
    pthread_mutex_lock(&w.apply_lock);
    bool ok = stop_session(&w);      /* logs an unconfirmed teardown */
    pthread_mutex_unlock(&w.apply_lock);
    return ok ? 0 : 1;
}
