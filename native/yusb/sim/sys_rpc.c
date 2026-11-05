/* Simulation kernel touchpoints (see linux/sys.h): every call is forwarded
 * to tests/usbsim/rpc.py, which runs it on the Python FakeRawGadgetKernel.
 * Linked into yoke-gadget-worker-sim only; the socket path comes from
 * $YOKE_GADGET_RPC. Each calling thread has its own connection, so a call
 * blocks in the fake kernel exactly as a real ioctl blocks. When a signal
 * interrupts the wait (EINTR), the shim asks the server, on a separate
 * connection, to interrupt that call; the call then returns -EINTR or its
 * real result, like raw_gadget's wait_for_completion_interruptible.
 *
 * Wire (host byte order): request {u32 op, i32 fd, u64 request, u32 kind,
 * u32 len, u64 value} + len bytes; reply {i64 result, u32 len} + len bytes.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <pthread.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>
#include "rawgadget.h"
#include "sys.h"

enum { R_HELLO = 1, R_OPEN, R_CLOSE, R_IOCTL, R_READ_TEXT, R_INTERRUPT };

typedef struct {
    uint32_t op;
    int32_t fd;
    uint64_t request;
    uint32_t kind;     /* 0: integer argument, 1: buffer */
    uint32_t len;
    uint64_t value;
} req_t;

typedef struct {
    int64_t result;
    uint32_t len;
    uint32_t pad;
} rep_t;

static _Thread_local int conn = -1;
static _Thread_local int64_t conn_id = -1;
static int control = -1;
static pthread_mutex_t control_lock = PTHREAD_MUTEX_INITIALIZER;

static int dial(void)
{
    const char *path = getenv("YOKE_GADGET_RPC");
    struct sockaddr_un addr = {.sun_family = AF_UNIX};
    if (!path || strlen(path) >= sizeof(addr.sun_path)) return -1;
    memcpy(addr.sun_path, path, strlen(path) + 1);
    int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (fd < 0) return -1;
    if (connect(fd, (struct sockaddr *)&addr, sizeof(addr))) {
        close(fd);
        return -1;
    }
    return fd;
}

static int send_all(int fd, const void *buf, size_t len)
{
    const uint8_t *p = buf;
    while (len) {
        ssize_t n = send(fd, p, len, MSG_NOSIGNAL);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return -1;
        p += n;
        len -= (size_t)n;
    }
    return 0;
}

/* Receive exactly len bytes; on EINTR ask the server to interrupt this
 * connection's call (once per signal, as the worker repeats signals). */
static int recv_all(int fd, void *buf, size_t len, bool interruptible)
{
    uint8_t *p = buf;
    while (len) {
        ssize_t n = recv(fd, p, len, 0);
        if (n < 0 && errno == EINTR) {
            if (interruptible) {
                req_t r = {R_INTERRUPT, 0, 0, 0, 0, (uint64_t)conn_id};
                pthread_mutex_lock(&control_lock);
                if (control < 0) control = dial();
                if (control >= 0) {
                    rep_t ack;
                    if (send_all(control, &r, sizeof(r)) || recv_all(control, &ack, sizeof(ack), false)) {
                        close(control);
                        control = -1;
                    }
                }
                pthread_mutex_unlock(&control_lock);
            }
            continue;
        }
        if (n <= 0) return -1;
        p += n;
        len -= (size_t)n;
    }
    return 0;
}

static int64_t call(req_t *r, const void *in, void *out, size_t out_cap, size_t *out_len)
{
    if (conn < 0) {
        conn = dial();
        if (conn < 0) return -EIO;
        req_t hello = {R_HELLO, 0, 0, 0, 0, 0};
        rep_t rep;
        if (send_all(conn, &hello, sizeof(hello)) || recv_all(conn, &rep, sizeof(rep), false)) return -EIO;
        conn_id = rep.result;
    }
    rep_t rep;
    if (send_all(conn, r, sizeof(*r)) || (r->len && send_all(conn, in, r->len)) ||
        recv_all(conn, &rep, sizeof(rep), r->op == R_IOCTL))
        return -EIO;
    size_t keep = rep.len < out_cap ? rep.len : out_cap;
    if (keep && recv_all(conn, out, keep, false)) return -EIO;
    for (size_t left = rep.len - keep; left;) {   /* drain anything beyond out_cap */
        uint8_t sink[256];
        size_t n = left < sizeof(sink) ? left : sizeof(sink);
        if (recv_all(conn, sink, n, false)) return -EIO;
        left -= n;
    }
    if (out_len) *out_len = keep;
    return rep.result;
}

static int rpc_open(const char *path)
{
    req_t r = {R_OPEN, 0, 0, 1, (uint32_t)strlen(path), 0};
    return (int)call(&r, path, NULL, 0, NULL);
}

static void rpc_close(int fd)
{
    req_t r = {R_CLOSE, fd, 0, 0, 0, 0};
    call(&r, NULL, NULL, 0, NULL);
}

/* Bytes a structured request carries (the ep_io/event length fields add
 * their payload); 0 for integer-argument requests. */
static size_t arg_size(unsigned long request, const uint8_t *arg)
{
    uint32_t length;
    switch (request) {
    case RG_IOCTL_INIT: return RG_INIT_SIZE;
    case RG_IOCTL_EP_ENABLE: return 9;
    case RG_IOCTL_EPS_INFO: return RG_EPS_MAX * RG_EP_INFO_SIZE;
    case RG_IOCTL_EVENT_FETCH:
    case RG_IOCTL_EP0_WRITE:
    case RG_IOCTL_EP0_READ:
    case RG_IOCTL_EP_WRITE:
    case RG_IOCTL_EP_READ:
        memcpy(&length, arg + 4, 4);
        return 8u + length;
    default:
        return 0;
    }
}

static long rpc_ioctl(int fd, unsigned long request, void *arg)
{
    size_t size = arg ? arg_size(request, arg) : 0;
    req_t r = {R_IOCTL, fd, request, size ? 1u : 0u, (uint32_t)size, size ? 0 : (uint64_t)(uintptr_t)arg};
    return (long)call(&r, arg, arg, size, NULL);
}

static long rpc_read_text(const char *path, char *out, size_t cap)
{
    req_t r = {R_READ_TEXT, 0, 0, 1, (uint32_t)strlen(path), 0};
    size_t got = 0;
    int64_t rc = call(&r, path, out, cap ? cap - 1 : 0, &got);
    if (cap) out[got] = 0;
    return rc < 0 ? (long)rc : (long)got;
}

static const ysys_t rpc_sys = {rpc_open, rpc_close, rpc_ioctl, rpc_read_text};

const ysys_t *ysys(void) { return &rpc_sys; }
