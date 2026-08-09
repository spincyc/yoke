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
- ``lost`` — holes in the observed seq range: ``(max_seq - min_seq + 1)``
  minus the count of distinct seqs seen. A late (reordered) arrival that
  fills a hole lowers this on the next frame.
- ``duplicated`` — arrivals whose seq was already seen.
- ``reordered`` — first-time arrivals whose seq is below the running max
  (i.e. they came late).
- ``last_latency`` / ``mean_latency`` — simulated glass-to-glass latency,
  ``now - meta["capture_now"]`` (seconds): most recent and running mean.
"""

from __future__ import annotations

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
    digest_of,
)

# RFC 3550 jitter smoothing gain (J += (|D| - J) / 16).
_JITTER_GAIN: Final = 16.0


class IntegritySink(StageBase):
    """Verify reassembled frames, record metrics, emit feedback."""

    name = "sink"

    def __init__(self, *, report_every: int = 30, stat_every: int = 30) -> None:
        if report_every <= 0 or stat_every <= 0:
            raise ValueError("report_every and stat_every must be positive")
        self._report_every = report_every
        self._stat_every = stat_every
        self._processed = 0
        self._frames_ok = 0
        self._frames_bad = 0
        self._duplicated = 0
        self._reordered = 0
        self._seen: set[int] = set()
        self._min_seq: int | None = None
        self._max_seq: int | None = None
        self._last_latency = 0.0
        self._lat_sum = 0.0
        self._lat_count = 0
        self._jitter = 0.0
        self._prev_transit: float | None = None
        self._congestion = 0.0
        self._last_report_key: tuple[float, float] | None = None

    # -- feedback injection --------------------------------------------------

    def set_congestion(self, value: float) -> None:
        """Inject an ECN-style congestion reading in [0, 1] (the real
        transport measures this; tests set it directly)."""
        self._congestion = value

    # -- introspection -------------------------------------------------------

    def metrics(self) -> Body:
        expected = self._expected()
        return {
            "frames_ok": self._frames_ok,
            "frames_bad": self._frames_bad,
            "lost": max(expected - len(self._seen), 0),
            "duplicated": self._duplicated,
            "reordered": self._reordered,
            "last_latency": self._last_latency,
            "mean_latency": (
                self._lat_sum / self._lat_count if self._lat_count else 0.0
            ),
        }

    # -- stage ---------------------------------------------------------------

    def process(self, unit: MediaUnit, now: float) -> StageOutcome:
        lossless = bool(unit.meta.get("lossless", True))
        ok, reason = self._verify(unit, lossless)
        if ok:
            self._frames_ok += 1
        else:
            self._frames_bad += 1

        self._record_arrival(unit.seq)
        self._record_latency(unit, now)
        self._processed += 1

        events: list[StageEvent] = [
            StageEvent(
                stage=self.name,
                kind=EventKind.INTEGRITY,
                seq=unit.seq,
                pts=unit.pts,
                now=now,
                detail={"ok": ok, "reason": reason, "lossless": lossless},
            )
        ]
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
        report = self._build_report(now)
        key = (round(report.loss, 9), round(report.congestion, 9))
        periodic = self._processed % self._report_every == 0
        if periodic or key != self._last_report_key:
            feedback.append(report)
            self._last_report_key = key

        return StageOutcome(events=events, feedback=feedback)

    # -- internals -----------------------------------------------------------

    def _verify(self, unit: MediaUnit, lossless: bool) -> tuple[bool, str]:
        header = parse_header(unit.payload)
        if header is None:
            return False, "short-payload"
        embedded_seq, embedded_pts, _crc = header
        if embedded_seq != unit.seq:
            return False, "seq-mismatch"
        if lossless:
            if embedded_pts != unit.pts:
                return False, "pts-mismatch"
            if digest_of(unit.payload) != unit.source_digest:
                return False, "digest-mismatch"
        return True, "ok"

    def _record_arrival(self, seq: int) -> None:
        if seq in self._seen:
            self._duplicated += 1
        else:
            if self._max_seq is not None and seq < self._max_seq:
                self._reordered += 1
            self._seen.add(seq)
        self._min_seq = seq if self._min_seq is None else min(self._min_seq, seq)
        self._max_seq = seq if self._max_seq is None else max(self._max_seq, seq)

    def _record_latency(self, unit: MediaUnit, now: float) -> None:
        capture_now = unit.meta.get("capture_now")
        if not isinstance(capture_now, (int, float)):
            return
        transit = now - float(capture_now)
        self._last_latency = transit
        self._lat_sum += transit
        self._lat_count += 1
        if self._prev_transit is not None:
            d = abs(transit - self._prev_transit)
            self._jitter += (d - self._jitter) / _JITTER_GAIN
        self._prev_transit = transit

    def _expected(self) -> int:
        if self._min_seq is None or self._max_seq is None:
            return 0
        return self._max_seq - self._min_seq + 1

    def _build_report(self, now: float) -> NetworkReport:
        expected = self._expected()
        lost = max(expected - len(self._seen), 0)
        loss = lost / expected if expected else 0.0
        return NetworkReport(
            now=now,
            loss=loss,
            jitter=self._jitter,
            congestion=self._congestion,
            frames_ok=self._frames_ok,
            frames_bad=self._frames_bad,
        )
