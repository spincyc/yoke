"""Optional passive collection shell; independent of the endpoint runner."""

from __future__ import annotations

import math
import socket
import threading
import time
from dataclasses import dataclass, field, replace

from yoke.core.types import CONTROL_GROUP, CONTROL_PORT, MAX_FRAME_BYTES
from yoke.net.mcast import open_multicast_socket, validate_multicast
from yoke.telemetry.collector import TelemetryCollector
from yoke.telemetry.host import HostSampler
from yoke.telemetry.http import SnapshotCache
from yoke.telemetry.model import Snapshot

MIN_INTERVAL = 0.2
MAX_INTERVAL = 60.0


def validate_interval(value: float) -> None:
    if isinstance(value, bool) or not math.isfinite(value):
        raise ValueError("sample interval must be finite")
    if not MIN_INTERVAL <= value <= MAX_INTERVAL:
        raise ValueError(
            f"sample interval must be {MIN_INTERVAL} through {MAX_INTERVAL}s"
        )


@dataclass(frozen=True)
class MonitorConfig:
    group: str = CONTROL_GROUP
    port: int = CONTROL_PORT
    interface: str = "0.0.0.0"
    psk: bytes = field(default=b"", repr=False)
    interval: float = 1.0
    local_node: str | None = None
    demo: bool = False

    def __post_init__(self) -> None:
        validate_interval(self.interval)
        validate_multicast(self.group, self.port, interface=self.interface)


class Monitor:
    """One receiver/sampler worker and one immutable cache for all consumers.

    No drivers, state files, sends, route changes or media sockets. The
    worker checks its deadline after every datagram so a busy control group
    cannot postpone sampling indefinitely. HTTP and terminal work never run
    on this receiver thread.
    """

    def __init__(self, config: MonitorConfig) -> None:
        self.config = config
        self._sampler = HostSampler(local_node=config.local_node)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None
        self._collector: TelemetryCollector | None = None
        self._cache: SnapshotCache | None = None
        self._error: Exception | None = None
        self._started_at = 0.0
        self._published_at = 0.0

    @property
    def cache(self) -> SnapshotCache:
        if self._cache is None:
            raise RuntimeError("monitor has not started")
        return self._cache

    def start(self) -> Monitor:
        if self._thread is not None or self._stop.is_set():
            raise RuntimeError("monitor instances can only be started once")
        self._started_at = time.monotonic()
        try:
            if not self.config.demo:
                self._collector = TelemetryCollector(
                    self.config.psk,
                    now=self._started_at,
                    wall_time=time.time(),
                )
                self._sock = open_multicast_socket(
                    self.config.group,
                    self.config.port,
                    interface=self.config.interface,
                )
            self._publish(self._started_at)
            self._thread = threading.Thread(
                target=self._run, name="yoke-telemetry", daemon=True
            )
            self._thread.start()
        except BaseException:
            self.close()
            raise
        return self

    def _publish(self, now: float) -> None:
        if self.config.demo:
            from yoke.telemetry.demo import demo_snapshot

            sample = demo_snapshot(now - self._started_at)
        else:
            assert self._collector is not None
            sample = self._collector.snapshot(now, self._sampler.sample(now))
        sample = replace(sample, sample_interval_seconds=self.config.interval)
        if self._cache is None:
            self._cache = SnapshotCache(sample)
        else:
            self._cache.update(sample)
        self._published_at = now

    def _run(self) -> None:
        deadline = self._started_at + self.config.interval
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                if now >= deadline:
                    self._publish(now)
                    # Skip missed slots rather than catching up in a burst.
                    deadline = now + self.config.interval
                timeout = min(0.2, max(0.001, deadline - time.monotonic()))
                if self._sock is None:
                    self._stop.wait(timeout)
                    continue
                self._sock.settimeout(timeout)
                try:
                    raw, _address = self._sock.recvfrom(MAX_FRAME_BYTES + 1)
                except TimeoutError:
                    continue
                assert self._collector is not None
                # Re-read wall time per datagram: a clock stepped by time
                # sync must not keep rejecting peers as future (ADR 0023).
                self._collector.observe(raw, time.monotonic(), wall=time.time())
        except Exception as exc:
            self._error = exc
            self._stop.set()

    def snapshot(self) -> Snapshot:
        if self._error is not None:
            raise OSError(
                f"telemetry collection failed: {self._error}"
            ) from self._error
        if self._stop.is_set():
            raise OSError("telemetry collection stopped")
        if time.monotonic() - self._published_at > max(3 * self.config.interval, 3):
            raise OSError("telemetry collection is stale")
        return self.cache.snapshot()

    def close(self) -> None:
        self._stop.set()
        try:
            if self._thread is not None and self._thread.ident is not None:
                self._thread.join(timeout=2)
                if self._thread.is_alive():
                    raise RuntimeError("telemetry worker did not stop")
        finally:
            if self._sock is not None:
                self._sock.close()
                self._sock = None

    def finish(self) -> Snapshot:
        """Stop collection, then publish its final partial interval safely.

        Joining transfers exclusive collector/sampler ownership back to the
        caller. This captures accepted observations even when the requested
        one-shot window is shorter than the regular publication interval.
        """
        self.close()
        if self._error is not None:
            raise OSError(
                f"telemetry collection failed: {self._error}"
            ) from self._error
        self._publish(time.monotonic())
        return self.cache.snapshot()

    def __enter__(self) -> Monitor:
        return self.start()

    def __exit__(self, *_args: object) -> None:
        self.close()
