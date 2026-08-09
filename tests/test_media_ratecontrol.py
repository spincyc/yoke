"""Tests for the closed-loop rate controller.

Deterministic and sans-I/O: reports are built directly and fed with an
explicit ``now``. Verifies the sliding law, EMA smoothing, band clamping,
ADJUST taps, and the controller+codec closed loop.
"""

from __future__ import annotations

from itertools import pairwise

from yoke.media.stages.codec import CodecEncodeStage, RatioCodec
from yoke.media.stages.ratecontrol import RateController, RateState
from yoke.media.types import (
    RATIO_MAX,
    RATIO_MIN,
    RATIO_TRANSPARENT,
    EventKind,
    MediaUnit,
    NetworkReport,
    UnitKind,
    digest_of,
)

NOW = 500.0


def congested(now: float = NOW) -> NetworkReport:
    return NetworkReport(now=now, loss=0.2, jitter=0.05, congestion=1.0)


def quiet(now: float = NOW) -> NetworkReport:
    return NetworkReport(now=now, loss=0.0, jitter=0.0, congestion=0.0)


def make_frame(payload: bytes) -> MediaUnit:
    return MediaUnit(
        kind=UnitKind.FRAME,
        seq=1,
        pts=90,
        payload=payload,
        source_digest=digest_of(payload),
    )


def test_starts_at_transparent() -> None:
    ctrl = RateController()
    assert ctrl.target_ratio() == RATIO_TRANSPARENT


def test_slides_up_under_sustained_congestion() -> None:
    ctrl = RateController()
    targets = [ctrl.target_ratio()]
    for _ in range(10):
        ctrl.on_feedback(congested(), NOW)
        targets.append(ctrl.target_ratio())
    # Monotonically increasing and converging toward the top of the band.
    assert all(b >= a for a, b in pairwise(targets))
    assert targets[-1] > targets[0]
    assert targets[-1] > 5.5
    assert targets[-1] <= RATIO_MAX


def test_slides_down_when_quiet() -> None:
    ctrl = RateController()
    # First drive it up.
    for _ in range(10):
        ctrl.on_feedback(congested(), NOW)
    high = ctrl.target_ratio()
    assert high > 5.0

    targets = [high]
    for _ in range(10):
        ctrl.on_feedback(quiet(), NOW)
        targets.append(ctrl.target_ratio())
    # Monotonically decreasing back toward the transparent resting point.
    assert all(b <= a for a, b in pairwise(targets))
    assert targets[-1] < 3.0
    # Never relaxes below the transparent floor (compression funds FEC).
    assert targets[-1] >= RATIO_TRANSPARENT - 1e-9


def test_ema_smooths_single_report() -> None:
    ctrl = RateController()
    ctrl.on_feedback(congested(), NOW)
    # One full-pressure report must not slam the dial to the ceiling.
    assert RATIO_TRANSPARENT < ctrl.target_ratio() < RATIO_MAX


def test_clamps_to_band() -> None:
    ctrl = RateController()
    extreme = NetworkReport(now=NOW, loss=9.0, jitter=9.0, congestion=9.0)
    for _ in range(50):
        ctrl.on_feedback(extreme, NOW)
    assert ctrl.target_ratio() <= RATIO_MAX

    for _ in range(50):
        ctrl.on_feedback(quiet(), NOW)
    assert ctrl.target_ratio() >= RATIO_MIN


def test_emits_adjust_on_material_change() -> None:
    ctrl = RateController()
    outcome = ctrl.on_feedback(congested(), NOW)
    adjusts = [e for e in outcome.events if e.kind is EventKind.ADJUST]
    assert len(adjusts) == 1
    assert adjusts[0].stage == "ratecontrol"
    assert adjusts[0].detail["target"] > adjusts[0].detail["prev"]


def test_no_adjust_when_already_at_rest() -> None:
    ctrl = RateController()
    outcome = ctrl.on_feedback(quiet(), NOW)
    assert [e for e in outcome.events if e.kind is EventKind.ADJUST] == []
    assert ctrl.target_ratio() == RATIO_TRANSPARENT


def test_writes_shared_rate_state() -> None:
    state = RateState()
    ctrl = RateController(state)
    assert state.target == RATIO_TRANSPARENT
    for _ in range(5):
        ctrl.on_feedback(congested(), NOW)
    assert state.target == ctrl.target_ratio()
    assert state.target > RATIO_TRANSPARENT


def test_process_stamps_target_ratio() -> None:
    ctrl = RateController()
    frame = make_frame(b"pixels")
    outcome = ctrl.process(frame, NOW)
    assert len(outcome.emit) == 1
    stamped = outcome.emit[0]
    assert stamped.payload == frame.payload
    assert stamped.meta["target_ratio"] == RATIO_TRANSPARENT


def test_controller_codec_closed_loop() -> None:
    state = RateState()
    ctrl = RateController(state)
    encode = CodecEncodeStage(RatioCodec(), state=state)
    frame = make_frame(b"x" * 600)

    before = encode.process(frame, NOW).emit
    fb_before = before[0].meta["frame_bytes"]
    assert before[0].meta["ratio"] == RATIO_TRANSPARENT

    # Sustained congestion raises the target -> more compression.
    for _ in range(8):
        ctrl.on_feedback(congested(), NOW)

    after = encode.process(frame, NOW).emit
    fb_after = after[0].meta["frame_bytes"]
    assert after[0].meta["ratio"] > RATIO_TRANSPARENT
    assert fb_after < fb_before  # smaller modelled on-wire size
