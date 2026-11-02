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
        "discontinuities",
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


def test_jitter_packet_and_byte_budgets_preserve_held_packets() -> None:
    jb = JitterBuffer(max_packets=2, max_bytes=2)
    jb.process(packet(1), T0)
    jb.process(packet(2), T0)
    rejected = jb.process(packet(3), T0)
    assert kinds(rejected.events, EventKind.DROP)
    assert jb.stats()["buffered"] == 2
    assert out_seqs(jb.tick(T0 + 1)) == [1, 2]
    assert jb._bytes == 0


def test_half_space_sequence_is_ambiguous_and_rejected() -> None:
    jb = JitterBuffer()
    jb.process(packet(1), T0)
    rejected = jb.process(packet(32769), T0)
    assert rejected.events[0].detail["reason"] == "ambiguous-sequence"
    assert out_seqs(jb.tick(T0 + 1)) == [1]


def test_repeated_full_sequence_cycles_do_not_become_duplicates() -> None:
    jb = JitterBuffer(min_delay=0, playout_delay=0)
    for i in range(65540):
        seq = i % 65536
        jb.process(packet(seq, pts=i), i / MEDIA_CLOCK_HZ)
        assert out_seqs(jb.tick(i / MEDIA_CLOCK_HZ)) == [seq]
    assert jb.stats()["duplicates"] == 0
    assert jb.stats()["dropped_gap"] == 0


def test_jitter_rejects_nonfinite_delays_and_invalid_budgets() -> None:
    import pytest

    for kwargs in (
        {"playout_delay": float("nan")},
        {"max_delay": float("inf")},
        {"stat_interval": float("nan")},
        {"max_packets": 32768},
        {"max_bytes": 0},
    ):
        with pytest.raises(ValueError):
            JitterBuffer(**kwargs)


def test_full_frame_identity_recovers_after_more_than_half_sequence_outage() -> None:
    jb = JitterBuffer()
    before = packet(10000, pts=150000).with_payload(b"x", frame_seq=100, rtp_ssrc=7)
    after = packet(50000, pts=300000).with_payload(b"x", frame_seq=200, rtp_ssrc=7)
    jb.process(before, T0)
    assert out_seqs(jb.tick(T0 + 1)) == [10000]
    result = jb.process(after, T0 + 2)
    adjustments = kinds(result.events, EventKind.ADJUST)
    assert adjustments[0].detail["reason"] == "newer-frame-discontinuity"
    assert adjustments[0].detail["unknown_gap"] is True
    assert out_seqs(jb.tick(T0 + 3)) == [50000]
    assert jb.stats()["discontinuities"] == 1
    assert jb.stats()["dropped_gap"] == 0  # the number of missing RTP cycles is unknown
    assert kinds(jb.process(before, T0 + 4).events, EventKind.LATE)


def test_bare_rtp_outage_requires_explicit_reset_without_epoch_evidence() -> None:
    jb = JitterBuffer()
    jb.process(packet(10000), T0)
    jb.tick(T0 + 1)
    assert kinds(jb.process(packet(50000), T0 + 2).events, EventKind.LATE)
    reset = jb.reset(T0 + 2)
    assert reset.events[0].detail["reason"] == "explicit-reset"
    jb.process(packet(50000), T0 + 2)
    assert out_seqs(jb.tick(T0 + 3)) == [50000]


def test_source_restart_and_ssrc_change_require_explicit_reset() -> None:
    jb = JitterBuffer()
    first = packet(10000).with_payload(b"x", frame_seq=100, rtp_ssrc=7)
    restart = packet(0).with_payload(b"x", frame_seq=0, rtp_ssrc=8)
    jb.process(first, T0)
    jb.tick(T0 + 1)
    assert jb.process(restart, T0 + 2).events[0].detail["reason"] == "source-changed"
    jb.reset(T0 + 2)
    jb.process(restart, T0 + 2)
    assert out_seqs(jb.tick(T0 + 3)) == [0]


def test_discontinuity_retires_held_packets_with_bounded_diagnostic() -> None:
    jb = JitterBuffer()
    jb.process(packet(10000).with_payload(b"x", frame_seq=100, rtp_ssrc=7), T0)
    jb.tick(T0 + 1)
    jb.process(packet(10001).with_payload(b"x", frame_seq=100, rtp_ssrc=7), T0 + 2)
    result = jb.process(
        packet(50000).with_payload(b"x", frame_seq=200, rtp_ssrc=7), T0 + 2
    )
    assert kinds(result.events, EventKind.ADJUST)[0].detail["discarded_packets"] == 1
    assert jb.stats()["buffered"] == 1
    assert out_seqs(jb.tick(T0 + 3)) == [50000]


def test_full_frame_progress_handles_outage_before_first_playout() -> None:
    jb = JitterBuffer()
    before = packet(10000).with_payload(b"x", frame_seq=100, rtp_ssrc=7)
    after = packet(50000).with_payload(b"x", frame_seq=200, rtp_ssrc=7)
    jb.process(before, T0)
    assert kinds(jb.process(after, T0).events, EventKind.ADJUST)
    # The old epoch must not reappear as a future packet before tick either.
    assert kinds(jb.process(before, T0).events, EventKind.LATE)
    assert out_seqs(jb.tick(T0 + 1)) == [50000]


def test_wire_discontinuity_keeps_already_played_frame_floor() -> None:
    from yoke.media.stages.codec import PassthroughCodec
    from yoke.media.stages.packetize import Packetizer
    from yoke.media.wire import pack_packet, unpack_packet

    def wire_packets(frame_seq: int, start_seq: int) -> list[MediaUnit]:
        frame = MediaUnit(
            UnitKind.FRAME, frame_seq, frame_seq * 1500, b"abcd", "a" * 64
        )
        packetizer = Packetizer(start_seq=start_seq, ssrc=7)
        return [
            unpack_packet(pack_packet(pkt))
            for part in PassthroughCodec(4).encode(frame, 1.0)
            for pkt in packetizer.process(part, 0.0).emit
        ]

    jb = JitterBuffer()
    for unit in wire_packets(20000, 14464):
        jb.process(unit, 0.0)
    assert [unit.meta["frame_seq"] for unit in jb.tick(1.0).emit] == [20000] * 4
    for unit in wire_packets(30000, 54464):
        jb.process(unit, 2.0)
    for unit in wire_packets(10000, 40000):
        rejected = jb.process(unit, 2.0)
        assert kinds(rejected.events, EventKind.LATE)
        assert not kinds(rejected.events, EventKind.HOLD)
    assert [unit.meta["frame_seq"] for unit in jb.tick(3.0).emit] == [30000] * 4
    assert jb.stats()["dropped_gap"] == 0
    assert jb.jitter_estimate() == 0.0
    # An explicit source/session reset intentionally opens a fresh identity
    # window; callers rebuilding a receiver can now accept its new frame 0.
    jb.reset(4.0)
    for unit in wire_packets(0, 0):
        jb.process(unit, 4.0)
    assert [unit.meta["frame_seq"] for unit in jb.tick(5.0).emit] == [0] * 4


def test_terminal_flush_releases_future_held_at_actual_finish_time() -> None:
    jb = JitterBuffer(playout_delay=0.2, min_delay=0.2, max_delay=0.2)
    for seq in (2, 0, 1):
        jb.process(packet(seq), T0)
    finished_at = T0 + 0.001
    assert jb.next_deadline() is not None and jb.next_deadline() > finished_at
    result = jb.flush(finished_at)
    assert out_seqs(result) == [0, 1, 2]
    assert kinds(result.events, EventKind.ADJUST)[0].detail == {
        "reason": "finalize",
        "forced_packets": 3,
    }
    released = kinds(result.events, EventKind.OUT)
    assert all(event.now == finished_at for event in released)
    assert all(event.detail["delay"] == finished_at - T0 for event in released)
    assert jb.next_deadline() is None
    assert jb._bytes == 0
    assert not jb.flush(finished_at).events


def test_terminal_flush_reports_known_gap_without_extending_clock() -> None:
    jb = JitterBuffer(playout_delay=0.2, min_delay=0.2, max_delay=0.2)
    for seq in (0, 2):
        jb.process(packet(seq), T0)
    result = jb.flush(T0)
    assert out_seqs(result) == [0, 2]
    assert [event.seq for event in kinds(result.events, EventKind.DROP)] == [1]
    assert jb.stats()["dropped_gap"] == 1
    assert all(event.now == T0 for event in result.events)
