"""Unit tests for the integrity sink (deterministic, sans-I/O).

Frames are built directly (via the source, then tampered with
``dataclasses.replace``) and handed to ``process(unit, now)`` with a
fabricated clock. No sockets, no wall time.
"""

from __future__ import annotations

import dataclasses

from yoke.media.stages.sink import IntegritySink
from yoke.media.stages.source import FRAME_HEADER_LEN, SyntheticSource
from yoke.media.types import (
    EventKind,
    MediaUnit,
    NetworkReport,
    StageEvent,
    UnitKind,
)

T0 = 1_754_000_000.0


def gen_frames(n: int, *, capture_now: float = T0, seed: int = 0) -> list[MediaUnit]:
    """n contiguous FRAME units (seq 0..n-1) from one source."""
    src = SyntheticSource(width=1920, height=1080, fps=60, seed=seed)
    return [src.frame(capture_now).emit[0] for _ in range(n)]


def corrupt_body(unit: MediaUnit) -> MediaUnit:
    """Keep the header (seq/pts intact) but scramble the body: a lossless
    path sees a digest mismatch; a lossy path still finds the frame."""
    header = unit.payload[:FRAME_HEADER_LEN]
    body = bytes(len(unit.payload) - FRAME_HEADER_LEN)
    return dataclasses.replace(unit, payload=header + body)


def as_lossy(unit: MediaUnit) -> MediaUnit:
    return dataclasses.replace(unit, meta={**unit.meta, "lossless": False})


def integrity_events(events: list[StageEvent]) -> list[StageEvent]:
    return [e for e in events if e.kind is EventKind.INTEGRITY]


def stat_events(events: list[StageEvent]) -> list[StageEvent]:
    return [e for e in events if e.kind is EventKind.STAT]


# -- verification: lossless --------------------------------------------------


def test_lossless_good_frame_counts_ok_and_emits_integrity() -> None:
    sink = IntegritySink()
    unit = gen_frames(1)[0]
    out = sink.process(unit, T0 + 0.01)
    assert sink.metrics()["frames_ok"] == 1
    assert sink.metrics()["frames_bad"] == 0
    ev = integrity_events(out.events)
    assert len(ev) == 1
    assert ev[0].detail == {"ok": True, "reason": "ok", "lossless": True}


def test_lossless_digest_mismatch_flagged_bad() -> None:
    sink = IntegritySink()
    bad = corrupt_body(gen_frames(1)[0])
    out = sink.process(bad, T0)
    assert sink.metrics()["frames_bad"] == 1
    assert sink.metrics()["frames_ok"] == 0
    assert integrity_events(out.events)[0].detail["reason"] == "digest-mismatch"


def test_seq_mismatch_flagged_bad_even_lossless() -> None:
    sink = IntegritySink()
    unit = gen_frames(1)[0]
    tampered = dataclasses.replace(unit, seq=999)
    out = sink.process(tampered, T0)
    assert sink.metrics()["frames_bad"] == 1
    assert integrity_events(out.events)[0].detail["reason"] == "seq-mismatch"


def test_short_payload_flagged_bad() -> None:
    sink = IntegritySink()
    unit = gen_frames(1)[0]
    stub = dataclasses.replace(unit, payload=b"\x00\x01")
    out = sink.process(stub, T0)
    assert sink.metrics()["frames_bad"] == 1
    assert integrity_events(out.events)[0].detail["reason"] == "short-payload"


# -- verification: lossy -----------------------------------------------------


def test_lossy_digest_mismatch_is_ok() -> None:
    sink = IntegritySink()
    lossy = as_lossy(corrupt_body(gen_frames(1)[0]))
    out = sink.process(lossy, T0)
    assert sink.metrics()["frames_ok"] == 1
    assert sink.metrics()["frames_bad"] == 0
    detail = integrity_events(out.events)[0].detail
    assert detail == {"ok": True, "reason": "ok", "lossless": False}


def test_lossy_still_requires_matching_seq() -> None:
    sink = IntegritySink()
    lossy = as_lossy(gen_frames(1)[0])
    tampered = dataclasses.replace(lossy, seq=42)
    sink.process(tampered, T0)
    assert sink.metrics()["frames_bad"] == 1


# -- ordering metrics: gaps / dupes / reorder --------------------------------


def test_gap_detected_and_filled_by_late_frame() -> None:
    sink = IntegritySink()
    frames = gen_frames(4)  # seq 0,1,2,3
    for seq in (0, 1, 3):
        sink.process(frames[seq], T0)
    assert sink.metrics()["lost"] == 1  # seq 2 missing in range [0,3]
    # the late arrival of seq 2 fills the hole and counts as reordered
    sink.process(frames[2], T0)
    assert sink.metrics()["lost"] == 0
    assert sink.metrics()["reordered"] == 1


def test_duplicate_detected() -> None:
    sink = IntegritySink()
    frames = gen_frames(2)
    sink.process(frames[0], T0)
    sink.process(frames[1], T0)
    sink.process(frames[0], T0)  # duplicate seq 0
    assert sink.metrics()["duplicated"] == 1
    # every arrival still lands in ok/bad (dups included)
    assert sink.metrics()["frames_ok"] == 3


def test_reorder_counts_out_of_order_arrivals() -> None:
    sink = IntegritySink()
    frames = gen_frames(3)
    for seq in (2, 0, 1):
        sink.process(frames[seq], T0)
    assert sink.metrics()["reordered"] == 2  # 0 and 1 arrive after 2
    assert sink.metrics()["lost"] == 0


# -- latency -----------------------------------------------------------------


def test_latency_from_capture_now() -> None:
    sink = IntegritySink()
    src = SyntheticSource(width=64, height=64, fps=60)
    u0 = src.frame(T0).emit[0]  # captured at T0
    u1 = src.frame(T0 + 0.2).emit[0]  # captured at T0 + 0.2
    sink.process(u0, T0 + 0.1)  # transit 0.1
    sink.process(u1, T0 + 0.35)  # transit 0.15
    assert abs(sink.metrics()["last_latency"] - 0.15) < 1e-6
    assert abs(sink.metrics()["mean_latency"] - 0.125) < 1e-6


def test_missing_capture_now_leaves_latency_zero() -> None:
    sink = IntegritySink()
    unit = gen_frames(1)[0]
    stripped = dataclasses.replace(unit, meta={"lossless": True})
    sink.process(stripped, T0 + 1.0)
    assert sink.metrics()["last_latency"] == 0.0
    assert sink.metrics()["mean_latency"] == 0.0


# -- observability: STAT + feedback ------------------------------------------


def test_periodic_stat_event() -> None:
    sink = IntegritySink(stat_every=2)
    frames = gen_frames(4)
    stats = 0
    for f in frames:
        out = sink.process(f, T0)
        stats += len(stat_events(out.events))
    assert stats == 2  # emitted on the 2nd and 4th processed frame


def test_feedback_emitted_first_frame_and_on_change() -> None:
    sink = IntegritySink(report_every=1000)  # disable the periodic path
    frames = gen_frames(4)  # seq 0,1,2,3
    # first frame: report_key changes from None -> emit
    out = sink.process(frames[0], T0)
    assert len(out.feedback) == 1
    assert isinstance(out.feedback[0], NetworkReport)
    assert out.feedback[0].loss == 0.0
    # contiguous second frame: nothing changed -> no feedback
    out = sink.process(frames[1], T0)
    assert out.feedback == []
    # skip seq 2, deliver seq 3: loss appears -> feedback
    out = sink.process(frames[3], T0)
    assert len(out.feedback) == 1
    assert out.feedback[0].loss > 0.0


def test_feedback_emitted_periodically() -> None:
    sink = IntegritySink(report_every=2)
    frames = gen_frames(4)
    counts = [len(sink.process(f, T0).feedback) for f in frames]
    # frame1: change from None; frame2: periodic; frame3: no change/no period;
    # frame4: periodic.
    assert counts == [1, 1, 0, 1]


def test_congestion_injection_surfaces_in_feedback() -> None:
    sink = IntegritySink(report_every=1000)
    frames = gen_frames(2)
    sink.process(frames[0], T0)  # first feedback, congestion 0.0
    sink.set_congestion(0.7)
    out = sink.process(frames[1], T0)
    assert len(out.feedback) == 1
    assert out.feedback[0].congestion == 0.7


def test_report_carries_frame_counts() -> None:
    sink = IntegritySink(report_every=1)  # report every frame
    good = gen_frames(1)[0]
    sink.process(good, T0)  # seq 0, ok
    bad = corrupt_body(dataclasses.replace(good, seq=1))  # seq 1, corrupt body
    out = sink.process(bad, T0)
    assert len(out.feedback) == 1
    report = out.feedback[0]
    assert report.frames_ok == 1
    assert report.frames_bad == 1


# -- metrics shape -----------------------------------------------------------


def test_metrics_shape() -> None:
    sink = IntegritySink()
    sink.process(gen_frames(1)[0], T0)
    assert set(sink.metrics()) == {
        "frames_ok",
        "frames_bad",
        "lost",
        "duplicated",
        "reordered",
        "last_latency",
        "mean_latency",
    }


def test_terminal_stage_emits_nothing_downstream() -> None:
    sink = IntegritySink()
    unit = gen_frames(1)[0]
    assert unit.kind is UnitKind.FRAME
    out = sink.process(unit, T0)
    assert out.emit == []
