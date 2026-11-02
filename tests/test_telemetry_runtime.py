"""Exercise the real optional worker/cache/CLI, without endpoint hardware."""

from __future__ import annotations

import json
import socket
import threading
import time
from typing import Any

import pytest

from yoke.cli import main
from yoke.core import frames
from yoke.core.types import FrameType
from yoke.telemetry.host import HostSampler
from yoke.telemetry.runtime import Monitor, MonitorConfig


def test_demo_never_reads_host_or_opens_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("offline demo performed source I/O")

    monkeypatch.setattr(HostSampler, "sample", forbidden)
    monkeypatch.setattr("yoke.telemetry.runtime.open_multicast_socket", forbidden)
    with Monitor(MonitorConfig(demo=True, interval=0.2)) as monitor:
        assert monitor.snapshot().authentication == "demo"
        assert monitor.cache.representations().json
    with pytest.raises(OSError, match="stopped"):
        monitor.snapshot()
    with pytest.raises(RuntimeError, match="once"):
        monitor.start()


@pytest.mark.parametrize("interval", [0, -1, True, float("nan"), float("inf"), 61])
def test_invalid_interval_is_rejected(interval: float) -> None:
    with pytest.raises(ValueError):
        MonitorConfig(interval=interval)


def test_failure_surfaces_and_worker_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    from yoke.telemetry import demo

    original = demo.demo_snapshot
    calls = 0

    def broken(now: float) -> Any:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise ValueError("source unavailable")
        return original(now)

    monkeypatch.setattr(demo, "demo_snapshot", broken)
    with Monitor(MonitorConfig(demo=True, interval=0.2)) as monitor:
        deadline = time.monotonic() + 3
        while calls == 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        with pytest.raises(OSError, match="source unavailable"):
            monitor.snapshot()


def test_thread_start_failure_preserves_error_and_closes_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Socket:
        closed = False

        def close(self) -> None:
            self.closed = True

    sock = Socket()
    monkeypatch.setattr(
        "yoke.telemetry.runtime.open_multicast_socket", lambda *_a, **_k: sock
    )
    monkeypatch.setattr(Monitor, "_publish", lambda *_a: None)

    def fail(_self: threading.Thread) -> None:
        raise RuntimeError("cannot start new thread")

    monkeypatch.setattr(threading.Thread, "start", fail)
    with pytest.raises(RuntimeError, match="cannot start new thread"):
        Monitor(MonitorConfig()).start()
    assert sock.closed


@pytest.mark.parametrize("command", ["metrics", "top"])
def test_one_shot_flushes_partial_interval(
    command: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    raw = frames.encode(
        FrameType.ANN,
        "usb-final",
        1,
        0,
        {
            "role": "producer",
            "kind": "usb",
            "name": "Final",
            "state": "active",
            "err": None,
            "media": None,
            "pairing": None,
        },
        b"",
    )

    class Socket:
        sent = False

        def settimeout(self, _timeout: float) -> None:
            pass

        def recvfrom(self, _size: int) -> tuple[bytes, object]:
            if not self.sent:
                self.sent = True
                return raw, None
            time.sleep(0.01)
            raise TimeoutError

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        "yoke.telemetry.runtime.open_multicast_socket", lambda *_a, **_k: Socket()
    )
    monkeypatch.setattr(HostSampler, "sample", lambda *_a: None)
    argv = [command, "--once", "--window", "0.05", "--interval", "60"]
    if command == "metrics":
        argv += ["--format", "json"]
    assert main(argv) == 0
    output = capsys.readouterr().out
    if command == "metrics":
        sample = json.loads(output)
        assert sample["devices"][0]["node_id"] == "usb-final"
        assert sample["observer"]["ok"] == 1
        assert sample["uptime_seconds"] > 0
        assert sample["sample_interval_seconds"] == 60
    else:
        assert "usb-final" in output


def test_real_loopback_observation_and_no_transmit() -> None:
    # Sending is test instrumentation only; the monitor owns no send path.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    cfg = MonitorConfig(
        group="239.255.42.91", port=port, interface="127.0.0.1", interval=0.2
    )
    try:
        monitor = Monitor(cfg).start()
    except OSError as error:
        pytest.skip(f"loopback multicast unavailable: {error}")
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.setsockopt(
                socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton("127.0.0.1")
            )
            raw = frames.encode(
                frame_type=FrameType.ANN,
                origin="usb-test",
                seq=time.time_ns(),
                ts=time.time(),
                body={
                    "role": "producer",
                    "kind": "usb",
                    "name": "Test",
                    "state": "active",
                    "err": None,
                    "pairing": None,
                    "media": None,
                },
                psk=b"",
            )
            sender.sendto(raw, (cfg.group, cfg.port))
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                sample = monitor.snapshot()
                if sample.devices:
                    break
                time.sleep(0.02)
        assert sample.devices[0].node_id == "usb-test"
        assert sample.devices[0].control_frames == 1
        assert sample.devices[0].control_bytes == len(raw)
        assert sample.devices[0].live
    finally:
        monitor.close()


def test_cli_demo_json_and_plain_top(capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        main(
            [
                "--psk-file",
                "/missing",
                "metrics",
                "--demo",
                "--once",
                "--format",
                "json",
            ]
        )
        == 0
    )
    sample = json.loads(capsys.readouterr().out)
    assert sample["schema_version"] == 1
    assert sample["authentication"] == "demo"
    assert main(["top", "--demo", "--once", "--width", "80", "--height", "24"]) == 0
    result = capsys.readouterr()
    assert "DEMO" in result.out
    assert "\x1b" not in result.out
    assert all(len(line) <= 80 for line in result.out.splitlines())
    assert len(result.out.splitlines()) <= 24


def test_cli_remote_top_reuses_collector_without_local_io(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from yoke.telemetry.demo import demo_snapshot
    from yoke.telemetry.http import MetricsServer, SnapshotCache

    def forbidden(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("remote view opened a local collector")

    monkeypatch.setattr("yoke.telemetry.runtime.open_multicast_socket", forbidden)
    monkeypatch.setattr(HostSampler, "sample", forbidden)
    with MetricsServer(SnapshotCache(demo_snapshot(0)), port=0) as server:
        assert (
            main(["--psk-file", "/missing", "top", "--url", server.url, "--once"]) == 0
        )
    assert "DEMO" in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv",
    [
        ["metrics", "--demo", "--once", "--interval", "nan"],
        ["top", "--demo", "--url", "http://localhost:9754", "--once"],
        ["top", "--demo", "--once", "--width", "0"],
        ["metrics", "--demo", "--once", "--window", "inf"],
    ],
)
def test_cli_bad_arguments_fail_cleanly(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(argv) == 2
    result = capsys.readouterr()
    assert result.err.startswith("yoke ")
    assert not result.out
