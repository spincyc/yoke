#include "box_pump.h"
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/socket.h>
#include <sys/uio.h>

#define SEGMENTS 16

int box_pump_prepare(int fd)
{
    int on = 1, flags = fcntl(fd, F_GETFL, 0);
    if (flags < 0 || fcntl(fd, F_SETFL, flags | O_NONBLOCK) < 0) return -1;
    return setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &on, sizeof(on));
}

static bool blocked(void) { return errno == EAGAIN || errno == EWOULDBLOCK; }

int box_pump(int fd, const box_core_io_t *io, void *core, uint64_t now_ms)
{
    for (;;) {
        bool progress = false, pending = false;
        yusb_iov_t segments[SEGMENTS];
        size_t count = io->tx_iov(core, segments, SEGMENTS);
        if (count) {
            struct iovec iov[SEGMENTS];
            for (size_t i = 0; i < count; ++i) {
                iov[i].iov_base = (void *)segments[i].base;
                iov[i].iov_len = segments[i].len;
            }
            struct msghdr msg = {.msg_iov = iov, .msg_iovlen = count};
            ssize_t sent = sendmsg(fd, &msg, MSG_DONTWAIT);
            if (sent > 0) {
                io->tx_done(core, (size_t)sent);
                progress = true;
            } else if (sent < 0 && !blocked() && errno != EINTR) {
                return BOX_PUMP_FAILED;
            } else {
                pending = true;
            }
        }
        uint8_t *buf;
        size_t room = io->rx_space(core, &buf);
        if (room) {
            ssize_t got = recv(fd, buf, room, MSG_DONTWAIT);
            if (got > 0) {
                if (io->rx_done(core, (size_t)got, now_ms) < 0) return BOX_PUMP_FAILED;
                progress = true;
            } else if (got == 0) {
                return BOX_PUMP_CLOSED;
            } else if (!blocked() && errno != EINTR) {
                return BOX_PUMP_FAILED;
            }
        }
        if (!progress) return (room ? BOX_PUMP_READ : 0) | (pending ? BOX_PUMP_WRITE : 0);
    }
}
