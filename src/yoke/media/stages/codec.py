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
  the compressed raster *size* is modelled in ``meta["frame_bytes"]`` rather than by
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
``frame_bytes``  modelled size of the *whole* frame (bytes); the source's
                 raster size (or payload size) divided by ``ratio`` (int). Present and
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

import math
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from yoke.media.pipeline import StageBase
from yoke.media.stages.ratecontrol import RateState
from yoke.media.types import (
    MAX_BUFFER_BYTES,
    MAX_FRAME_BYTES,
    MAX_PENDING_FRAMES,
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
    is_digest,
    require_uint,
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
    if type(ratio) not in (int, float) or (
        isinstance(ratio, float) and not math.isfinite(ratio)
    ):
        raise ValueError("target ratio must be finite")
    return min(RATIO_MAX, max(RATIO_MIN, ratio))


def _frame_bytes(frame: MediaUnit) -> int:
    """Account modeled rasters separately from the small verification payload."""
    if frame.kind is not UnitKind.FRAME:
        raise ValueError("codec input must be a frame")
    if len(frame.payload) > MAX_FRAME_BYTES:
        raise ValueError("frame exceeds byte budget")
    value = frame.meta.get("frame_bytes", len(frame.payload))
    return require_uint(value, "frame_bytes", MAX_FRAME_BYTES)


def _slice_identity(unit: MediaUnit) -> tuple[int, int]:
    if unit.kind is not UnitKind.SLICE:
        raise ValueError("codec input must be a slice")
    require_uint(unit.seq, "seq", 0xFFFF_FFFF_FFFF_FFFF)
    require_uint(unit.pts, "pts", 0xFFFF_FFFF_FFFF_FFFF)
    index = require_uint(unit.meta.get("slice_index"), "slice_index", 0xFFFF)
    count = require_uint(unit.meta.get("slice_count"), "slice_count", 0xFFFF)
    if index >= count:
        raise ValueError("slice index/count out of range")
    if not is_digest(unit.source_digest):
        raise ValueError("source_digest is not a SHA-256 digest")
    if "frame_bytes" in unit.meta:
        require_uint(unit.meta["frame_bytes"], "frame_bytes", MAX_FRAME_BYTES)
    if "ratio" in unit.meta:
        ratio = unit.meta["ratio"]
        if _clamp_ratio(ratio) != ratio:
            raise ValueError("slice ratio outside band")
    if "lossless" in unit.meta and type(unit.meta["lossless"]) is not bool:
        raise ValueError("lossless must be boolean")
    return index, count


def _signature(unit: MediaUnit) -> tuple[object, ...]:
    """Fields which must agree across all slices in one frame."""
    return (
        unit.seq,
        unit.pts,
        unit.source_digest,
        unit.meta.get("slice_count"),
        unit.meta.get("frame_bytes"),
        unit.meta.get("ratio"),
        unit.meta.get("lossless"),
        unit.meta.get("capture_now"),
    )


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
    slots: dict[int, MediaUnit] = {}
    signature = _signature(slices[0])
    for unit in slices:
        index, count = _slice_identity(unit)
        if _signature(unit) != signature:
            raise ValueError("conflicting frame metadata")
        if index in slots and slots[index] != unit:
            raise ValueError("conflicting duplicate slice")
        slots[index] = unit
    ordered = [slots[index] for index in sorted(slots)]
    count = int(ordered[0].meta["slice_count"])
    incomplete = len(slots) < count
    if sum(len(unit.payload) for unit in ordered) > MAX_FRAME_BYTES:
        raise ValueError("frame exceeds byte budget")
    payload = b"".join(unit.payload for unit in ordered)
    head = ordered[0]
    ratio = head.meta.get("ratio", RATIO_MIN)
    frame_bytes = head.meta.get("frame_bytes", len(payload))
    meta = {
        "slice_count": count,
        "frame_bytes": frame_bytes,
        "ratio": ratio,
        "lossless": lossless,
        "incomplete": incomplete,
    }
    if "capture_now" in head.meta:
        meta["capture_now"] = head.meta["capture_now"]
    return MediaUnit(
        kind=UnitKind.FRAME,
        seq=head.seq,
        pts=head.pts,
        payload=payload,
        source_digest=head.source_digest,
        meta=meta,
    )


class PassthroughCodec:
    """Bit-exact codec: slices carry real bytes, decode is byte-for-byte.

    ``target_ratio`` is ignored (a passthrough is always 1.0); the
    modelled on-wire size equals the uncompressed frame size.
    """

    lossless: bool = True

    def __init__(self, n_slices: int = DEFAULT_SLICES) -> None:
        if require_uint(n_slices, "n_slices", 0xFFFF) < 1:
            raise ValueError("n_slices must be >= 1")
        self._n_slices = n_slices

    def encode(self, frame: MediaUnit, target_ratio: float) -> list[MediaUnit]:
        return _slice_frame(
            frame,
            self._n_slices,
            frame_bytes=_frame_bytes(frame),
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
        if require_uint(n_slices, "n_slices", 0xFFFF) < 1:
            raise ValueError("n_slices must be >= 1")
        self._n_slices = n_slices

    def encode(self, frame: MediaUnit, target_ratio: float) -> list[MediaUnit]:
        ratio = _clamp_ratio(target_ratio)
        frame_bytes = int(_frame_bytes(frame) / ratio)
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


@dataclass
class _FrameBuffer:
    signature: tuple[object, ...]
    count: int
    slices: dict[int, MediaUnit] = field(default_factory=dict)
    bytes: int = 0


class CodecDecodeStage(StageBase):
    """Bounded, consistent assembly of slices, with a recent replay window.

    Frames emit when every distinct slice is present. Capacity evicts the
    oldest partial frame; malformed/conflicting slices cannot overwrite
    accepted bytes. ``flush`` explicitly emits best-effort incomplete frames.
    """

    name = "codec-decode"

    def __init__(
        self,
        codec: Codec,
        *,
        max_frames: int = MAX_PENDING_FRAMES,
        max_bytes: int = MAX_BUFFER_BYTES,
        max_frame_bytes: int = MAX_FRAME_BYTES,
        done_window: int = 1024,
    ) -> None:
        for name, value in (
            ("max_frames", max_frames),
            ("max_bytes", max_bytes),
            ("max_frame_bytes", max_frame_bytes),
            ("done_window", done_window),
        ):
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        self._codec = codec
        self._max_frames = max_frames
        self._max_bytes = max_bytes
        self._max_frame_bytes = max_frame_bytes
        self._done_window = done_window
        self._buffers: OrderedDict[tuple[int, int], _FrameBuffer] = OrderedDict()
        self._done: OrderedDict[tuple[int, int], None] = OrderedDict()
        self._bytes = 0

    def process(self, unit: MediaUnit, now: float) -> StageOutcome:
        out = StageOutcome()
        try:
            index, count = _slice_identity(unit)
        except (TypeError, ValueError) as exc:
            self._drop(out, unit.seq, unit.pts, now, f"bad-slice: {exc}")
            return out
        key = (unit.seq, unit.pts)
        if key in self._done:
            self._drop(out, *key, now, "frame-already-emitted")
            return out
        size = len(unit.payload)
        if size > min(self._max_frame_bytes, self._max_bytes):
            self._drop(out, *key, now, "frame-byte-budget")
            return out
        frame = self._buffers.get(key)
        if frame is None:
            while len(self._buffers) >= self._max_frames:
                self._retire(next(iter(self._buffers)), now, out, "frame-capacity")
            frame = _FrameBuffer(_signature(unit), count)
            self._buffers[key] = frame
        elif frame.signature != _signature(unit):
            self._retire(key, now, out, "conflicting-frame-meta")
            return out
        previous = frame.slices.get(index)
        if previous is not None:
            if previous != unit:
                self._retire(key, now, out, "conflicting-slice")
            else:
                self._drop(out, *key, now, "duplicate-slice")
            return out
        if frame.bytes + size > self._max_frame_bytes:
            self._retire(key, now, out, "frame-byte-budget")
            return out
        while self._bytes + size > self._max_bytes:
            victim = next(iter(self._buffers))
            self._retire(victim, now, out, "buffer-byte-budget")
            if victim == key:
                return out
        frame.slices[index] = unit
        frame.bytes += size
        self._bytes += size
        out.events.append(
            StageEvent(
                stage=self.name,
                kind=EventKind.IN,
                seq=unit.seq,
                pts=unit.pts,
                now=now,
                detail={"slice_index": index, "have": len(frame.slices), "of": count},
            )
        )
        if len(frame.slices) == count:
            decoded = self._codec.decode(list(frame.slices.values()))
            self._remove(key)
            out.emit.append(decoded)
            out.events.append(self._out_event(decoded, now))
        return out

    def flush(self, now: float) -> StageOutcome:
        """Emit partial frames in identity order, flagged ``incomplete``."""
        out = StageOutcome()
        for key in sorted(self._buffers):
            frame = self._codec.decode(list(self._buffers[key].slices.values()))
            out.emit.append(frame)
            out.events.append(self._out_event(frame, now))
            self._remove(key)
        return out

    def _remove(self, key: tuple[int, int]) -> None:
        self._bytes -= self._buffers.pop(key).bytes
        self._done[key] = None
        while len(self._done) > self._done_window:
            self._done.popitem(last=False)

    def _retire(
        self, key: tuple[int, int], now: float, out: StageOutcome, why: str
    ) -> None:
        self._remove(key)
        self._drop(out, *key, now, why)

    def _drop(
        self, out: StageOutcome, seq: int, pts: int, now: float, why: str
    ) -> None:
        out.events.append(
            StageEvent(
                stage=self.name,
                kind=EventKind.DROP,
                seq=seq,
                pts=pts,
                now=now,
                detail={"why": why},
            )
        )

    def _out_event(self, frame: MediaUnit, now: float) -> StageEvent:
        return StageEvent(
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
