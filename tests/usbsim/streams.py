"""Byte streams for the simulation: in-memory pairs and a delaying TCP relay.

Both inject one-way latency and non-reordering jitter (TCP preserves byte
order, so a later chunk is never delivered before an earlier one) and can
reset a connection. :class:`MemoryStream` implements the subset of the
socket API the USB/IP client and exporter use: ``sendall``, ``recv_into``,
``settimeout``, ``shutdown`` and ``close``.
"""

from __future__ import annotations

import contextlib
import random
import select
import socket
import struct
import threading
import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class Link:
    """Shared fault and delay settings for one connection."""

    latency: float = 0.0
    jitter: float = 0.0
    seed: int = 0
    rng: random.Random = field(init=False)

    def __post_init__(self) -> None:
        self.rng = random.Random(self.seed)

    def delay(self) -> float:
        if self.jitter <= 0:
            return self.latency
        return self.latency + self.rng.uniform(0.0, self.jitter)


class _Channel:
    def __init__(self) -> None:
        self.cond = threading.Condition()
        self.chunks: deque[tuple[float, memoryview]] = deque()
        self.last = 0.0
        self.closed = False
        self.reset = False


class MemoryStream:
    """One end of an in-memory, ordered, delayed byte stream."""

    def __init__(self, rx: _Channel, tx: _Channel, link: Link) -> None:
        self._rx = rx
        self._tx = tx
        self._link = link
        self._timeout: float | None = None
        self.bytes_sent = 0
        self.sends = 0

    def settimeout(self, timeout: float | None) -> None:
        self._timeout = timeout

    def sendall(self, data: bytes | bytearray | memoryview) -> None:
        chunk = memoryview(bytes(data))
        with self._tx.cond:
            if self._tx.reset:
                raise ConnectionResetError("simulated connection reset")
            if self._tx.closed:
                raise BrokenPipeError("simulated stream closed")
            now = time.monotonic()
            due = max(self._tx.last, now + self._link.delay())
            self._tx.last = due
            self._tx.chunks.append((due, chunk))
            self.bytes_sent += len(chunk)
            self.sends += 1
            self._tx.cond.notify_all()

    def recv_into(self, buffer: bytearray | memoryview, nbytes: int = 0) -> int:
        view = memoryview(buffer)
        want = nbytes or len(view)
        deadline = None if self._timeout is None else time.monotonic() + self._timeout
        rx = self._rx
        with rx.cond:
            while True:
                if rx.reset:
                    raise ConnectionResetError("simulated connection reset")
                now = time.monotonic()
                if rx.chunks and rx.chunks[0][0] <= now:
                    break
                if rx.closed and not rx.chunks:
                    return 0
                if deadline is not None and now >= deadline:
                    raise TimeoutError("simulated recv timeout")
                waits = [rx.chunks[0][0] - now] if rx.chunks else []
                if deadline is not None:
                    waits.append(deadline - now)
                rx.cond.wait(min(waits) if waits else None)
            got = 0
            while got < want and rx.chunks and rx.chunks[0][0] <= now:
                due, chunk = rx.chunks[0]
                take = min(want - got, len(chunk))
                view[got : got + take] = chunk[:take]
                got += take
                if take == len(chunk):
                    rx.chunks.popleft()
                else:
                    rx.chunks[0] = (due, chunk[take:])
            return got

    def shutdown(self, how: int = socket.SHUT_RDWR) -> None:
        self.close()

    def close(self) -> None:
        for channel in (self._tx, self._rx):
            with channel.cond:
                channel.closed = True
                channel.cond.notify_all()


def memory_pair(
    latency: float = 0.0, jitter: float = 0.0, seed: int = 0
) -> tuple[MemoryStream, MemoryStream, MemoryLink]:
    link = MemoryLink(latency, jitter, seed)
    a_to_b, b_to_a = _Channel(), _Channel()
    link.channels = (a_to_b, b_to_a)
    return MemoryStream(b_to_a, a_to_b, link), MemoryStream(a_to_b, b_to_a, link), link


@dataclass
class MemoryLink(Link):
    channels: tuple[_Channel, _Channel] | None = None

    def reset(self) -> None:
        """Abort both directions; pending and future I/O raise ECONNRESET."""
        assert self.channels is not None
        for channel in self.channels:
            with channel.cond:
                channel.reset = True
                channel.chunks.clear()
                channel.cond.notify_all()


class TcpRelay:
    """A 127.0.0.1 TCP relay that delays each direction and can send RST."""

    def __init__(
        self, target: tuple[str, int], *, latency: float = 0.0, jitter: float = 0.0
    ) -> None:
        self._target = target
        self._link = Link(latency, jitter)
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.address: tuple[str, int] = self._listener.getsockname()[:2]
        self._sockets: list[socket.socket] = []
        self._lock = threading.Lock()
        self._stopped = False
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                client, _ = self._listener.accept()
            except OSError:
                return
            try:
                upstream = socket.create_connection(self._target, timeout=5)
            except OSError:
                client.close()
                continue
            upstream.settimeout(None)
            for sock in (client, upstream):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self._lock:
                self._sockets += [client, upstream]
            for src, dst in ((client, upstream), (upstream, client)):
                threading.Thread(
                    target=self._pump, args=(src, dst), daemon=True
                ).start()

    def _pump(self, src: socket.socket, dst: socket.socket) -> None:
        queue: deque[tuple[float, bytes]] = deque()
        cond = threading.Condition()
        done = threading.Event()

        def deliver() -> None:
            while True:
                with cond:
                    while not queue and not done.is_set():
                        cond.wait()
                    if not queue:
                        break
                    due, data = queue[0]
                    now = time.monotonic()
                    if due > now:
                        cond.wait(due - now)
                        continue
                    queue.popleft()
                try:
                    dst.sendall(data)
                except OSError:
                    break
            with contextlib.suppress(OSError):
                dst.shutdown(socket.SHUT_WR)

        sender = threading.Thread(target=deliver, daemon=True)
        sender.start()
        last = 0.0
        while True:
            try:
                ready, _, _ = select.select([src], [], [], 0.5)
                if not ready:
                    if self._stopped:
                        break
                    continue
                data = src.recv(65536)
            except OSError:
                break
            if not data:
                break
            with cond:
                last = max(last, time.monotonic() + self._link.delay())
                queue.append((last, data))
                cond.notify_all()
        done.set()
        with cond:
            cond.notify_all()

    def reset_all(self) -> None:
        """Abort every relayed connection with a TCP RST (SO_LINGER 0)."""
        with self._lock:
            sockets, self._sockets = self._sockets, []
        for sock in sockets:
            with contextlib.suppress(OSError):
                sock.setsockopt(
                    socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
                )
                sock.close()

    def close(self) -> None:
        self._stopped = True
        self._listener.close()
        self.reset_all()
