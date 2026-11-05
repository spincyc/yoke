/* Production kernel touchpoints (see sys.h). */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <sys/ioctl.h>
#include <unistd.h>
#include "sys.h"

static int sys_open(const char *path)
{
    int fd = open(path, O_RDWR | O_CLOEXEC);
    return fd < 0 ? -errno : fd;
}

static void sys_close(int fd) { close(fd); }

static long sys_ioctl(int fd, unsigned long request, void *arg)
{
    long rc = ioctl(fd, request, arg);
    return rc < 0 ? -errno : rc;
}

static long sys_read_text(const char *path, char *out, size_t cap)
{
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) return -errno;
    ssize_t n = read(fd, out, cap ? cap - 1 : 0);
    int err = errno;
    close(fd);
    if (n < 0) return -err;
    if (cap) out[n] = 0;
    return n;
}

static const ysys_t linux_sys = {sys_open, sys_close, sys_ioctl, sys_read_text};

const ysys_t *ysys(void) { return &linux_sys; }
