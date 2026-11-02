"""Real loopback HTTP coverage; no device or control-network access."""

from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import replace
from http.client import HTTPConnection

import pytest

from yoke.core.types import MAX_TRACKED
from yoke.telemetry import http, prometheus
from yoke.telemetry.http import MetricsServer, SnapshotCache
from yoke.telemetry.model import (
    EVENTS,
    MAX_COUNTER,
    MAX_SNAPSHOT_BYTES,
    DeviceSnapshot,
    Snapshot,
)


def sample(uptime: float = 1.0) -> Snapshot:
    return Snapshot(uptime, "open-unauthenticated", (), {"ok": 3})


def request(
    server: MetricsServer,
    path: str = "/v1/snapshot",
    method: str = "GET",
    body: bytes | None = None,
) -> tuple[int, dict[str, str], bytes]:
    connection = HTTPConnection(*server.address, timeout=2.0)
    try:
        connection.request(method, path, body=body)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def test_cache_detaches_models_and_publishes_immutable_representations() -> None:
    original = sample()
    cache = SnapshotCache(original)
    first = cache.representations()
    original.observer["ok"] = 99
    detached = cache.snapshot()
    detached.observer["ok"] = 100
    assert cache.snapshot().observer["ok"] == 3
    assert cache.representations() is first
    cache.update(replace(original, uptime_seconds=2.0))
    assert cache.snapshot().uptime_seconds == 2.0
    assert json.loads(first.json)["uptime_seconds"] == 1.0
    assert cache.representations() is not first


def test_failed_update_keeps_previous_coherent_sample(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = SnapshotCache(sample())
    previous = cache.representations()
    monkeypatch.setattr(prometheus, "render", lambda _: b"x" * 33)
    monkeypatch.setattr(http, "MAX_METRICS_BYTES", 32)
    with pytest.raises(ValueError, match="response size"):
        cache.update(sample(2))
    assert cache.representations() is previous
    assert cache.snapshot().uptime_seconds == 1


def test_scrapes_reuse_bytes_without_sampling_or_encoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = SnapshotCache(sample())
    cached = cache.representations()

    def forbidden(*args: object, **kwargs: object) -> bytes:
        pytest.fail("a scrape must not sample, clone, or render")

    monkeypatch.setattr(Snapshot, "to_json", forbidden)
    monkeypatch.setattr(Snapshot, "to_body", forbidden)
    monkeypatch.setattr(prometheus, "render", forbidden)
    with MetricsServer(cache, port=0) as server:
        assert server.start() is server
        for path, body, content_type in (
            ("/v1/snapshot", cached.json, http.JSON_CONTENT_TYPE),
            ("/metrics", cached.metrics, prometheus.CONTENT_TYPE),
        ):
            for _ in range(2):
                status, headers, response = request(server, path)
                assert status == 200
                assert response == body
                assert headers["Content-Length"] == str(len(body))
                assert headers["Content-Type"] == content_type
                assert headers["Connection"] == "close"
                assert headers["Cache-Control"] == "no-store"
                assert "Server" not in headers
            status, headers, response = request(server, path, "HEAD")
            assert status == 200
            assert response == b""
            assert headers["Content-Length"] == str(len(body))


@pytest.mark.parametrize("path", ["/", "/missing", "/metrics?x=1", "/v2/snapshot"])
def test_unknown_routes(path: str) -> None:
    with MetricsServer(SnapshotCache(sample()), port=0) as server:
        assert request(server, path)[0] == 404
        status, headers, body = request(server, path, "HEAD")
        assert status == 404
        assert body == b""
        assert int(headers["Content-Length"]) > 0


@pytest.mark.parametrize(
    "method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"]
)
def test_uploads_and_commands_are_not_supported(method: str) -> None:
    with MetricsServer(SnapshotCache(sample()), port=0) as server:
        status, headers, _ = request(server, "/v1/snapshot", method, b"command")
        assert status == 405
        assert headers["Allow"] == "GET, HEAD"
        assert request(server, "/metrics", "UNKNOWN")[0] == 501


def test_get_with_body_and_expect_continue_are_rejected() -> None:
    with MetricsServer(SnapshotCache(sample()), port=0) as server:
        assert request(server, body=b"unwanted")[0] == 400
        with socket.create_connection(server.address, timeout=2) as connection:
            connection.sendall(
                b"POST /v1/snapshot HTTP/1.1\r\nHost: test\r\n"
                b"Expect: 100-continue\r\nContent-Length: 100\r\n\r\n"
            )
            assert connection.recv(1024).startswith(b"HTTP/1.1 400")


@pytest.mark.parametrize("listen", ["localhost", "::1", "invalid", "127.1"])
def test_only_literal_ipv4_listen_addresses(listen: str) -> None:
    with pytest.raises(ValueError, match="IPv4 literal"):
        MetricsServer(SnapshotCache(sample()), listen=listen)


@pytest.mark.parametrize("listen", ["0.0.0.0", "192.0.2.10"])
def test_nonloopback_requires_explicit_exposure_without_binding(listen: str) -> None:
    with pytest.raises(ValueError, match="explicit exposure"):
        MetricsServer(SnapshotCache(sample()), listen=listen)
    MetricsServer(SnapshotCache(sample()), listen=listen, expose=True)


@pytest.mark.parametrize("port", [-1, 65536, True])
def test_invalid_ports(port: int) -> None:
    with pytest.raises(ValueError, match="port"):
        MetricsServer(SnapshotCache(sample()), port=port)


def test_explicit_lifecycle_releases_listener() -> None:
    server = MetricsServer(SnapshotCache(sample()), port=0)
    with pytest.raises(RuntimeError, match="not running"):
        _ = server.address
    server.close()
    server.start()
    address = server.address
    assert server.url == f"http://127.0.0.1:{address[1]}"
    assert request(server)[0] == 200
    server.close()
    server.close()
    with pytest.raises(OSError), socket.create_connection(address, timeout=0.1):
        pass
    with pytest.raises(RuntimeError, match="not running"):
        _ = server.url


def test_slow_headers_have_an_absolute_deadline_and_close_drains_one_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(http, "REQUEST_DEADLINE_SECONDS", 0.15)
    server = MetricsServer(SnapshotCache(sample()), port=0).start()
    connections: list[socket.socket] = []
    started = time.monotonic()
    try:
        first = socket.create_connection(server.address, timeout=2)
        connections.append(first)
        first.sendall(b"G")
        for _ in range(4):
            time.sleep(0.05)
            try:
                first.sendall(b"E")
            except OSError:
                break
        assert first.recv(1024) == b""
        assert request(server)[0] == 200
        for _ in range(3):
            connection = socket.create_connection(server.address, timeout=2)
            connections.append(connection)
            connection.sendall(b"GET /metrics HTTP/1.1\r\n")
        workers = [
            thread
            for thread in threading.enumerate()
            if thread.name == "yoke-metrics-http"
        ]
        assert len(workers) == 1
        server.close()
        assert not workers[0].is_alive()
        assert time.monotonic() - started < 1.5
    finally:
        server.close()
        for connection in connections:
            connection.close()


def test_cache_updates_are_atomic_during_reads() -> None:
    cache = SnapshotCache(sample())
    seen: list[bytes] = []

    def read_samples() -> None:
        for _ in range(100):
            seen.append(cache.representations().json)

    worker = threading.Thread(target=read_samples)
    worker.start()
    for uptime in range(1, 10):
        cache.update(sample(float(uptime)))
    worker.join()
    assert seen
    assert all(1 <= json.loads(value)["uptime_seconds"] <= 9 for value in seen)


def test_maximum_fleet_has_a_separate_bounded_metrics_budget() -> None:
    devices = tuple(
        DeviceSnapshot(
            node_id=f"hdmi-{index:027d}",
            name="",
            kind="hdmi",
            role="consumer",
            state="active",
            live=True,
            last_seen_age_seconds=1,
            pairing_producer="p",
            driver_detail=None,
            control_frames=MAX_COUNTER,
            control_bytes=MAX_COUNTER,
            events=dict.fromkeys(EVENTS, MAX_COUNTER),
        )
        for index in range(MAX_TRACKED)
    )
    snapshot = Snapshot(1, "open-unauthenticated", devices, {})
    representations = SnapshotCache(snapshot).representations()
    assert len(representations.json) < MAX_SNAPSHOT_BYTES
    assert MAX_SNAPSHOT_BYTES < len(representations.metrics) <= http.MAX_METRICS_BYTES
