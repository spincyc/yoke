#pragma once
#include <fcntl.h>
#include <stdint.h>

/* Socket I/O policy only; TLS remains wholly in Espressif/mbedTLS. Keeping
 * the socket nonblocking ensures a partial TLS record cannot trap one step
 * past the absolute monotonic deadline. Restore caller flags on every exit.
 * This exact function is also exercised with real mbedTLS on loopback. */
static int yoke_tls_handshake_deadline(
    int fd, void *context, int (*step)(void *), uint64_t (*now_us)(void),
    void (*wait_tick)(void), uint64_t timeout_us,
    int want_read, int want_write, int timeout_error)
{
    int flags = fcntl(fd, F_GETFL, 0);
    if (flags < 0 || fcntl(fd, F_SETFL, flags | O_NONBLOCK) < 0) return -1;
    uint64_t started = now_us();
    int ret;
    for (;;) {
        if (now_us() - started >= timeout_us) { ret = timeout_error; break; }
        ret = step(context);
        if (now_us() - started >= timeout_us) { ret = timeout_error; break; }
        if (ret != want_read && ret != want_write) break;
        wait_tick();
    }
    if (fcntl(fd, F_SETFL, flags) < 0) return -1;
    return ret;
}
