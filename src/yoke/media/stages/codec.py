"""Pluggable line-based codec model and its encode/decode pipeline stages.

A :class:`Codec` turns one whole :class:`~yoke.media.types.MediaUnit`
frame into a list of line-based ``SLICE`` units and back, modelling the
compression *sweet spot* (doc/research/COMPRESSION.md): a
line-based, visually-lossless codec running at a target ratio in the
``[RATIO_MIN, RATIO_MAX]`` band — never the frame-based cliff.

Two software models ship here; a real codec (SVT-JPEG-XS, VC-2, DSC)
drops in behind the same protocol and is invoked out-of-process:

* :class:`PassthroughCodec` — bit-exact. Slices carry the real bytes;
  ``decode`` reassembles the exact original payload (ratio 1.0).
* :class:`RatioCodec` — models a visually-lossless codec at a target
  ratio. The *actual* payload bytes are preserved so ``decode`` can still
  reassemble and a sink can verify the embedded frame-id structurally;
  the on-wire *size* is modelled in ``meta["frame_bytes"]`` rather than by
  discarding bytes, which keeps the whole thing deterministic and sans-I/O.

Slice identity
--------------
Every slice of a frame shares the frame's ``seq`` and ``pts`` (they are
one frame's worth of data); a slice is identified *within* the frame by
``meta["slice_index"]`` (0-based), with ``meta["slice_count"]`` giving the
total. Downstream stages (packetize/fec) and any snooper depend on these
meta keys exactly:

===============  ==========================================================
``slice_index``  0-based position of this slice within its frame
``slice_count``  total number of slices the frame was split into
``frame_bytes``  modelled on-wire size of the *whole* frame (bytes); the
                 uncompressed size divided by ``ratio`` (int). Present and
                 identical on every slice of the frame.
``ratio``        the applied compression ratio (clamped to the band)
``lossless``     whether the path is bit-exact (True) or lossy-modelled
``incomplete``   set on a *decoded frame* when slices were missing; never
                 present on a slice
===============  ==========================================================

Missing slices: :meth:`Codec.decode` is tolerant — it reassembles a
best-effort frame from whatever slices are present, in ``slice_index``
order, and flags ``meta["incomplete"]=True`` when any are absent (for a
lossless frame that means the reconstruction is not bit-exact).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from yoke.media.pipeline import StageBase
from yoke.media.stages.ratecontrol import RateState
from yoke.media.types import (
    PT_JPEGXS,
    PT_RAW,
    RATIO_MAX,
    RATIO_MIN,
    RATIO_TRANSPARENT,
    EventKind,
    MediaUnit,
    StageEvent,
    StageOutcome,
    UnitKind,
)

DEFAULT_SLICES = 4
"""Slices per frame for the software model (a real codec derives this
from frame height / lines-per-slice)."""


@runtime_checkable
class Codec(Protocol):
    """A line-based codec: frame <-> line slices, plus its RTP identity."""

    lossless: bool

    def encode(self, frame: MediaUnit, target_ratio: float) -> list[MediaUnit]:
        """Split a whole FRAME into line-based SLICE units."""
        ...

    def decode(self, slices: list[MediaUnit]) -> MediaUnit:
        """Reassemble SLICE units back into a FRAME (best-effort)."""
        ...

    def payload_type(self) -> int:
        """The RTP payload type this codec's slices carry."""
        ...


def _clamp_ratio(ratio: float) -> float:
    """Keep the target inside the visually-lossless band."""
    return min(RATIO_MAX, max(RATIO_MIN, ratio))


def _slice_frame(
    frame: MediaUnit,
    n_slices: int,
    *,
    frame_bytes: int,
    ratio: float,
    lossless: bool,
) -> list[MediaUnit]:
    """Split ``frame.payload`` into ``n_slices`` contiguous SLICE units.

    The real bytes always ride along (see module docstring); the modelled
    on-wire size is carried in ``frame_bytes``. The split is exact — the
    remainder is spread across the leading slices — so a lossless decode
    reassembles the original payload byte-for-byte.
    """
    payload = frame.payload
    total = len(payload)
    base, rem = divmod(total, n_slices)
    slices: list[MediaUnit] = []
    offset = 0
    for index in range(n_slices):
        size = base + (1 if index < rem else 0)
        chunk = payload[offset : offset + size]
        offset += size
        slices.append(
            MediaUnit(
                kind=UnitKind.SLICE,
                seq=frame.seq,
                pts=frame.pts,
                payload=chunk,
                source_digest=frame.source_digest,
                meta={
                    **frame.meta,
                    "slice_index": index,
                    "slice_count": n_slices,
                    "frame_bytes": frame_bytes,
                    "ratio": ratio,
                    "lossless": lossless,
                },
            )
        )
    return slices


def _reassemble(slices: list[MediaUnit], *, lossless: bool) -> MediaUnit:
    """Concatenate slices in ``slice_index`` order into a FRAME.

    Missing slices are tolerated: whatever is present is joined in order
    and the frame is flagged ``incomplete``. A lossless frame reassembled
    from a full set is bit-exact (its digest matches ``source_digest``).
    """
    if not slices:
        raise ValueError("cannot reassemble a frame from zero slices")
    ordered = sorted(slices, key=lambda unit: int(unit.meta["slice_index"]))
    count = max(int(unit.meta["slice_count"]) for unit in ordered)
    present = {int(unit.meta["slice_index"]) for unit in ordered}
    incomplete = len(present) < count
    payload = b"".join(unit.payload for unit in ordered)
    head = ordered[0]
    ratio = head.meta.get("ratio", RATIO_MIN)
    frame_bytes = head.meta.get("frame_bytes", len(payload))
    return MediaUnit(
        kind=UnitKind.FRAME,
        seq=head.seq,
        pts=head.pts,
        payload=payload,
        source_digest=head.source_digest,
        meta={
            "slice_count": count,
            "frame_bytes": frame_bytes,
            "ratio": ratio,
            "lossless": lossless,
            "incomplete": incomplete,
        },
    )


class PassthroughCodec:
    """Bit-exact codec: slices carry real bytes, decode is byte-for-byte.

    ``target_ratio`` is ignored (a passthrough is always 1.0); the
    modelled on-wire size equals the uncompressed frame size.
    """

    lossless: bool = True

    def __init__(self, n_slices: int = DEFAULT_SLICES) -> None:
        if n_slices < 1:
            raise ValueError("n_slices must be >= 1")
        self._n_slices = n_slices

    def encode(self, frame: MediaUnit, target_ratio: float) -> list[MediaUnit]:
        return _slice_frame(
            frame,
            self._n_slices,
            frame_bytes=len(frame.payload),
            ratio=RATIO_MIN,
            lossless=True,
        )

    def decode(self, slices: list[MediaUnit]) -> MediaUnit:
        return _reassemble(slices, lossless=True)

    def payload_type(self) -> int:
        return PT_RAW


class RatioCodec:
    """Models a line-based visually-lossless codec at a target ratio.

    The payload bytes are preserved (so structural/frame-id checks pass);
    only the modelled on-wire size (``frame_bytes = uncompressed / ratio``)
    reflects the compression. The target ratio is clamped into the
    visually-lossless band and the applied value is recorded on every slice.
    """

    lossless: bool = False

    def __init__(self, n_slices: int = DEFAULT_SLICES) -> None:
        if n_slices < 1:
            raise ValueError("n_slices must be >= 1")
        self._n_slices = n_slices

    def encode(self, frame: MediaUnit, target_ratio: float) -> list[MediaUnit]:
        ratio = _clamp_ratio(target_ratio)
        frame_bytes = int(len(frame.payload) / ratio)
        return _slice_frame(
            frame,
            self._n_slices,
            frame_bytes=frame_bytes,
            ratio=ratio,
            lossless=False,
        )

    def decode(self, slices: list[MediaUnit]) -> MediaUnit:
        return _reassemble(slices, lossless=False)

    def payload_type(self) -> int:
        return PT_JPEGXS


class CodecEncodeStage(StageBase):
    """Wrap a :class:`Codec` as a pipeline stage: one FRAME -> N SLICEs.

    The target ratio is resolved, in order, from: a shared
    :class:`~yoke.media.stages.ratecontrol.RateState` (the closed loop —
    the rate controller writes it, this stage reads it), then the frame's
    own ``meta["target_ratio"]`` (stamped by an upstream controller), then
    the stage's static default. This keeps the encode stage decoupled from
    how the target is chosen while still driving the closed loop.
    """

    name = "codec"

    def __init__(
        self,
        codec: Codec,
        *,
        state: RateState | None = None,
        target_ratio: float = RATIO_TRANSPARENT,
    ) -> None:
        self._codec = codec
        self._state = state
        self._default = target_ratio

    def _target(self, frame: MediaUnit) -> float:
        if self._state is not None:
            return self._state.target
        meta_target = frame.meta.get("target_ratio")
        if isinstance(meta_target, (int, float)):
            return float(meta_target)
        return self._default

    def process(self, unit: MediaUnit, now: float) -> StageOutcome:
        target = self._target(unit)
        slices = self._codec.encode(unit, target)
        events = [
            StageEvent(
                stage=self.name,
                kind=EventKind.IN,
                seq=unit.seq,
                pts=unit.pts,
                now=now,
                detail={"bytes": len(unit.payload), "target_ratio": target},
            )
        ]
        for sl in slices:
            events.append(
                StageEvent(
                    stage=self.name,
                    kind=EventKind.OUT,
                    seq=sl.seq,
                    pts=sl.pts,
                    now=now,
                    detail={
                        "slice_index": sl.meta["slice_index"],
                        "slice_count": sl.meta["slice_count"],
                        "frame_bytes": sl.meta["frame_bytes"],
                        "ratio": sl.meta["ratio"],
                    },
                )
            )
        return StageOutcome(emit=slices, events=events)


class CodecDecodeStage(StageBase):
    """Wrap a :class:`Codec` as a pipeline stage: N SLICEs -> one FRAME.

    Slices are buffered per frame key ``(seq, pts)`` and may arrive in any
    order; the frame is decoded and emitted as soon as all of its slices
    are present. :meth:`flush` force-emits any partially-buffered frames
    (marked ``incomplete``) — useful when a completeness deadline passes.
    """

    name = "codec-decode"

    def __init__(self, codec: Codec) -> None:
        self._codec = codec
        self._buffers: dict[tuple[int, int], dict[int, MediaUnit]] = {}

    def process(self, unit: MediaUnit, now: float) -> StageOutcome:
        key = (unit.seq, unit.pts)
        index = int(unit.meta["slice_index"])
        count = int(unit.meta["slice_count"])
        slots = self._buffers.setdefault(key, {})
        slots[index] = unit
        events = [
            StageEvent(
                stage=self.name,
                kind=EventKind.IN,
                seq=unit.seq,
                pts=unit.pts,
                now=now,
                detail={"slice_index": index, "have": len(slots), "of": count},
            )
        ]
        if len(slots) < count:
            return StageOutcome(events=events)
        frame = self._codec.decode(list(slots.values()))
        del self._buffers[key]
        events.append(
            StageEvent(
                stage=self.name,
                kind=EventKind.OUT,
                seq=frame.seq,
                pts=frame.pts,
                now=now,
                detail={"bytes": len(frame.payload), "incomplete": False},
            )
        )
        return StageOutcome(emit=[frame], events=events)

    def flush(self, now: float) -> StageOutcome:
        """Emit every partially-buffered frame, flagged ``incomplete``."""
        out = StageOutcome()
        for key in sorted(self._buffers):
            slots = self._buffers[key]
            frame = self._codec.decode(list(slots.values()))
            out.emit.append(frame)
            out.events.append(
                StageEvent(
                    stage=self.name,
                    kind=EventKind.OUT,
                    seq=frame.seq,
                    pts=frame.pts,
                    now=now,
                    detail={
                        "bytes": len(frame.payload),
                        "incomplete": frame.meta["incomplete"],
                    },
                )
            )
        self._buffers.clear()
        return out
