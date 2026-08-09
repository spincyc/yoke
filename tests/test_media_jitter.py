"""Unit tests for the adaptive jitter buffer (deterministic, sans-I/O).

PACKET MediaUnits are built directly (``seq`` = the 16-bit RTP sequence
number, ``pts`` the 90 kHz timestamp) and handed to ``process(unit,
now)`` / ``tick(now)`` against a fabricated clock. No sockets, no wall
time, no sleeps: every release is asserted against the fake clock within
a small epsilon.
"""

from __future__ import annotations

from yoke.media.stages.jitter import JitterBuffer
from yoke.media.types import (
    MEDIA_CLOCK_HZ,
    EventKind,
    MediaUnit,
    StageEvent,
    StageOutcome,
    UnitKind,
)

T0 = 1_754_000_000.0
EPS = 1e-9
# 60 fps in 90 kHz ticks: one frame is 1500 ticks == 1/60 s.
FRAME_TICKS = MEDIA_CLOCK_HZ // 60


def packet(seq: int, *, pts: int = 0, payload: bytes = b"x") -> MediaUnit:
    return MediaUnit(
        kind=UnitKind.PACKET,
        seq=seq,
        pts=pts,
        payload=payload,
        source_digest="d",
        meta={"frame_seq": pts, "frag": 0},
    )


def kinds(events: list[StageEvent], kind: EventKind) -> list[StageEvent]:
    return [e for e in events if e.kind is kind]


def out_seqs(outcome: StageOutcome) -> list[int]:
    return [u.seq for u in outcome.emit]


# -- in-order playout --------------------------------------------------------


def test_in_order_release_after_playout_delay_in_order() -> None:
    jb = JitterBuffer(playout_delay=0.02)
    for i in range(3):
        out = jb.process(packet(i, pts=i * FRAME_TICKS), T0)
        assert kinds(out.events, EventKind.HOLD)  # held, not released
        assert out.emit == []

    delay = jb.playout_delay()
    # Just before the deadline nothing is due.
    early = jb.tick(T0 + delay - 1e-4)
    assert early.emit == []
    # At the deadline all three are due, in ascending seq order.
    due = jb.tick(T0 + delay)
    assert out_seqs(due) == [0, 1, 2]
    assert [e.seq for e in kinds(due.events, EventKind.OUT)] == [0, 1, 2]
    assert jb.stats()["released"] == 3
    assert jb.stats()["buffered"] == 0


def test_hold_does_not_release_immediately() -> None:
    jb = JitterBuffer()
    out = jb.process(packet(5), T0)
    assert out.emit == []
    assert len(kinds(out.events, EventKind.HOLD)) == 1


# -- reordering --------------------------------------------------------------


def test_reordered_packets_released_in_seq_order() -> None:
    jb = JitterBuffer(playout_delay=0.02)
    for seq in (2, 0, 1):  # arrive out of order
        jb.process(packet(seq, pts=seq * FRAME_TICKS), T0)
    due = jb.tick(T0 + jb.playout_delay() + EPS)
    assert out_seqs(due) == [0, 1, 2]


# -- late arrival ------------------------------------------------------------


def test_packet_after_slot_released_is_late_and_dropped() -> None:
    jb = JitterBuffer(playout_delay=0.02)
    jb.process(packet(10), T0)
    jb.tick(T0 + jb.playout_delay() + EPS)  # release 10, watermark = 10
    assert jb.stats()["released"] == 1

    late = jb.process(packet(9), T0 + 0.05)  # its slot is already behind us
    assert late.emit == []
    assert len(kinds(late.events, EventKind.LATE)) == 1
    assert jb.stats()["late"] == 1
    assert jb.stats()["buffered"] == 0


# -- genuine gap -------------------------------------------------------------


def test_gap_whose_deadline_passes_is_skipped_and_flow_continues() -> None:
    jb = JitterBuffer(playout_delay=0.02)
    jb.process(packet(1, pts=FRAME_TICKS), T0)
    jb.process(packet(3, pts=3 * FRAME_TICKS), T0)  # seq 2 is missing
    due = jb.tick(T0 + jb.playout_delay() + EPS)
    # 1 and 3 play out; 2 is declared lost.
    assert out_seqs(due) == [1, 3]
    assert jb.stats()["dropped_gap"] == 1
    gap = kinds(due.events, EventKind.DROP)
    assert [e.seq for e in gap] == [2]

    # Later packets still flow.
    jb.process(packet(4, pts=4 * FRAME_TICKS), T0 + 0.05)
    later = jb.tick(T0 + 0.05 + jb.playout_delay() + EPS)
    assert out_seqs(later) == [4]


# -- duplicates --------------------------------------------------------------


def test_duplicate_dropped_without_disturbing_order() -> None:
    jb = JitterBuffer(playout_delay=0.02)
    jb.process(packet(1, pts=FRAME_TICKS), T0)
    jb.process(packet(3, pts=3 * FRAME_TICKS), T0)
    dup = jb.process(packet(3, pts=3 * FRAME_TICKS), T0)  # duplicate of a held one
    assert dup.emit == []
    assert dup.events == []  # dropped silently
    assert jb.stats()["duplicates"] == 1
    assert jb.stats()["late"] == 0
    # Order and content are undisturbed.
    jb.process(packet(2, pts=2 * FRAME_TICKS), T0)
    due = jb.tick(T0 + jb.playout_delay() + EPS)
    assert out_seqs(due) == [1, 2, 3]


def test_duplicate_of_released_counts_as_duplicate_not_late() -> None:
    jb = JitterBuffer(playout_delay=0.02)
    jb.process(packet(7), T0)
    jb.tick(T0 + jb.playout_delay() + EPS)  # release 7
    dup = jb.process(packet(7), T0 + 0.05)  # second copy of a released packet
    assert dup.emit == []
    assert dup.events == []
    assert jb.stats()["duplicates"] == 1
    assert jb.stats()["late"] == 0


# -- 16-bit wraparound -------------------------------------------------------


def test_wraparound_ordering_is_correct() -> None:
    jb = JitterBuffer(playout_delay=0.02)
    # Arrive shuffled across the 16-bit wrap.
    for seq in (0, 65535, 1, 65534):
        jb.process(packet(seq), T0)
    due = jb.tick(T0 + jb.playout_delay() + EPS)
    assert out_seqs(due) == [65534, 65535, 0, 1]


def test_wraparound_late_after_release() -> None:
    jb = JitterBuffer(playout_delay=0.02)
    jb.process(packet(65535), T0)
    jb.process(packet(0), T0)
    jb.tick(T0 + jb.playout_delay() + EPS)  # release 65535 then 0
    # 65534 sits behind the wrapped watermark -> late, not a huge future gap.
    late = jb.process(packet(65534), T0 + 0.05)
    assert late.emit == []
    assert len(kinds(late.events, EventKind.LATE)) == 1
    assert jb.stats()["late"] == 1


def test_wraparound_gap_skipped() -> None:
    jb = JitterBuffer(playout_delay=0.02)
    jb.process(packet(65535), T0)
    jb.process(packet(1), T0)  # seq 0 missing, across the wrap
    due = jb.tick(T0 + jb.playout_delay() + EPS)
    assert out_seqs(due) == [65535, 1]
    assert jb.stats()["dropped_gap"] == 1
    assert [e.seq for e in kinds(due.events, EventKind.DROP)] == [0]


# -- next_deadline -----------------------------------------------------------


def test_next_deadline_none_when_empty() -> None:
    jb = JitterBuffer()
    assert jb.next_deadline() is None


def test_next_deadline_is_release_instant_and_tick_releases_exactly_due() -> None:
    jb = JitterBuffer(playout_delay=0.02)
    jb.process(packet(0), T0)
    jb.process(packet(1), T0 + 0.01)  # later arrival -> later deadline

    deadline = jb.next_deadline()
    assert deadline is not None
    assert abs(deadline - (T0 + jb.playout_delay())) < EPS

    # Tick a hair before: nothing due.
    assert jb.tick(deadline - 1e-4).emit == []
    # Tick at the deadline: exactly seq 0 (seq 1 is not due yet).
    at = jb.tick(deadline)
    assert out_seqs(at) == [0]
    assert jb.stats()["buffered"] == 1

    # next_deadline now points at seq 1's release.
    d2 = jb.next_deadline()
    assert d2 is not None
    assert abs(d2 - (T0 + 0.01 + jb.playout_delay())) < EPS
    assert out_seqs(jb.tick(d2)) == [1]


# -- adaptive playout --------------------------------------------------------


def test_playout_grows_under_jitter_then_shrinks_when_smooth() -> None:
    jb = JitterBuffer(playout_delay=0.02, min_delay=0.005, max_delay=0.2)
    base = jb.playout_delay()

    # High inter-arrival variance: alternate tight/loose spacing while the
    # media clock advances at a steady one-frame cadence.
    now = T0
    frame_s = FRAME_TICKS / MEDIA_CLOCK_HZ
    for i in range(60):
        jb.process(packet(i, pts=i * FRAME_TICKS), now)
        now += frame_s + (0.04 if i % 2 == 0 else -0.02)
    grown = jb.playout_delay()
    assert grown > base  # jitter pushed the delay up
    assert jb.jitter_estimate() > 0.0

    # Now a perfectly smooth stream: arrivals match the media cadence.
    for i in range(60, 400):
        jb.process(packet(i, pts=i * FRAME_TICKS), now)
        now += frame_s
    assert jb.playout_delay() < grown  # reclaimed latency
    assert jb.playout_delay() >= 0.005 - EPS  # still bounded below by min_delay


def test_playout_stays_within_bounds() -> None:
    jb = JitterBuffer(playout_delay=0.02, min_delay=0.01, max_delay=0.03)
    now = T0
    for i in range(200):
        jb.process(packet(i, pts=i * FRAME_TICKS), now)
        now += 0.001 if i % 2 == 0 else 0.5  # violent variance
        assert 0.01 - EPS <= jb.playout_delay() <= 0.03 + EPS


# -- periodic STAT -----------------------------------------------------------


def test_stat_emitted_periodically_on_tick() -> None:
    jb = JitterBuffer(playout_delay=0.02, stat_interval=1.0)
    jb.process(packet(0), T0)  # establishes the stat baseline at T0

    # A tick within the interval emits no STAT.
    within = jb.tick(T0 + 0.5)
    assert kinds(within.events, EventKind.STAT) == []

    # A tick past the interval emits exactly one STAT carrying the schema.
    past = jb.tick(T0 + 1.0)
    stats = kinds(past.events, EventKind.STAT)
    assert len(stats) == 1
    assert set(stats[0].detail) == {
        "buffered",
        "released",
        "late",
        "dropped_gap",
        "duplicates",
        "playout_delay",
        "jitter_estimate",
    }


# -- passthrough of non-packet units -----------------------------------------


def test_non_packet_unit_passes_through_unbuffered() -> None:
    jb = JitterBuffer()
    frame = MediaUnit(
        kind=UnitKind.FRAME, seq=0, pts=0, payload=b"f", source_digest="d"
    )
    out = jb.process(frame, T0)
    assert out.emit == [frame]
    assert jb.stats()["buffered"] == 0
