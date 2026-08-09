"""Unit tests for the synthetic frame source (deterministic, sans-I/O).

No sockets, no wall clock: the source is driven by calling ``frame(now)``
with a fabricated time. Everything is reproducible from the seed.
"""

from __future__ import annotations

import zlib

import pytest

from yoke.media.stages.source import (
    FRAME_HEADER_LEN,
    MODEL_BODY_BYTES,
    SyntheticSource,
    parse_header,
)
from yoke.media.types import (
    MEDIA_CLOCK_HZ,
    RATIO_MAX,
    RATIO_MIN,
    RATIO_TRANSPARENT,
    EventKind,
    UnitKind,
    digest_of,
)

T0 = 1_754_000_000.0


def make_source(
    *,
    width: int = 1920,
    height: int = 1080,
    fps: int = 60,
    pattern: str = "counter",
    seed: int = 0,
    clock_hz: int = MEDIA_CLOCK_HZ,
) -> SyntheticSource:
    return SyntheticSource(
        width=width,
        height=height,
        fps=fps,
        pattern=pattern,
        seed=seed,
        clock_hz=clock_hz,
    )


# -- sequencing --------------------------------------------------------------


def test_seq_and_pts_are_monotonic() -> None:
    src = make_source(fps=60)
    step = MEDIA_CLOCK_HZ // 60
    for n in range(5):
        out = src.frame(T0 + n)
        assert len(out.emit) == 1
        unit = out.emit[0]
        assert unit.kind is UnitKind.FRAME
        assert unit.seq == n
        assert unit.pts == n * step


def test_pts_step_honors_clock_and_fps() -> None:
    src = make_source(fps=30, clock_hz=90_000)
    u0 = src.frame(T0).emit[0]
    u1 = src.frame(T0).emit[0]
    assert u1.pts - u0.pts == 90_000 // 30 == 3000


def test_out_event_shape() -> None:
    src = make_source()
    out = src.frame(T0)
    assert len(out.events) == 1
    ev = out.events[0]
    assert ev.stage == "source"
    assert ev.kind is EventKind.OUT
    assert ev.seq == 0
    assert ev.pts == 0
    assert ev.now == T0
    assert ev.detail["frame_bytes"] == 1920 * 1080 * 3


# -- payload layout / integrity ---------------------------------------------


def test_embedded_seq_pts_and_crc_round_trip() -> None:
    src = make_source(fps=60)
    for _ in range(3):
        unit = src.frame(T0).emit[0]
        parsed = parse_header(unit.payload)
        assert parsed is not None
        seq, pts, crc = parsed
        assert seq == unit.seq
        assert pts == unit.pts
        body = unit.payload[FRAME_HEADER_LEN:]
        assert len(body) == MODEL_BODY_BYTES
        assert crc == zlib.crc32(body) & 0xFFFFFFFF


def test_source_digest_matches_payload() -> None:
    unit = make_source().frame(T0).emit[0]
    assert unit.source_digest == digest_of(unit.payload)


def test_frame_bytes_models_full_raster_not_payload() -> None:
    unit = make_source(width=1280, height=720).frame(T0).emit[0]
    assert unit.meta["frame_bytes"] == 1280 * 720 * 3
    # the payload actually moved is tiny, not the modeled raster
    assert len(unit.payload) == FRAME_HEADER_LEN + MODEL_BODY_BYTES
    assert len(unit.payload) < unit.meta["frame_bytes"]


def test_capture_now_and_lossless_in_meta() -> None:
    unit = make_source().frame(T0 + 5.0).emit[0]
    assert unit.meta["capture_now"] == T0 + 5.0
    assert unit.meta["lossless"] is True


# -- determinism -------------------------------------------------------------


def test_deterministic_for_same_seed() -> None:
    a = make_source(seed=7)
    b = make_source(seed=7)
    for _ in range(4):
        ua = a.frame(T0).emit[0]
        ub = b.frame(T0).emit[0]
        assert ua.payload == ub.payload
        assert ua.source_digest == ub.source_digest


def test_different_seed_changes_payload() -> None:
    a = make_source(seed=1).frame(T0).emit[0]
    b = make_source(seed=2).frame(T0).emit[0]
    assert a.payload != b.payload


# -- patterns ----------------------------------------------------------------


def test_patterns_produce_distinct_payloads() -> None:
    counter = make_source(pattern="counter").frame(T0).emit[0]
    bars = make_source(pattern="bars").frame(T0).emit[0]
    noise = make_source(pattern="noise").frame(T0).emit[0]
    payloads = {counter.payload, bars.payload, noise.payload}
    assert len(payloads) == 3


def test_pattern_hints_differ() -> None:
    counter = make_source(pattern="counter").frame(T0).emit[0]
    bars = make_source(pattern="bars").frame(T0).emit[0]
    noise = make_source(pattern="noise").frame(T0).emit[0]
    assert counter.meta["ratio_hint"] == RATIO_TRANSPARENT
    assert bars.meta["compressible"] is True
    assert bars.meta["ratio_hint"] == RATIO_MAX
    assert noise.meta["compressible"] is False
    assert noise.meta["ratio_hint"] == RATIO_MIN


def test_bars_body_is_static_across_frames() -> None:
    src = make_source(pattern="bars")
    f0 = src.frame(T0).emit[0]
    f1 = src.frame(T0).emit[0]
    body0 = f0.payload[FRAME_HEADER_LEN:]
    body1 = f1.payload[FRAME_HEADER_LEN:]
    assert body0 == body1  # static content -> high compressibility
    # ... but the whole payloads differ because the header carries the seq
    assert f0.payload != f1.payload


def test_counter_body_varies_per_frame() -> None:
    src = make_source(pattern="counter")
    body0 = src.frame(T0).emit[0].payload[FRAME_HEADER_LEN:]
    body1 = src.frame(T0).emit[0].payload[FRAME_HEADER_LEN:]
    assert body0 != body1


# -- describe / guards -------------------------------------------------------


def test_describe_reports_parameters() -> None:
    src = make_source(width=800, height=600, fps=24, pattern="bars", clock_hz=48_000)
    assert src.describe() == {
        "width": 800,
        "height": 600,
        "fps": 24,
        "pattern": "bars",
        "clock_hz": 48_000,
    }


def test_process_raises_head_stage() -> None:
    src = make_source()
    unit = src.frame(T0).emit[0]
    with pytest.raises(RuntimeError):
        src.process(unit, T0)


def test_invalid_pattern_and_fps_rejected() -> None:
    with pytest.raises(ValueError):
        make_source(pattern="spirograph")
    with pytest.raises(ValueError):
        make_source(fps=0)


def test_parse_header_rejects_short_payload() -> None:
    assert parse_header(b"\x00\x01\x02") is None
