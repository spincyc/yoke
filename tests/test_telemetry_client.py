"""Explicit remote-client parsing and real loopback transport failures."""

from __future__ import annotations

import contextlib
import socket
import ssl
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from yoke.telemetry import client
from yoke.telemetry.client import SnapshotClient
from yoke.telemetry.http import MetricsServer, SnapshotCache
from yoke.telemetry.model import Snapshot


def sample() -> Snapshot:
    return Snapshot(1.0, "open-unauthenticated", (), {})


@contextlib.contextmanager
def response_server(
    body: bytes,
    *,
    status: int = 200,
    headers: tuple[tuple[str, str], ...] = (),
) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            for key, value in headers:
                self.send_header(key, value)
            self.send_header("Connection", "close")
            self.end_headers()
            with contextlib.suppress(OSError):
                self.wfile.write(body)

        def log_message(self, message_format: str, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


@pytest.mark.parametrize("suffix", ["", "/", "/v1/snapshot"])
def test_explicit_base_and_snapshot_urls(suffix: str) -> None:
    original = sample()
    with MetricsServer(SnapshotCache(original), port=0) as server:
        remote = SnapshotClient(server.url + suffix)
        assert remote.url == server.url + "/v1/snapshot"
        assert remote.get() == original


@pytest.mark.parametrize(
    "url",
    [
        "example.com",
        "//example.com",
        "ftp://example.com",
        "file:///v1/snapshot",
        "http:///v1/snapshot",
        "http://user:secret@example.com",
        "http://@example.com",
        "http://example.com?",
        "http://example.com#",
        "http://example.com?token=secret",
        "http://example.com/#fragment",
        "http://example.com/metrics",
        "http://example.com/v1/snapshot/",
        "http://example.com:0",
        "http://example.com:65536",
        "http://example.com:bad",
        "http://[invalid]",
        " http://example.com",
        "http://example.com\n",
        "http://example.com/\x00",
    ],
)
def test_invalid_explicit_urls(url: str) -> None:
    with pytest.raises(ValueError, match="telemetry URL"):
        SnapshotClient(url)


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True])
def test_invalid_timeouts(timeout: float) -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        SnapshotClient("http://127.0.0.1", timeout=timeout)


def test_https_keeps_certificate_and_hostname_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contexts: list[ssl.SSLContext] = []

    def offline_tls(
        context: ssl.SSLContext, connection: socket.socket, *, server_hostname: str
    ) -> socket.socket:
        contexts.append(context)
        assert server_hostname == "127.0.0.1"
        raise OSError("offline TLS test")

    monkeypatch.setattr(ssl.SSLContext, "wrap_socket", offline_tls)
    with response_server(b"") as url:
        remote = SnapshotClient(url.replace("http:", "https:"))
        with pytest.raises(OSError, match="offline TLS test"):
            remote.get()
    assert len(contexts) == 1
    assert contexts[0].verify_mode == ssl.CERT_REQUIRED
    assert contexts[0].check_hostname


def test_environment_proxies_are_not_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("all_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("no_proxy", "")
    with response_server(sample().to_json()) as url:
        assert SnapshotClient(url).get() == sample()


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_redirects_are_errors_without_following(status: int) -> None:
    with (
        response_server(sample().to_json()) as target,
        response_server(
            b"", status=status, headers=(("Location", target + "/v1/snapshot"),)
        ) as source,
        pytest.raises(OSError, match=f"status {status}"),
    ):
        SnapshotClient(source).get()


@pytest.mark.parametrize("status", [304, 400, 404, 500, 503])
def test_http_failure_is_not_a_snapshot(status: int) -> None:
    with (
        response_server(b"{}", status=status) as url,
        pytest.raises(OSError, match=f"status {status}"),
    ):
        SnapshotClient(url).get()


@pytest.mark.parametrize(
    "body",
    [
        b"not JSON",
        b"\xff",
        b'{"schema_version":1,"schema_version":1}',
        b'{"x":{"nested":1,"nested":2}}',
        b'{"schema_version":NaN}',
        b'{"schema_version":Infinity}',
        b'{"schema_version":-Infinity}',
        b"[]",
        b"{}",
        b'{"schema_version":999}',
        b"[" * 1100 + b"]" * 1100,
    ],
)
def test_untrusted_json_is_strictly_validated(body: bytes) -> None:
    with response_server(body) as url, pytest.raises(ValueError):
        SnapshotClient(url).get()


@pytest.mark.parametrize("declared", ["-1", "+1", "invalid", "1, 1"])
def test_invalid_content_lengths(declared: str) -> None:
    with (
        response_server(b"{}", headers=(("Content-Length", declared),)) as url,
        pytest.raises(ValueError, match="Content-Length"),
    ):
        SnapshotClient(url).get()


def test_duplicate_and_short_content_lengths_are_rejected() -> None:
    for headers in (
        (("Content-Length", "2"), ("Content-Length", "2")),
        (("Content-Length", "20"),),
    ):
        with (
            response_server(b"{}", headers=headers) as url,
            pytest.raises(ValueError, match="Content-Length"),
        ):
            SnapshotClient(url).get()


@pytest.mark.parametrize("declared", [False, True])
def test_response_size_is_bounded_before_json_decoding(
    monkeypatch: pytest.MonkeyPatch, declared: bool
) -> None:
    monkeypatch.setattr(client, "MAX_SNAPSHOT_BYTES", 16)
    headers = (("Content-Length", "17"),) if declared else ()
    with (
        response_server(b"x" * 17, headers=headers) as url,
        pytest.raises(ValueError, match="size limit"),
    ):
        SnapshotClient(url).get()


def test_failures_do_not_return_previous_data() -> None:
    server = MetricsServer(SnapshotCache(sample()), port=0).start()
    remote = SnapshotClient(server.url, timeout=0.1)
    try:
        assert remote.get() == sample()
    finally:
        server.close()
    with pytest.raises(OSError):
        remote.get()


def test_blocked_response_is_timed_out() -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    address = listener.getsockname()
    done = threading.Event()

    def hold_connection() -> None:
        with listener.accept()[0]:
            done.wait(1.0)

    worker = threading.Thread(target=hold_connection)
    worker.start()
    try:
        with pytest.raises(OSError):
            SnapshotClient(f"http://127.0.0.1:{address[1]}", timeout=0.05).get()
    finally:
        done.set()
        listener.close()
        worker.join()


@pytest.mark.parametrize("stage", ["headers", "body"])
def test_slow_drip_cannot_extend_the_absolute_deadline(stage: str) -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    address = listener.getsockname()
    done = threading.Event()

    def drip_response() -> None:
        with listener.accept()[0] as connection, contextlib.suppress(OSError):
            connection.recv(4096)
            prefix = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            prefix += (
                b"X-Slow: "
                if stage == "headers"
                else b"Content-Length: 30\r\nConnection: close\r\n\r\n"
            )
            connection.sendall(prefix)
            for _ in range(30):
                if done.wait(0.03):
                    break
                connection.sendall(b"x")

    worker = threading.Thread(target=drip_response)
    worker.start()
    started = time.monotonic()
    try:
        with pytest.raises(OSError):
            SnapshotClient(f"http://127.0.0.1:{address[1]}", timeout=0.12).get()
        assert time.monotonic() - started < 0.5
    finally:
        done.set()
        listener.close()
        worker.join()


def test_tcp_address_attempts_share_one_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [1.0]
    timeouts: list[float] = []
    attempts: list[tuple[str, int]] = []

    class FailedConnection:
        def settimeout(self, timeout: float) -> None:
            timeouts.append(timeout)

        def connect(self, address: tuple[str, int]) -> None:
            attempts.append(address)
            now[0] += 0.06
            raise OSError("offline address")

        def close(self) -> None:
            pass

    monkeypatch.setattr(client.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(client.socket, "socket", lambda *args: FailedConnection())
    monkeypatch.setattr(
        client.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 80)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.2", 80)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.3", 80)),
        ],
    )
    with pytest.raises(TimeoutError, match="deadline"):
        client._connect("example.invalid", 80, 1.1)
    assert timeouts == pytest.approx([0.1, 0.04])
    assert len(attempts) == 2
