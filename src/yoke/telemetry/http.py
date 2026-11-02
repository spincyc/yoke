"""Explicit, bounded HTTP exposure of pre-rendered telemetry samples.

There is one serving thread and at most one active connection. A small socket
backlog bounds waiting clients, and an absolute deadline bounds both slow
headers and blocked responses. Scrapes never collect or serialize samples.
"""

from __future__ import annotations

import io
import ipaddress
import socket
import threading
import time
from collections.abc import Buffer
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import TracebackType
from typing import cast

from yoke.telemetry import prometheus
from yoke.telemetry.model import Snapshot

JSON_CONTENT_TYPE = "application/json; charset=utf-8"
# The full bounded fleet expands into several Prometheus series per device.
MAX_METRICS_BYTES = 16 * 1024 * 1024
REQUEST_DEADLINE_SECONDS = 1.0


@dataclass(frozen=True)
class CachedRepresentations:
    """A coherent pair of immutable representations of one sample."""

    json: bytes
    metrics: bytes


class SnapshotCache:
    """Render on updates and atomically publish a complete cached sample."""

    def __init__(self, initial: Snapshot) -> None:
        self._lock = threading.Lock()
        self.update(initial)

    def update(self, snapshot: Snapshot) -> None:
        """Validate and detach caller-owned mappings before rendering once."""
        detached = Snapshot.from_body(snapshot.to_body())
        representations = CachedRepresentations(
            json=detached.to_json(), metrics=prometheus.render(detached)
        )
        if len(representations.metrics) > MAX_METRICS_BYTES:
            raise ValueError("telemetry metrics exceed response size limit")
        with self._lock:
            self._snapshot = detached
            self._representations = representations

    def snapshot(self) -> Snapshot:
        """Return a detached model; callers cannot mutate the cached sample."""
        with self._lock:
            snapshot = self._snapshot
        return Snapshot.from_body(snapshot.to_body())

    def representations(self) -> CachedRepresentations:
        """Return cached bytes without encoding, rendering, or source reads."""
        with self._lock:
            return self._representations


class _Deadline:
    def __init__(self, connection: socket.socket) -> None:
        self.connection = connection
        self.expires = time.monotonic() + REQUEST_DEADLINE_SECONDS

    def apply(self) -> None:
        remaining = self.expires - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("telemetry HTTP request deadline exceeded")
        self.connection.settimeout(remaining)


class _Reader(io.RawIOBase):
    def __init__(self, deadline: _Deadline) -> None:
        self._deadline = deadline

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Buffer, /) -> int:
        self._deadline.apply()
        return self._deadline.connection.recv_into(buffer)


class _Writer(io.BufferedIOBase):
    def __init__(self, deadline: _Deadline) -> None:
        self._deadline = deadline

    def writable(self) -> bool:
        return True

    def write(self, buffer: Buffer, /) -> int:
        self._deadline.apply()
        self._deadline.connection.sendall(buffer)
        return memoryview(buffer).nbytes


class _Server(HTTPServer):
    request_queue_size = 4

    def __init__(self, address: tuple[str, int], cache: SnapshotCache) -> None:
        self.cache = cache
        super().__init__(address, _Handler)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        self.connection = self.request
        deadline = _Deadline(self.connection)
        self.rfile = io.BufferedReader(_Reader(deadline))
        self.wfile = _Writer(deadline)

    def handle(self) -> None:
        try:
            super().handle()
        except OSError:
            # A timed-out or disconnected client must not kill the server.
            self.close_connection = True

    def log_message(self, message_format: str, *args: object) -> None:
        """The embedding shell owns logging; omit request metadata by default."""

    def _respond(
        self, status: int, body: bytes, content_type: str, *, allow: bool = False
    ) -> None:
        self.close_connection = True
        self.send_response_only(status)
        self.send_header("Date", self.date_time_string())
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Content-Type-Options", "nosniff")
        if allow:
            self.send_header("Allow", "GET, HEAD")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_error(
        self, code: int, message: str | None = None, explain: str | None = None
    ) -> None:
        # Never reflect arbitrary request text or emit the Python server banner.
        self._respond(code, b"HTTP request rejected\n", "text/plain; charset=utf-8")

    def handle_expect_100(self) -> bool:
        self.send_error(400)
        return False

    def do_GET(self) -> None:
        if self.headers.get("Transfer-Encoding") is not None or self.headers.get_all(
            "Content-Length", []
        ) not in ([], ["0"]):
            self.send_error(400)
            return
        representations = cast(_Server, self.server).cache.representations()
        if self.path == "/metrics":
            self._respond(200, representations.metrics, prometheus.CONTENT_TYPE)
        elif self.path == "/v1/snapshot":
            self._respond(200, representations.json, JSON_CONTENT_TYPE)
        else:
            self._respond(404, b"Not found\n", "text/plain; charset=utf-8")

    def do_HEAD(self) -> None:
        self.do_GET()

    def _unsupported(self) -> None:
        self._respond(
            405, b"Method not allowed\n", "text/plain; charset=utf-8", allow=True
        )

    do_POST = _unsupported  # noqa: N815 - stdlib HTTP dispatch names
    do_PUT = _unsupported  # noqa: N815 - stdlib HTTP dispatch names
    do_PATCH = _unsupported  # noqa: N815 - stdlib HTTP dispatch names
    do_DELETE = _unsupported  # noqa: N815 - stdlib HTTP dispatch names
    do_OPTIONS = _unsupported  # noqa: N815 - stdlib HTTP dispatch names
    do_TRACE = _unsupported  # noqa: N815 - stdlib HTTP dispatch names
    do_CONNECT = _unsupported  # noqa: N815 - stdlib HTTP dispatch names


class MetricsServer:
    """Serve cached telemetry only after explicit :meth:`start`.

    ``listen`` must be an IPv4 literal. Non-loopback addresses require
    ``expose=True``. Port zero requests an OS-assigned port, available through
    :attr:`address` and :attr:`url` after starting. Closing waits for the single
    worker and its bounded active request, then releases the listener.
    """

    def __init__(
        self,
        cache: SnapshotCache,
        *,
        listen: str = "127.0.0.1",
        port: int = 9754,
        expose: bool = False,
    ) -> None:
        try:
            address = ipaddress.IPv4Address(listen)
        except ipaddress.AddressValueError as error:
            raise ValueError(
                "telemetry listen address must be an IPv4 literal"
            ) from error
        if not address.is_loopback and not expose:
            raise ValueError("non-loopback telemetry listen requires explicit exposure")
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("telemetry port must be between 0 and 65535")
        self._cache = cache
        self._listen = str(address)
        self._port = port
        self._lock = threading.Lock()
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        """The bound address; unavailable before starting or after closing."""
        with self._lock:
            if self._server is None:
                raise RuntimeError("telemetry HTTP server is not running")
            address = self._server.server_address
            return str(address[0]), int(address[1])

    @property
    def url(self) -> str:
        host, port = self.address
        return f"http://{host}:{port}"

    def start(self) -> MetricsServer:
        with self._lock:
            if self._server is not None:
                return self
            server = _Server((self._listen, self._port), self._cache)
            thread = threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.05},
                name="yoke-metrics-http",
                daemon=True,
            )
            try:
                thread.start()
            except BaseException:
                server.server_close()
                raise
            self._server = server
            self._thread = thread
        return self

    def close(self) -> None:
        with self._lock:
            if self._server is None:
                return
            self._server.shutdown()
            self._server.server_close()
            if self._thread is not None:
                self._thread.join()
            self._server = None
            self._thread = None

    def __enter__(self) -> MetricsServer:
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
