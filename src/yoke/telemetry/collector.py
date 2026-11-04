"""Pure passive collection from existing YWP/1 control observations."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from yoke.core.types import PEER_FORGET, PEER_TIMEOUT, Body, FrameType
from yoke.snoop import Snooper, SnoopRecord
from yoke.telemetry.model import (
    EVENTS,
    MAX_COUNTER,
    MAX_DETAIL_LENGTH,
    MAX_SECONDS,
    DeviceSnapshot,
    Snapshot,
)


@dataclass
class _Counters:
    last_announcement: float
    frames: int = 0
    bytes: int = 0
    events: dict[str, int] = field(default_factory=dict)


class TelemetryCollector:
    """Accumulate retained per-device counters without owning any I/O.

    Only accepted origins with an announcement get device series. Unknown
    event names share one ``other`` bucket. Counters reset after peer eviction
    and saturate at unsigned 64-bit bounds. Packet processing neither builds
    nor serializes a snapshot.
    """

    def __init__(self, psk: bytes, *, now: float, wall_time: float) -> None:
        self._validate_now(now)
        self._validate_now(wall_time)
        self._snooper = Snooper(psk, now=now, wall_time=wall_time)
        self._authentication = "site-psk" if psk else "open-unauthenticated"
        self._started = now
        self._now = now
        self._counters: dict[str, _Counters] = {}

    def observe(
        self, raw: bytes, now: float, *, wall: float | None = None
    ) -> list[SnoopRecord]:
        """Judge a datagram, counting only accepted announced-device traffic.

        ``wall`` is the receiver's current wall clock for the future-sequence
        bound (ADR 0023); timers and ages use monotonic ``now`` only.
        """
        self._advance(now)
        records = self._snooper.observe(raw, now, wall=wall)
        for record in records:
            frame = record.frame
            if record.verdict != "ok" or frame is None:
                continue
            view = self._snooper.nodes.get(frame.origin)
            if view is None:
                continue
            counter = self._counters.get(frame.origin)
            if counter is None or now - counter.last_announcement > PEER_FORGET:
                counter = _Counters(last_announcement=view.last_seen)
                self._counters[frame.origin] = counter
            if frame.type is FrameType.ANN:
                counter.last_announcement = now
            counter.frames = min(MAX_COUNTER, counter.frames + 1)
            counter.bytes = min(MAX_COUNTER, counter.bytes + len(raw))
            if frame.type is FrameType.EVT:
                event = frame.body.get("event")
                event = event if event in EVENTS else "other"
                counter.events[event] = min(
                    MAX_COUNTER, counter.events.get(event, 0) + 1
                )
        # A new origin is counted before reconciling, so even a full FIFO
        # eviction changes the relative sizes. Ordinary announcements need
        # no additional walk through every retained peer.
        if len(self._counters) != len(self._snooper.nodes):
            self._retain_counters()
        return records

    def snapshot(self, now: float, host: Body | None = None) -> Snapshot:
        """Age liveness and retention even when no datagrams have arrived."""
        self._advance(now)
        self._snooper.topology(now)
        self._retain_counters()
        devices = []
        for node_id, view in sorted(self._snooper.nodes.items()):
            counter = self._counters[node_id]
            detail = view.err
            if view.driver_status is not None:
                evidence = view.driver_status.get("detail")
                if not detail and isinstance(evidence, str) and evidence:
                    detail = evidence
            age = max(0.0, now - view.last_seen)
            devices.append(
                DeviceSnapshot(
                    node_id=node_id,
                    name=view.name,
                    kind=view.kind,
                    role=view.role,
                    state=view.state,
                    live=age <= PEER_TIMEOUT,
                    last_seen_age_seconds=min(MAX_SECONDS, age),
                    pairing_producer=view.pairing_producer
                    if view.role == "consumer"
                    else None,
                    driver_detail=detail[:MAX_DETAIL_LENGTH]
                    if detail is not None
                    else None,
                    control_frames=counter.frames,
                    control_bytes=counter.bytes,
                    events=dict(counter.events),
                )
            )
        return Snapshot(
            uptime_seconds=min(MAX_SECONDS, now - self._started),
            authentication=self._authentication,
            devices=tuple(devices),
            observer={
                verdict: min(MAX_COUNTER, count)
                for verdict, count in self._snooper.counts.items()
            },
            host=host,
        )

    def _retain_counters(self) -> None:
        self._counters = {
            node_id: counter
            for node_id, counter in self._counters.items()
            if node_id in self._snooper.nodes
        }

    @staticmethod
    def _validate_now(now: float) -> None:
        if isinstance(now, bool) or not isinstance(now, (int, float)):
            raise ValueError("telemetry clock must be a finite monotonic number")
        if not math.isfinite(now) or abs(now) > MAX_SECONDS:
            raise ValueError("telemetry clock must be a finite monotonic number")

    def _advance(self, now: float) -> None:
        self._validate_now(now)
        if now < self._now:
            raise ValueError("telemetry clock cannot move backwards")
        self._now = now
