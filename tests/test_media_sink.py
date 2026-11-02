"""Unit tests for the integrity sink (deterministic, sans-I/O).

Frames are built directly (via the source, then tampered with
``dataclasses.replace``) and handed to ``process(unit, now)`` with a
fabricated clock. No sockets, no wall time.
"""

from __future__ import annotations

import dataclasses
import json
import math

import pytest

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
    assert ev[0].detail == {
        "ok": True,
        "reason": "ok",
        "lossless": True,
        "latency": pytest.approx(0.01, abs=1e-6),
    }


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
    assert detail == {"ok": True, "reason": "ok", "lossless": False, "latency": 0.0}


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


def test_missing_capture_now_leaves_latency_unknown() -> None:
    sink = IntegritySink()
    unit = gen_frames(1)[0]
    stripped = dataclasses.replace(unit, meta={"lossless": True})
    sink.process(stripped, T0 + 1.0)
    assert sink.metrics()["last_latency"] is None
    assert sink.metrics()["mean_latency"] is None


def test_latency_distinguishes_unknown_measured_zero_and_positive_samples() -> None:
    sink = IntegritySink(stat_every=1)
    src = SyntheticSource(width=64, height=64, fps=60)
    assert sink.metrics()["last_latency"] is None
    assert sink.metrics()["mean_latency"] is None
    unknown = dataclasses.replace(src.frame(10).emit[0], meta={"lossless": True})
    out = sink.process(unknown, 10.5)
    assert "latency" not in integrity_events(out.events)[0].detail
    assert stat_events(out.events)[0].detail["mean_latency"] is None
    assert (
        json.loads(json.dumps(sink.metrics(), allow_nan=False))["last_latency"] is None
    )

    out = sink.process(src.frame(11).emit[0], 11)
    assert integrity_events(out.events)[0].detail["latency"] == 0.0
    assert sink.metrics()["last_latency"] == 0.0
    assert sink.metrics()["mean_latency"] == 0.0

    out = sink.process(src.frame(12).emit[0], 12.25)
    assert integrity_events(out.events)[0].detail["latency"] == 0.25
    assert sink.metrics()["last_latency"] == 0.25
    assert sink.metrics()["mean_latency"] == 0.125


def test_missing_later_timestamps_retain_only_valid_latency_samples() -> None:
    sink = IntegritySink()
    src = SyntheticSource(width=64, height=64, fps=60)
    sink.process(src.frame(10).emit[0], 10.25)
    for capture in (None, math.nan, 20):
        unit = src.frame(11).emit[0]
        out = sink.process(unit.with_payload(unit.payload, capture_now=capture), 12)
        assert "latency" not in integrity_events(out.events)[0].detail
        assert sink.metrics()["last_latency"] == 0.25
        assert sink.metrics()["mean_latency"] == 0.25


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


def test_incomplete_lossy_frame_cannot_pass_structural_integrity() -> None:
    sink = IntegritySink()
    unit = as_lossy(gen_frames(1)[0]).with_payload(
        gen_frames(1)[0].payload, incomplete=True
    )
    out = sink.process(unit, T0)
    assert integrity_events(out.events)[0].detail["reason"] == "incomplete-frame"
    assert sink.metrics()["frames_bad"] == 1


def test_history_bounds_storage_and_freezes_retired_gaps() -> None:
    sink = IntegritySink(history_size=3)
    frames = gen_frames(8)
    for index in (0, 2, 3, 4, 5, 6, 7):
        sink.process(frames[index], T0)
    assert sink.metrics()["lost"] == 1
    assert len(sink._seen) <= 3
    assert len(sink._sequence_heap) <= 3
    out = sink.process(frames[1], T0)
    assert sink.metrics()["lost"] == 1
    assert sink.metrics()["reordered"] == 0
    assert any(e.detail.get("why") == "outside-history" for e in out.events)
    # Integrity still counts every arrival, even outside the ordering window.
    assert sink.metrics()["frames_ok"] == 8
    sink.process(frames[6], T0)
    assert sink.metrics()["duplicated"] == 1


def test_recent_reordering_remains_exact_after_window_moves() -> None:
    sink = IntegritySink(history_size=3)
    frames = gen_frames(7)
    for index in (0, 1, 2, 3, 4, 6, 5, 5):
        sink.process(frames[index], T0)
    assert sink.metrics()["lost"] == 0
    assert sink.metrics()["reordered"] == 1
    assert sink.metrics()["duplicated"] == 1


@pytest.mark.parametrize(
    "capture", [math.nan, math.inf, -math.inf, -1.0, True, "unknown", None]
)
def test_invalid_capture_time_cannot_poison_latency(capture: object) -> None:
    sink = IntegritySink()
    unit = gen_frames(1)[0].with_payload(gen_frames(1)[0].payload, capture_now=capture)
    out = sink.process(unit, T0)
    assert "latency" not in integrity_events(out.events)[0].detail
    assert sink.metrics()["mean_latency"] is None
    assert out.feedback[0].jitter == 0.0


def test_future_capture_time_is_unknown_latency() -> None:
    sink = IntegritySink()
    out = sink.process(gen_frames(1, capture_now=T0 + 1)[0], T0)
    assert "latency" not in integrity_events(out.events)[0].detail
    assert sink.metrics()["last_latency"] is None


@pytest.mark.parametrize("value", [math.nan, math.inf, -0.1, 1.1, True])
def test_invalid_congestion_is_rejected(value: float) -> None:
    with pytest.raises(ValueError):
        IntegritySink().set_congestion(value)


def test_finite_large_latency_does_not_overflow_running_mean() -> None:
    sink = IntegritySink()
    for unit in gen_frames(3, capture_now=0.0):
        sink.process(unit, 1e308)
    assert sink.metrics()["mean_latency"] == 1e308
    assert math.isfinite(sink.metrics()["last_latency"])


def test_capture_integer_outside_float_range_is_unknown() -> None:
    sink = IntegritySink()
    unit = gen_frames(1)[0]
    out = sink.process(unit.with_payload(unit.payload, capture_now=10**1000), T0)
    assert "latency" not in integrity_events(out.events)[0].detail
    assert sink.metrics()["mean_latency"] is None
