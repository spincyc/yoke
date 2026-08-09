"""Tests for the line-based codec model and its pipeline stages.

Deterministic and sans-I/O: frames are built directly and threaded
through the stages with an explicit ``now``. No sockets, no wall clock.
"""

from __future__ import annotations

import random

from yoke.media.stages.codec import (
    CodecDecodeStage,
    CodecEncodeStage,
    PassthroughCodec,
    RatioCodec,
)
from yoke.media.stages.ratecontrol import RateState
from yoke.media.types import (
    PT_JPEGXS,
    PT_RAW,
    RATIO_MAX,
    RATIO_MIN,
    EventKind,
    MediaUnit,
    UnitKind,
    digest_of,
)

NOW = 1_000.0


def make_frame(payload: bytes, *, seq: int = 7, pts: int = 900) -> MediaUnit:
    """A whole-frame MediaUnit stamped with its own content digest."""
    return MediaUnit(
        kind=UnitKind.FRAME,
        seq=seq,
        pts=pts,
        payload=payload,
        source_digest=digest_of(payload),
    )


def frame_payload(seed: int, size: int) -> bytes:
    rng = random.Random(seed)
    return bytes(rng.randrange(256) for _ in range(size))


# -- passthrough: bit-exact round-trip --------------------------------------


def test_passthrough_round_trips_bit_exact() -> None:
    codec = PassthroughCodec()
    payload = frame_payload(1, 251)  # not divisible by 4
    frame = make_frame(payload)

    slices = codec.encode(frame, target_ratio=3.0)
    assert len(slices) == 4
    assert all(s.kind is UnitKind.SLICE for s in slices)
    assert all(s.meta["lossless"] is True for s in slices)
    # Passthrough models no compression: ratio 1.0, frame_bytes == input.
    assert all(s.meta["ratio"] == RATIO_MIN for s in slices)
    assert all(s.meta["frame_bytes"] == len(payload) for s in slices)

    frame2 = codec.decode(slices)
    assert frame2.kind is UnitKind.FRAME
    assert frame2.payload == payload  # bit-exact
    assert frame2.meta["lossless"] is True
    assert frame2.meta["incomplete"] is False
    assert digest_of(frame2.payload) == frame.source_digest
    assert frame2.source_digest == frame.source_digest


def test_passthrough_slices_share_seq_and_pts() -> None:
    codec = PassthroughCodec()
    frame = make_frame(frame_payload(2, 40), seq=11, pts=1234)
    slices = codec.encode(frame, target_ratio=1.0)
    assert all(s.seq == 11 and s.pts == 1234 for s in slices)
    assert [s.meta["slice_index"] for s in slices] == [0, 1, 2, 3]
    assert all(s.meta["slice_count"] == 4 for s in slices)


def test_passthrough_payload_type_is_raw() -> None:
    assert PassthroughCodec().payload_type() == PT_RAW
    assert PassthroughCodec().lossless is True


# -- ratio codec: models frame_bytes, clamps, preserves bytes ---------------


def test_ratio_codec_models_frame_bytes() -> None:
    codec = RatioCodec()
    payload = frame_payload(3, 400)
    frame = make_frame(payload)

    slices = codec.encode(frame, target_ratio=4.0)
    assert all(s.meta["ratio"] == 4.0 for s in slices)
    assert all(s.meta["frame_bytes"] == 100 for s in slices)  # 400 / 4
    assert all(s.meta["lossless"] is False for s in slices)
    # Actual bytes are preserved so structure survives.
    reassembled = b"".join(s.payload for s in slices)
    assert reassembled == payload

    frame2 = codec.decode(slices)
    assert frame2.payload == payload
    assert frame2.meta["lossless"] is False
    assert frame2.meta["incomplete"] is False


def test_ratio_codec_clamps_target_to_band() -> None:
    codec = RatioCodec()
    frame = make_frame(frame_payload(4, 120))

    high = codec.encode(frame, target_ratio=999.0)
    assert all(s.meta["ratio"] == RATIO_MAX for s in high)

    low = codec.encode(frame, target_ratio=0.1)
    assert all(s.meta["ratio"] == RATIO_MIN for s in low)


def test_ratio_codec_payload_type_is_jpegxs() -> None:
    assert RatioCodec().payload_type() == PT_JPEGXS
    assert RatioCodec().lossless is False


# -- reassembly ordering & missing slices -----------------------------------


def test_slices_reassemble_in_arbitrary_order() -> None:
    codec = PassthroughCodec()
    payload = frame_payload(5, 199)
    frame = make_frame(payload)
    slices = codec.encode(frame, target_ratio=1.0)

    shuffled = list(slices)
    random.Random(99).shuffle(shuffled)
    assert [s.meta["slice_index"] for s in shuffled] != [0, 1, 2, 3]

    frame2 = codec.decode(shuffled)
    assert frame2.payload == payload
    assert frame2.meta["incomplete"] is False


def test_missing_slice_flags_incomplete() -> None:
    codec = PassthroughCodec()
    payload = frame_payload(6, 200)
    frame = make_frame(payload)
    slices = codec.encode(frame, target_ratio=1.0)

    # Drop slice index 1.
    partial = [s for s in slices if s.meta["slice_index"] != 1]
    frame2 = codec.decode(partial)
    assert frame2.meta["incomplete"] is True
    # Best-effort: present slices joined in order, but not bit-exact.
    assert frame2.payload != payload
    assert digest_of(frame2.payload) != frame.source_digest


# -- encode / decode stages -------------------------------------------------


def test_encode_stage_emits_events_and_slices() -> None:
    stage = CodecEncodeStage(RatioCodec(), target_ratio=2.5)
    frame = make_frame(frame_payload(7, 160))

    outcome = stage.process(frame, NOW)
    assert len(outcome.emit) == 4
    kinds = [e.kind for e in outcome.events]
    assert kinds.count(EventKind.IN) == 1
    assert kinds.count(EventKind.OUT) == 4
    in_event = next(e for e in outcome.events if e.kind is EventKind.IN)
    assert in_event.detail["target_ratio"] == 2.5


def test_decode_stage_buffers_until_complete_any_order() -> None:
    encode = CodecEncodeStage(PassthroughCodec(), target_ratio=1.0)
    decode = CodecDecodeStage(PassthroughCodec())
    payload = frame_payload(8, 321)
    frame = make_frame(payload)

    slices = encode.process(frame, NOW).emit
    order = list(slices)
    random.Random(3).shuffle(order)

    emitted: list[MediaUnit] = []
    for i, sl in enumerate(order):
        outcome = decode.process(sl, NOW)
        emitted.extend(outcome.emit)
        # Frame only appears once the last slice lands.
        if i < len(order) - 1:
            assert outcome.emit == []
    assert len(emitted) == 1
    assert emitted[0].payload == payload
    assert emitted[0].meta["incomplete"] is False


def test_decode_stage_flush_emits_incomplete() -> None:
    encode = CodecEncodeStage(PassthroughCodec(), target_ratio=1.0)
    decode = CodecDecodeStage(PassthroughCodec())
    frame = make_frame(frame_payload(9, 88))
    slices = encode.process(frame, NOW).emit

    # Feed all but the last slice, then flush.
    for sl in slices[:-1]:
        assert decode.process(sl, NOW).emit == []
    flushed = decode.flush(NOW)
    assert len(flushed.emit) == 1
    assert flushed.emit[0].meta["incomplete"] is True


def test_encode_stage_reads_shared_rate_state() -> None:
    state = RateState(target=RATIO_MIN)
    stage = CodecEncodeStage(RatioCodec(), state=state)
    frame = make_frame(frame_payload(10, 240))

    at_min = stage.process(frame, NOW).emit
    assert all(s.meta["ratio"] == RATIO_MIN for s in at_min)

    state.target = 4.0  # controller slides the dial up
    at_four = stage.process(frame, NOW).emit
    assert all(s.meta["ratio"] == 4.0 for s in at_four)
    assert at_four[0].meta["frame_bytes"] == 60  # 240 / 4


def test_encode_stage_reads_meta_target_when_no_state() -> None:
    stage = CodecEncodeStage(RatioCodec(), target_ratio=2.0)
    frame = make_frame(frame_payload(11, 100)).with_payload(
        frame_payload(11, 100), target_ratio=5.0
    )
    slices = stage.process(frame, NOW).emit
    assert all(s.meta["ratio"] == 5.0 for s in slices)
