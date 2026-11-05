"""Serve a :class:`FakeRawGadgetKernel` to the native worker over a Unix socket.

The native worker's simulation build (``yoke-gadget-worker-sim``) links
``native/yusb/sim/sys_rpc.c`` instead of libc for open/close/ioctl and UDC
sysfs reads, and forwards each call here. One handler thread serves each
client connection, so a call blocks inside the fake kernel exactly like the
Python backend's injected ioctl, and ``interrupt`` reaches the right
thread (a signal that arrives while it is not blocked is lost, as in the
kernel). Wire format: see the C file. Simulated evidence only.
"""

from __future__ import annotations

import contextlib
import errno
import os
import socket
import struct
import threading
from pathlib import Path

from tests.usbsim.udc import FakeRawGadgetKernel

_REQ = struct.Struct("=IiQIIQ")
_REP = struct.Struct("=qII")
HELLO, OPEN, CLOSE, IOCTL, READ_TEXT, INTERRUPT = range(1, 7)


def _recv_exact(conn: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = conn.recv(size - len(data))
        if not chunk:
            raise ConnectionError("rpc peer closed")
        data += chunk
    return data


class RawGadgetRpcServer:
    def __init__(self, kernel: FakeRawGadgetKernel, path: Path) -> None:
        self.kernel = kernel
        self.path = path
        self._lock = threading.Lock()
        self._threads: dict[int, int] = {}
        self._next = 1
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(str(path))
        self._listener.listen(64)
        self._conns: list[socket.socket] = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            with self._lock:
                self._conns.append(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        try:
            while True:
                op, fd, request, kind, length, value = _REQ.unpack(
                    _recv_exact(conn, _REQ.size)
                )
                payload = _recv_exact(conn, length) if length else b""
                result, data = self._dispatch(op, fd, request, kind, payload, value)
                conn.sendall(_REP.pack(result, len(data), 0) + data)
        except (ConnectionError, OSError):
            pass
        finally:
            with contextlib.suppress(OSError):
                conn.close()

    def _dispatch(
        self, op: int, fd: int, request: int, kind: int, payload: bytes, value: int
    ) -> tuple[int, bytes]:
        kernel = self.kernel
        if op == HELLO:
            with self._lock:
                ident = self._next
                self._next += 1
                self._threads[ident] = threading.get_ident()
            return ident, b""
        if op == OPEN:
            try:
                return kernel.open(payload.decode(), os.O_RDWR), b""
            except OSError as exc:
                return -(exc.errno or errno.EIO), b""
        if op == CLOSE:
            with contextlib.suppress(OSError):
                kernel.close(fd)
            return 0, b""
        if op == IOCTL:
            if kind:
                buf = bytearray(payload)
                return kernel.ioctl(fd, request, buf), bytes(buf)
            return kernel.ioctl(fd, request, value), b""
        if op == READ_TEXT:
            try:
                return 0, kernel.read_text(Path(payload.decode())).encode()
            except OSError as exc:
                return -(exc.errno or errno.EIO), b""
        if op == INTERRUPT:
            with self._lock:
                thread = self._threads.get(value)
            if thread is not None:
                kernel.interrupt(thread)
            return 0, b""
        return -errno.ENOSYS, b""

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._listener.shutdown(socket.SHUT_RDWR)  # wakes accept()
        with contextlib.suppress(OSError):
            self._listener.close()
        with self._lock:
            conns = list(self._conns)
        for conn in conns:
            with contextlib.suppress(OSError):
                conn.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                conn.close()
        with contextlib.suppress(OSError):
            self.path.unlink()
