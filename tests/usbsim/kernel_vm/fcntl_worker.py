"""Negative control for the VM test: the worker with ``fcntl.ioctl`` dispatch.

This reproduces the reviewed defect on the real Raw Gadget driver: CPython
retries ``fcntl.ioctl`` on EINTR when no Python handler raised, and signal
handlers never run off the main thread, so SIGUSR1 cannot cancel a blocked
EVENT_FETCH/EP_READ and teardown cannot be confirmed. Never use in production.
"""

from __future__ import annotations

import fcntl
import sys

from yoke.usb import rawgadget, worker


def fcntl_ioctl(_self: object, fd: int, request: int, arg: int | bytearray) -> int:
    try:
        if isinstance(arg, int):
            return fcntl.ioctl(fd, request, arg)
        return int(fcntl.ioctl(fd, request, arg, True))
    except OSError as exc:
        return -(exc.errno or 5)


rawgadget.LibcIoctl.__call__ = fcntl_ioctl  # type: ignore[method-assign,assignment]

if __name__ == "__main__":
    raise SystemExit(worker.main(sys.argv[1:]))
