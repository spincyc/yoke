#pragma once
/* The network transport between one TCP socket and a sans-I/O USB/IP core
 * (native/yusb yusb_proxy or yusb_export): received bytes land directly in
 * the core's buffers (rx_space/rx_done) and its pending segments go out
 * with one sendmsg (tx_iov/tx_done). Nonblocking; POSIX sockets, so the
 * same code runs over lwIP on the box and over loopback in the tests. */
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "yusb.h"

typedef struct {
    size_t (*rx_space)(void *core, uint8_t **buf);
    int (*rx_done)(void *core, size_t n, uint64_t now_ms);
    size_t (*tx_iov)(const void *core, yusb_iov_t *iov, size_t max);
    void (*tx_done)(void *core, size_t n);
} box_core_io_t;

enum {
    BOX_PUMP_READ = 1,    /* the core has room: wait for readable */
    BOX_PUMP_WRITE = 2,   /* bytes still to send: wait for writable */
    BOX_PUMP_CLOSED = 4,  /* peer closed */
    BOX_PUMP_FAILED = 8,  /* socket error, or the core refused the bytes */
};

/* Move bytes both ways until neither direction progresses. Would block:
 * READ and/or WRITE, what to wait for; 0 when the core has neither room nor
 * bytes to send (its own progress pumps again: a readable socket must not
 * be watched meanwhile). Otherwise CLOSED or FAILED. */
int box_pump(int fd, const box_core_io_t *io, void *core, uint64_t now_ms);
/* TCP_NODELAY (USB/IP is request/response; see USB-DATAPATH.md) and
 * nonblocking mode. 0, or -1 with errno. */
int box_pump_prepare(int fd);
