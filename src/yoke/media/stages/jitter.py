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

from collections import deque
from dataclasses import dataclass
from typing import Final

from yoke.media.pipeline import StageBase
from yoke.media.types import (
    MEDIA_CLOCK_HZ,
    Body,
    EventKind,
    MediaUnit,
    StageEvent,
    StageOutcome,
    UnitKind,
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


def _seq_lt(a: int, b: int) -> bool:
    """RFC 1982 serial "less than" for 16-bit sequence numbers."""
    return 0 < ((b - a) % _SEQ_MOD) < _SEQ_HALF


def _seq_dist(a: int, b: int) -> int:
    """Forward distance from ``a`` to ``b`` in serial arithmetic."""
    return (b - a) % _SEQ_MOD


def _serial_min(seqs: list[int]) -> int:
    """The serial-smallest sequence number in a cluster (span < half)."""
    candidate = seqs[0]
    for s in seqs[1:]:
        if _seq_lt(s, candidate):
            candidate = s
    return candidate


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
    ) -> None:
        if not 0.0 <= min_delay <= max_delay:
            raise ValueError("require 0 <= min_delay <= max_delay")
        if stat_interval <= 0:
            raise ValueError("stat_interval must be positive")
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
            "playout_delay": self._playout,
            "jitter_estimate": self._jitter,
        }

    # -- stage ---------------------------------------------------------------

    def process(self, unit: MediaUnit, now: float) -> StageOutcome:
        if self._last_stat is None:
            self._last_stat = now
        if unit.kind is not UnitKind.PACKET:
            return StageOutcome(emit=[unit])  # not ours to buffer

        seq = unit.seq % _SEQ_MOD
        if seq in self._buffer:
            self._duplicates += 1  # second copy, still held
            return StageOutcome()
        if self._watermark is not None and not _seq_lt(self._watermark, seq):
            # At or behind the watermark: its slot has already been released.
            if seq in self._released_set:
                self._duplicates += 1  # second copy of a released packet
                return StageOutcome()
            self._late += 1  # a first, too-late arrival
            return StageOutcome(
                events=[
                    self._event(
                        EventKind.LATE,
                        seq,
                        unit.pts,
                        now,
                        {"watermark": self._watermark},
                    )
                ]
            )

        # Accept: buffer, update jitter/playout, hold for playout.
        self._buffer[seq] = _Held(unit=unit, arrival=now)
        self._update_jitter(now, unit.pts)
        self._adapt_playout()
        return StageOutcome(
            events=[
                self._event(
                    EventKind.HOLD,
                    seq,
                    unit.pts,
                    now,
                    {"deadline": now + self._playout, "playout_delay": self._playout},
                )
            ]
        )

    def tick(self, now: float) -> StageOutcome:
        out = StageOutcome()
        self._release_due(now, out)
        self._maybe_stat(now, out)
        return out

    def next_deadline(self) -> float | None:
        if not self._buffer:
            return None
        head = self._head_seq()
        return self._buffer[head].arrival + self._playout

    # -- release -------------------------------------------------------------

    def _release_due(self, now: float, out: StageOutcome) -> None:
        while self._buffer:
            head = self._head_seq()
            entry = self._buffer[head]
            if entry.arrival + self._playout > now:
                break  # head not yet due; nothing behind it can jump ahead
            if self._watermark is not None:
                self._skip_gap(head, now, out)
            del self._buffer[head]
            self._watermark = head
            self._mark_released(head)
            self._released += 1
            out.emit.append(entry.unit)
            out.events.append(
                self._event(
                    EventKind.OUT,
                    head,
                    entry.unit.pts,
                    now,
                    {"delay": now - entry.arrival, "playout_delay": self._playout},
                )
            )

    def _skip_gap(self, head: int, now: float, out: StageOutcome) -> None:
        """Declare the missing seqs between the watermark and a due head
        lost: they will never make their deadline. Advance past them."""
        assert self._watermark is not None
        missing = _seq_dist(self._watermark, head) - 1
        if missing <= 0:
            return  # contiguous
        if missing > _MAX_GAP:
            # Pathological hole (or a stream reset): summarise and jump.
            self._dropped_gap += missing
            out.events.append(
                self._event(
                    EventKind.DROP,
                    head,
                    0,
                    now,
                    {"reason": "gap", "skipped": missing},
                )
            )
            self._watermark = (head - 1) % _SEQ_MOD
            return
        expected = (self._watermark + 1) % _SEQ_MOD
        for _ in range(missing):
            self._dropped_gap += 1
            out.events.append(
                self._event(EventKind.DROP, expected, 0, now, {"reason": "gap"})
            )
            self._watermark = expected
            expected = (expected + 1) % _SEQ_MOD

    def _head_seq(self) -> int:
        """The serial-next buffered sequence number (head of line)."""
        if self._watermark is None:
            return _serial_min(list(self._buffer))
        watermark = self._watermark
        return min(self._buffer, key=lambda s: _seq_dist(watermark, s))

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
                self._event(EventKind.STAT, self._watermark or 0, 0, now, self.stats())
            )

    def _event(
        self, kind: EventKind, seq: int, pts: int, now: float, detail: Body
    ) -> StageEvent:
        return StageEvent(
            stage=self.name, kind=kind, seq=seq, pts=pts, now=now, detail=detail
        )
