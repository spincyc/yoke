"""Integrity sink: the receiver's terminal stage.

:class:`IntegritySink` receives reassembled frame units (kind ``FRAME``)
from the codec-decode stage, verifies them glass-to-glass, records health
metrics, and emits observability taps plus periodic ``NetworkReport``
feedback for the sender's rate controller. It is sans-I/O: ``now`` is
passed in; nothing here reads a clock or a socket.

Verification modes (driven by ``meta["lossless"]``, absent ==> assume the
bit-exact test path):

- lossless: the embedded ``seq``/``pts`` must match the unit, AND
  ``digest_of(payload)`` must match ``source_digest`` — a full glass-to-
  glass bit-exact check. A corrupted payload is flagged bad.
- lossy: only the embedded ``seq`` must match (the frame is present and
  correctly identified). A digest mismatch is EXPECTED and NOT counted
  bad — the codec changed the bits on purpose.

Metric definitions (via :meth:`metrics`):

- ``frames_ok`` / ``frames_bad`` — arrivals that passed / failed
  verification. Every processed unit lands in exactly one (duplicates
  included), so their sum is the number of arrivals.
- ``lost`` — holes in the accepted seq range. Late arrivals repair gaps
  within the recent sequence window; older gaps remain in cumulative loss.
- ``duplicated`` — arrivals whose seq was already seen.
- ``reordered`` — first-time arrivals whose seq is below the running max
  (i.e. they came late), within the retained window. Older arrivals emit
  ``outside-history`` and leave ordering counters unchanged.
- ``last_latency`` / ``mean_latency`` — most recent valid capture-to-sink
  sample and mean of valid samples, in seconds. Both are ``None`` until a
  valid aligned-clock ``now - meta["capture_now"]`` sample is available.
  A measured zero is ``0.0``; later missing timestamps retain prior samples.
"""

from __future__ import annotations

import heapq
import math
from typing import Final

from yoke.media.pipeline import StageBase
from yoke.media.stages.source import parse_header
from yoke.media.types import (
    Body,
    EventKind,
    MediaUnit,
    NetworkReport,
    StageEvent,
    StageOutcome,
    UnitKind,
    digest_of,
)

# RFC 3550 jitter smoothing gain (J += (|D| - J) / 16).
_JITTER_GAIN: Final = 16.0


class IntegritySink(StageBase):
    """Verify reassembled frames, record metrics, emit feedback."""

    name = "sink"

    def __init__(
        self, *, report_every: int = 30, stat_every: int = 30, history_size: int = 4096
    ) -> None:
        if any(
            type(v) is not int or v <= 0
            for v in (report_every, stat_every, history_size)
        ):
            raise ValueError(
                "sink intervals and history_size must be positive integers"
            )
        self._report_every = report_every
        self._stat_every = stat_every
        self._processed = 0
        self._frames_ok = 0
        self._frames_bad = 0
        self._duplicated = 0
        self._reordered = 0
        self._seen: set[int] = set()
        self._sequence_heap: list[int] = []
        self._history_size = history_size
        self._distinct = 0
        self._min_seq: int | None = None
        self._max_seq: int | None = None
        self._last_latency: float | None = None
        self._lat_mean = 0.0
        self._lat_count = 0
        self._jitter = 0.0
        self._prev_transit: float | None = None
        self._congestion = 0.0
        self._last_report_key: tuple[float, float] | None = None

    # -- feedback injection --------------------------------------------------

    def set_congestion(self, value: float) -> None:
        """Inject an ECN-style congestion reading in [0, 1] (the real
        transport measures this; tests set it directly)."""
        if (
            type(value) not in (int, float)
            or not 0 <= value <= 1
            or not math.isfinite(value)
        ):
            raise ValueError("congestion must be finite and in [0, 1]")
        self._congestion = value

    # -- introspection -------------------------------------------------------

    def metrics(self) -> Body:
        expected = self._expected()
        return {
            "frames_ok": self._frames_ok,
            "frames_bad": self._frames_bad,
            "lost": max(expected - self._distinct, 0),
            "duplicated": self._duplicated,
            "reordered": self._reordered,
            "last_latency": self._last_latency,
            "mean_latency": self._lat_mean if self._lat_count else None,
        }

    # -- stage ---------------------------------------------------------------

    def process(self, unit: MediaUnit, now: float) -> StageOutcome:
        lossless = bool(unit.meta.get("lossless", True))
        ok, reason = self._verify(unit, lossless)
        if ok:
            self._frames_ok += 1
        else:
            self._frames_bad += 1

        within_history = self._record_arrival(unit.seq)
        latency = self._record_latency(unit, now)
        self._processed += 1

        detail: Body = {"ok": ok, "reason": reason, "lossless": lossless}
        if latency is not None:
            detail["latency"] = latency
        events: list[StageEvent] = [
            StageEvent(
                stage=self.name,
                kind=EventKind.INTEGRITY,
                seq=unit.seq,
                pts=unit.pts,
                now=now,
                detail=detail,
            )
        ]
        if not within_history:
            events.append(
                StageEvent(
                    stage=self.name,
                    kind=EventKind.DROP,
                    seq=unit.seq,
                    pts=unit.pts,
                    now=now,
                    detail={"why": "outside-history"},
                )
            )
        if self._processed % self._stat_every == 0:
            events.append(
                StageEvent(
                    stage=self.name,
                    kind=EventKind.STAT,
                    seq=unit.seq,
                    pts=unit.pts,
                    now=now,
                    detail=self.metrics(),
                )
            )

        feedback: list[NetworkReport] = []
        expected = self._expected()
        lost = max(expected - self._distinct, 0)
        loss = lost / expected if expected else 0.0
        key = (round(loss, 9), round(self._congestion, 9))
        periodic = self._processed % self._report_every == 0
        if periodic or key != self._last_report_key:
            feedback.append(self._build_report(now))
            self._last_report_key = key

        return StageOutcome(events=events, feedback=feedback)

    # -- internals -----------------------------------------------------------

    def _verify(self, unit: MediaUnit, lossless: bool) -> tuple[bool, str]:
        if unit.kind is not UnitKind.FRAME:
            return False, "not-a-frame"
        if unit.meta.get("incomplete", False):
            return False, "incomplete-frame"
        header = parse_header(unit.payload)
        if header is None:
            return False, "short-payload"
        embedded_seq, embedded_pts, _crc = header
        if type(unit.seq) is not int or embedded_seq != unit.seq:
            return False, "seq-mismatch"
        if lossless:
            if type(unit.pts) is not int or embedded_pts != unit.pts:
                return False, "pts-mismatch"
            if digest_of(unit.payload) != unit.source_digest:
                return False, "digest-mismatch"
        return True, "ok"

    def _record_arrival(self, seq: int) -> bool:
        if type(seq) is not int or not 0 <= seq <= 0xFFFF_FFFF_FFFF_FFFF:
            return False
        if self._max_seq is not None and seq < self._max_seq - self._history_size + 1:
            return False
        if seq in self._seen:
            self._duplicated += 1
        else:
            if self._max_seq is not None and seq < self._max_seq:
                self._reordered += 1
            self._seen.add(seq)
            heapq.heappush(self._sequence_heap, seq)
            self._distinct += 1
        self._min_seq = seq if self._min_seq is None else min(self._min_seq, seq)
        self._max_seq = seq if self._max_seq is None else max(self._max_seq, seq)
        floor = self._max_seq - self._history_size + 1
        while self._sequence_heap and self._sequence_heap[0] < floor:
            self._seen.remove(heapq.heappop(self._sequence_heap))
        return True

    def _record_latency(self, unit: MediaUnit, now: float) -> float | None:
        capture_now = unit.meta.get("capture_now")
        if (
            not isinstance(capture_now, (int, float))
            or isinstance(capture_now, bool)
            or capture_now < 0
            or not math.isfinite(now)
        ):
            return None
        try:
            capture = float(capture_now)
        except OverflowError:
            return None
        if not math.isfinite(capture):
            return None
        transit = now - capture
        if not math.isfinite(transit) or transit < 0:
            return None
        self._last_latency = transit
        self._lat_count += 1
        self._lat_mean += (transit - self._lat_mean) / self._lat_count
        if self._prev_transit is not None:
            d = abs(transit - self._prev_transit)
            self._jitter += (d - self._jitter) / _JITTER_GAIN
        self._prev_transit = transit
        return transit

    def _expected(self) -> int:
        if self._min_seq is None or self._max_seq is None:
            return 0
        return self._max_seq - self._min_seq + 1

    def _build_report(self, now: float) -> NetworkReport:
        expected = self._expected()
        lost = max(expected - self._distinct, 0)
        loss = lost / expected if expected else 0.0
        return NetworkReport(
            now=now,
            loss=loss,
            jitter=self._jitter,
            congestion=self._congestion,
            frames_ok=self._frames_ok,
            frames_bad=self._frames_bad,
        )
