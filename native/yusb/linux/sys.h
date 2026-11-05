#pragma once
/* The worker's only kernel touchpoints for Raw Gadget and UDC sysfs.
 *
 * Production links sys_linux.c (libc). The simulation links
 * native/yusb/sim/sys_rpc.c, which forwards the same calls to the Python
 * FakeRawGadgetKernel (tests/usbsim), so the worker's struct packing,
 * error mapping and no-retry cancellation run unchanged against it, as
 * the Python RawGadgetBackend does with its injected ioctl.
 */
#include <stddef.h>

typedef struct {
    /* Open the Raw Gadget node read-write and blocking: fd or -errno. */
    int (*open)(const char *path);
    void (*close)(int fd);
    /* ioctl(2) called exactly once: never retried, EINTR included.
     * Returns the kernel result or -errno. `arg` is a pointer for
     * structured requests and an integer cast to void * otherwise. */
    long (*ioctl)(int fd, unsigned long request, void *arg);
    /* Read a small sysfs text attribute: bytes or -errno. */
    long (*read_text)(const char *path, char *out, size_t cap);
} ysys_t;

const ysys_t *ysys(void);
