"""Adaptive jitter buffer: reorder, hold for playout, drop late/lost.

:class:`JitterBuffer` sits between FEC-decode and depacketize on the
receiver (see doc/design/MEDIA.md). It absorbs network jitter and
reordering by holding each RTP packet a short, adaptive *playout delay*
and then releasing packets in ascending RTP sequence order. It is
sans-I/O: ``now`` (Unix seconds) is passed into every call; the shell
(and the tests) own the clock and drive ``tick``. ``next_deadline``
tells the shell the next instant a held packet is due, so a fake clock
can advance exactly to a release.

Sequence handling (RFC 1982 serial arithmetic, 16-bit):

- ``process`` keys each packet by its 16-bit RTP sequence number
  (``unit.seq``), which wraps at 65536. Ordering and the "already
  released" comparison use serial arithmetic, so the buffer stays
  correct across the wrap (``..., 65534, 65535, 0, 1, ...``).
- A first-time packet whose slot has already been released (it sits at
  or behind the released watermark) is *late*: dropped, counted, and
  tapped ``LATE``. A second copy of a packet still buffered or already
  released is a *duplicate*: dropped silently and counted apart from
  late.

Playout / adaptation law:

- ``playout_delay`` lives in ``[min_delay, max_delay]`` and starts at a
  constructor default. A packet's playout deadline is ``arrival +
  playout_delay``.
- The delay tracks a target of ``PLAYOUT_K * jitter`` (RFC 3550 smoothed
  inter-arrival variation), floored at ``min_delay`` and capped at
  ``max_delay``. It grows quickly toward a rising target and shrinks
  slowly toward a falling one, so a jitter spike is covered promptly
  while the added latency is reclaimed gently once the link is smooth.

``tick`` releases every held packet whose deadline has passed, in
sequence order, advancing the watermark. A gap whose head-of-line
successor is already due is declared lost: the missing sequence numbers
are skipped (tapped ``DROP``) so the stream keeps flowing.
"""

from __future__ import annotations

import heapq
import math
from collections import deque
from dataclasses import dataclass
from typing import Final

from yoke.media.pipeline import StageBase
from yoke.media.types import (
    MAX_BUFFER_BYTES,
    MEDIA_CLOCK_HZ,
    Body,
    EventKind,
    MediaUnit,
    StageEvent,
    StageOutcome,
    UnitKind,
    require_uint,
)

# 16-bit RTP sequence space (RFC 3550 / serial arithmetic RFC 1982).
_SEQ_MOD: Final = 1 << 16
_SEQ_HALF: Final = 1 << 15
# RFC 3550 jitter smoothing gain: J += (|D| - J) / 16.
_JITTER_GAIN: Final = 16.0
# Playout adaptation.
_PLAYOUT_K: Final = 4.0  # target playout = K * jitter, floored at min_delay
_GROW_RATE: Final = 0.6  # fast approach toward a rising target
_SHRINK_RATE: Final = 0.05  # slow approach toward a falling target
# Cap on how many missing sequence numbers a single gap enumerates,
# bounding work when a huge hole (or a reset) appears.
_MAX_GAP: Final = 4096
# How many recently-released sequence numbers to remember, so a second
# copy of a released packet is recognised as a duplicate (not a late
# first arrival). Bounds memory across the wrap.
_RELEASED_WINDOW: Final = 1024


@dataclass(frozen=True)
class _Held:
    """A buffered packet and the instant it arrived."""

    unit: MediaUnit
    arrival: float


class JitterBuffer(StageBase):
    """Reorder and delay-play RTP packets; drop late and lost ones."""

    name = "jitter"

    def __init__(
        self,
        *,
        playout_delay: float = 0.02,
        min_delay: float = 0.005,
        max_delay: float = 0.2,
        stat_interval: float = 1.0,
        max_packets: int = _SEQ_HALF - 1,
        max_bytes: int = MAX_BUFFER_BYTES,
    ) -> None:
        if not all(
            math.isfinite(v)
            for v in (playout_delay, min_delay, max_delay, stat_interval)
        ):
            raise ValueError("jitter delays and interval must be finite")
        if playout_delay < 0:
            raise ValueError("playout_delay must be nonnegative")
        if not 0.0 <= min_delay <= max_delay:
            raise ValueError("require 0 <= min_delay <= max_delay")
        if stat_interval <= 0:
            raise ValueError("stat_interval must be positive")
        if require_uint(max_packets, "max_packets", _SEQ_HALF - 1) == 0:
            raise ValueError("max_packets must be positive")
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer")
        self._max_packets = max_packets
        self._max_bytes = max_bytes
        self._bytes = 0
        self._heap: list[int] = []
        self._reference: int | None = None
        self._source_ssrc: int | None = None
        self._source_frame: int | None = None
        self._released_source_frame: int | None = None
        self._min_delay = min_delay
        self._max_delay = max_delay
        self._stat_interval = stat_interval
        self._playout = min(max_delay, max(min_delay, playout_delay))
        self._jitter = 0.0
        self._prev_transit: float | None = None
        self._buffer: dict[int, _Held] = {}
        self._watermark: int | None = None  # last released/skipped seq
        self._released_set: set[int] = set()
        self._released_order: deque[int] = deque()
        self._last_stat: float | None = None
        # cumulative counters
        self._released = 0
        self._late = 0
        self._dropped_gap = 0
        self._duplicates = 0
        self._discontinuities = 0

    # -- introspection -------------------------------------------------------

    def playout_delay(self) -> float:
        """Current adaptive playout delay (seconds)."""
        return self._playout

    def jitter_estimate(self) -> float:
        """Current RFC 3550 smoothed inter-arrival jitter (seconds)."""
        return self._jitter

    def stats(self) -> Body:
        return {
            "buffered": len(self._buffer),
            "released": self._released,
            "late": self._late,
            "dropped_gap": self._dropped_gap,
            "duplicates": self._duplicates,
            "discontinuities": self._discontinuities,
            "playout_delay": self._playout,
            "jitter_estimate": self._jitter,
        }

    # -- stage ---------------------------------------------------------------

    def process(self, unit: MediaUnit, now: float) -> StageOutcome:
        if self._last_stat is None:
            self._last_stat = now
        if unit.kind is not UnitKind.PACKET:
            return StageOutcome(emit=[unit])  # not ours to buffer

        try:
            require_uint(unit.seq, "sequence", _SEQ_MOD - 1)
            require_uint(unit.pts, "pts", 0xFFFF_FFFF_FFFF_FFFF)
        except ValueError:
            return StageOutcome(
                events=[
                    self._event(
                        EventKind.DROP,
                        unit.seq,
                        unit.pts,
                        now,
                        {"reason": "bad-identity"},
                    )
                ]
            )
        identity = self._source_identity(unit)
        if len(unit.payload) > self._max_bytes:
            return StageOutcome(
                events=[
                    self._event(
                        EventKind.DROP,
                        unit.seq,
                        unit.pts,
                        now,
                        {"reason": "buffer-capacity"},
                    )
                ]
            )
        if (
            identity is not None
            and self._source_ssrc is not None
            and identity[0] != self._source_ssrc
        ):
            return StageOutcome(
                events=[
                    self._event(
                        EventKind.DROP,
                        unit.seq,
                        unit.pts,
                        now,
                        {
                            "reason": "source-changed",
                            "expected_ssrc": self._source_ssrc,
                        },
                    )
                ]
            )
        if (
            identity is not None
            and self._released_source_frame is not None
            and identity[1] < self._released_source_frame
        ):
            self._late += 1
            return StageOutcome(
                events=[
                    self._event(
                        EventKind.LATE,
                        unit.seq,
                        unit.pts,
                        now,
                        {"reason": "older-source-frame"},
                    )
                ]
            )
        events: list[StageEvent] = []
        reference = self._reference if self._reference is not None else unit.seq
        distance = (unit.seq - reference) % _SEQ_MOD
        seq = reference + (distance if distance < _SEQ_HALF else distance - _SEQ_MOD)
        if (
            identity is not None
            and self._source_frame is not None
            and identity[1] > self._source_frame
            and (distance == _SEQ_HALF or seq <= reference)
        ):
            # The full source frame identity proves forward progress even
            # when 16-bit serial arithmetic cannot select the RTP epoch.
            # Restart the ordering window; the number of skipped cycles
            # cannot be recovered from this framing, so do not invent it.
            released_source_frame = self._released_source_frame
            events.extend(self.reset(now, reason="newer-frame-discontinuity").events)
            # This is the same source, so already played frames remain
            # retired even while its new RTP ordering window is empty.
            self._released_source_frame = released_source_frame
            events[-1].detail.update(frame_seq=identity[1], rtp_seq=unit.seq)
            reference, distance, seq = unit.seq, 0, unit.seq
        elif (
            identity is not None
            and self._source_frame is not None
            and identity[1] < self._source_frame
            and seq >= reference
        ):
            self._late += 1
            return StageOutcome(
                events=[
                    self._event(
                        EventKind.LATE,
                        unit.seq,
                        unit.pts,
                        now,
                        {"reason": "older-source-frame"},
                    )
                ]
            )
        if distance == _SEQ_HALF:
            return StageOutcome(
                events=[
                    self._event(
                        EventKind.DROP,
                        unit.seq,
                        unit.pts,
                        now,
                        {"reason": "ambiguous-sequence"},
                    )
                ]
            )
        if seq in self._buffer:
            self._duplicates += 1  # second copy, still held
            return StageOutcome()
        if self._watermark is not None and seq <= self._watermark:
            # At or behind the watermark: its slot has already been released.
            if seq in self._released_set:
                self._duplicates += 1  # second copy of a released packet
                return StageOutcome()
            self._late += 1  # a first, too-late arrival
            return StageOutcome(
                events=[
                    self._event(
                        EventKind.LATE,
                        unit.seq,
                        unit.pts,
                        now,
                        {"watermark": self._watermark % _SEQ_MOD},
                    )
                ]
            )

        if (
            len(self._buffer) >= self._max_packets
            or self._bytes + len(unit.payload) > self._max_bytes
            or (
                self._heap
                and max(reference, seq) - min(self._heap[0], seq) >= _SEQ_HALF
            )
        ):
            return StageOutcome(
                events=[
                    self._event(
                        EventKind.DROP,
                        unit.seq,
                        unit.pts,
                        now,
                        {"reason": "buffer-capacity"},
                    )
                ]
            )
        # Heap keys are unwrapped sequences: release is O(log n), and
        # later sequence cycles never collide with remembered duplicates.
        self._buffer[seq] = _Held(unit=unit, arrival=now)
        heapq.heappush(self._heap, seq)
        self._bytes += len(unit.payload)
        self._reference = max(reference, seq)
        if identity is not None:
            self._source_ssrc = identity[0]
            self._source_frame = max(
                self._source_frame if self._source_frame is not None else identity[1],
                identity[1],
            )
        self._update_jitter(now, unit.pts)
        self._adapt_playout()
        return StageOutcome(
            events=[
                *events,
                self._event(
                    EventKind.HOLD,
                    unit.seq,
                    unit.pts,
                    now,
                    {"deadline": now + self._playout, "playout_delay": self._playout},
                ),
            ]
        )

    @staticmethod
    def _source_identity(unit: MediaUnit) -> tuple[int, int] | None:
        try:
            return (
                require_uint(unit.meta["rtp_ssrc"], "ssrc", 0xFFFF_FFFF),
                require_uint(
                    unit.meta["frame_seq"], "frame_seq", 0xFFFF_FFFF_FFFF_FFFF
                ),
            )
        except (KeyError, ValueError):
            return None

    def reset(self, now: float, *, reason: str = "explicit-reset") -> StageOutcome:
        """Start a new ordering/source window, retaining cumulative stats.

        Call on a known source/session restart. Bare RTP cannot prove a
        new sequence epoch after a half-space outage. Reference inputs can
        instead prove progress with a newer full frame identity.
        This resets this stage only: a new source/session also requires
        rebuilding downstream reassembly and deduplication state.
        """
        discarded = len(self._buffer)
        last_seq = (self._watermark or 0) % _SEQ_MOD
        self._buffer.clear()
        self._heap.clear()
        self._bytes = 0
        self._reference = self._watermark = None
        self._source_ssrc = self._source_frame = None
        self._released_source_frame = None
        self._released_set.clear()
        self._released_order.clear()
        self._prev_transit = None
        self._jitter = 0.0
        self._discontinuities += 1
        return StageOutcome(
            events=[
                self._event(
                    EventKind.ADJUST,
                    last_seq,
                    0,
                    now,
                    {
                        "reason": reason,
                        "discarded_packets": discarded,
                        "unknown_gap": True,
                    },
                )
            ]
        )

    def tick(self, now: float) -> StageOutcome:
        out = StageOutcome()
        self._release_due(now, out)
        self._maybe_stat(now, out)
        return out

    def flush(self, now: float) -> StageOutcome:
        """At terminal input, release held packets at the real finish time.

        No more packets can fill a gap, so terminal release need not wait
        for playout deadlines. The ADJUST event makes this forced drain
        explicit; OUT timestamps and delays retain the supplied clock.
        """
        if not self._buffer:
            return StageOutcome()
        out = StageOutcome(
            events=[
                self._event(
                    EventKind.ADJUST,
                    self._head_seq() % _SEQ_MOD,
                    0,
                    now,
                    {"reason": "finalize", "forced_packets": len(self._buffer)},
                )
            ]
        )
        self._release_due(now, out, force=True)
        self._maybe_stat(now, out)
        return out

    def next_deadline(self) -> float | None:
        if not self._buffer:
            return None
        head = self._head_seq()
        return self._buffer[head].arrival + self._playout

    # -- release -------------------------------------------------------------

    def _release_due(
        self, now: float, out: StageOutcome, *, force: bool = False
    ) -> None:
        while self._buffer:
            head = self._head_seq()
            entry = self._buffer[head]
            if not force and entry.arrival + self._playout > now:
                break  # head not yet due; nothing behind it can jump ahead
            if self._watermark is not None:
                self._skip_gap(head, now, out)
            del self._buffer[head]
            heapq.heappop(self._heap)
            self._bytes -= len(entry.unit.payload)
            self._watermark = head
            self._mark_released(head)
            self._released += 1
            identity = self._source_identity(entry.unit)
            if identity is not None:
                self._released_source_frame = max(
                    self._released_source_frame
                    if self._released_source_frame is not None
                    else identity[1],
                    identity[1],
                )
            out.emit.append(entry.unit)
            out.events.append(
                self._event(
                    EventKind.OUT,
                    entry.unit.seq,
                    entry.unit.pts,
                    now,
                    {"delay": now - entry.arrival, "playout_delay": self._playout},
                )
            )

    def _skip_gap(self, head: int, now: float, out: StageOutcome) -> None:
        """Declare the missing seqs between the watermark and a due head
        lost: they will never make their deadline. Advance past them."""
        assert self._watermark is not None
        missing = head - self._watermark - 1
        if missing <= 0:
            return  # contiguous
        if missing > _MAX_GAP:
            # Pathological hole (or a stream reset): summarise and jump.
            self._dropped_gap += missing
            out.events.append(
                self._event(
                    EventKind.DROP,
                    head % _SEQ_MOD,
                    0,
                    now,
                    {"reason": "gap", "skipped": missing},
                )
            )
            self._watermark = head - 1
            return
        expected = self._watermark + 1
        for _ in range(missing):
            self._dropped_gap += 1
            out.events.append(
                self._event(
                    EventKind.DROP, expected % _SEQ_MOD, 0, now, {"reason": "gap"}
                )
            )
            self._watermark = expected
            expected += 1

    def _head_seq(self) -> int:
        """The serial-next buffered sequence number (head of line)."""
        return self._heap[0]

    def _mark_released(self, seq: int) -> None:
        if seq in self._released_set:
            return
        self._released_set.add(seq)
        self._released_order.append(seq)
        while len(self._released_order) > _RELEASED_WINDOW:
            self._released_set.discard(self._released_order.popleft())

    # -- estimation ----------------------------------------------------------

    def _update_jitter(self, now: float, pts: int) -> None:
        # transit = arrival - pts; the constant clock offset cancels in the
        # difference D, so only inter-arrival variation survives (RFC 3550).
        transit = now - pts / MEDIA_CLOCK_HZ
        if self._prev_transit is not None:
            d = abs(transit - self._prev_transit)
            self._jitter += (d - self._jitter) / _JITTER_GAIN
        self._prev_transit = transit

    def _adapt_playout(self) -> None:
        target = min(self._max_delay, max(self._min_delay, _PLAYOUT_K * self._jitter))
        rate = _GROW_RATE if target > self._playout else _SHRINK_RATE
        self._playout += (target - self._playout) * rate
        self._playout = min(self._max_delay, max(self._min_delay, self._playout))

    # -- helpers -------------------------------------------------------------

    def _maybe_stat(self, now: float, out: StageOutcome) -> None:
        if self._last_stat is None:
            self._last_stat = now
            return
        if now - self._last_stat >= self._stat_interval:
            self._last_stat = now
            out.events.append(
                self._event(
                    EventKind.STAT,
                    (self._watermark or 0) % _SEQ_MOD,
                    0,
                    now,
                    self.stats(),
                )
            )

    def _event(
        self, kind: EventKind, seq: int, pts: int, now: float, detail: Body
    ) -> StageEvent:
        return StageEvent(
            stage=self.name, kind=kind, seq=seq, pts=pts, now=now, detail=detail
        )
