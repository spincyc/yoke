"""Synthetic frame source: a deterministic stand-in for HDMI capture.

:class:`SyntheticSource` is the pipeline's head stage. It does NOT
self-drive (no clock reads, no timers): the shell or a test calls
:meth:`SyntheticSource.frame` with the current time and gets back exactly
one :class:`~yoke.media.types.MediaUnit` (kind ``FRAME``) plus an ``OUT``
tap. ``seq`` counts frames from 0; ``pts`` advances by ``clock_hz // fps``
ticks per frame (the 90 kHz RTP convention).

Payload layout (big-endian, so every lane parses it identically)::

    offset 0   u64  seq        frame sequence number (== unit.seq)
    offset 8   u64  pts        90 kHz presentation timestamp (== unit.pts)
    offset 16  u32  body_crc   zlib.crc32 of the body region
    offset 20  ...  body       MODEL_BODY_BYTES of pattern content

The header embeds the frame id + a checksum so a downstream sink verifies
glass-to-glass integrity: a bit-exact path re-derives ``digest_of(payload)``
and matches it against ``source_digest``; a lossy path confirms the
embedded ``seq`` survived.

The payload is a *small model* of a frame (a few hundred bytes), NOT a
real ~6 MB raster: ``meta["frame_bytes"]`` carries the modeled full size
(``width * height * 3``) so downstream bandwidth math is real while the
bytes actually moved stay tiny.

Patterns:

- ``counter``  pseudo-random body from ``Random(seed ^ seq)`` (the base
  verification pattern; incompressible-ish, transparent ratio hint).
- ``bars``     a static tiled body (identical every frame) → highly
  compressible; ``meta["compressible"] = True``, ratio hint ``RATIO_MAX``.
- ``noise``    fully random body with a distinct salt → incompressible;
  ``meta["compressible"] = False``, ratio hint ``RATIO_MIN``.
"""

from __future__ import annotations

import random
import struct
import zlib
from typing import Final

from yoke.media.pipeline import StageBase
from yoke.media.types import (
    MAX_FRAME_BYTES,
    MEDIA_CLOCK_HZ,
    RATIO_MAX,
    RATIO_MIN,
    RATIO_TRANSPARENT,
    Body,
    EventKind,
    MediaUnit,
    StageEvent,
    StageOutcome,
    UnitKind,
    digest_of,
    require_uint,
)

# Header layout (bytes). Public so the sink and other lanes parse the
# same offsets rather than re-deriving them.
SEQ_OFFSET: Final = 0
PTS_OFFSET: Final = 8
CRC_OFFSET: Final = 16
FRAME_HEADER_LEN: Final = 20
_HEADER: Final = struct.Struct("!QQI")
# Modeled body size: a small representative slice, NOT a real raster. The
# true frame size rides in meta["frame_bytes"] for bandwidth accounting.
MODEL_BODY_BYTES: Final = 512
# Bytes per pixel used to model an uncompressed frame (RGB / RFC 4175-ish).
_BYTES_PER_PIXEL: Final = 3
# Distinct RNG salt so "noise" bodies differ from "counter" at equal seq.
_NOISE_SALT: Final = 0x9E3779B9
_BAR_UNIT_LEN: Final = 16

# pattern -> (compressible hint, ratio hint) for the rate controller.
_PATTERN_HINTS: Final[dict[str, tuple[bool, float]]] = {
    "counter": (True, RATIO_TRANSPARENT),
    "bars": (True, RATIO_MAX),
    "noise": (False, RATIO_MIN),
}


def parse_header(payload: bytes) -> tuple[int, int, int] | None:
    """Return ``(seq, pts, body_crc)`` from a source payload, or ``None``
    if it is too short to carry the header. Single source of the layout
    so every stage decodes it the same way."""
    if len(payload) < FRAME_HEADER_LEN:
        return None
    seq, pts, crc = _HEADER.unpack_from(payload)
    return int(seq), int(pts), int(crc)


class SyntheticSource(StageBase):
    """Deterministic frame generator standing in for HDMI capture."""

    name = "source"

    def __init__(
        self,
        *,
        width: int,
        height: int,
        fps: int,
        pattern: str = "counter",
        seed: int = 0,
        clock_hz: int = MEDIA_CLOCK_HZ,
    ) -> None:
        for name, value in (("width", width), ("height", height)):
            if require_uint(value, name, MAX_FRAME_BYTES) == 0:
                raise ValueError(f"{name} must be positive")
        if width * height * _BYTES_PER_PIXEL > MAX_FRAME_BYTES:
            raise ValueError("modeled frame exceeds byte budget")
        if require_uint(clock_hz, "clock_hz", 0xFFFF_FFFF_FFFF_FFFF) == 0:
            raise ValueError("clock_hz must be positive")
        if require_uint(fps, "fps", clock_hz) == 0:
            raise ValueError("fps must be positive and no greater than clock_hz")
        require_uint(seed, "seed", 0xFFFF_FFFF_FFFF_FFFF)
        if pattern not in _PATTERN_HINTS:
            raise ValueError(f"unknown pattern {pattern!r}")
        self._width = width
        self._height = height
        self._fps = fps
        self._pattern = pattern
        self._seed = seed
        self._clock_hz = clock_hz
        self._pts_per_frame = clock_hz // fps
        self._frame_bytes = width * height * _BYTES_PER_PIXEL
        self._seq = 0
        # A fixed, highly compressible unit tiled to make "bars" static.
        self._bar_unit = random.Random(seed).randbytes(_BAR_UNIT_LEN)
        self._bar_body = (self._bar_unit * (MODEL_BODY_BYTES // _BAR_UNIT_LEN + 1))[
            :MODEL_BODY_BYTES
        ]
        self._bar_crc = zlib.crc32(self._bar_body) & 0xFFFFFFFF
        self._rng = random.Random(seed)

    def describe(self) -> Body:
        """Static parameters of this source (for probes / topology dumps)."""
        return {
            "width": self._width,
            "height": self._height,
            "fps": self._fps,
            "pattern": self._pattern,
            "clock_hz": self._clock_hz,
        }

    def frame(self, now: float) -> StageOutcome:
        """Produce exactly one frame stamped for ``now``.

        A producer method (NOT :meth:`process`): the source is a head
        stage the shell drives, so time enters here rather than through a
        pipeline push.
        """
        seq = self._seq
        pts = seq * self._pts_per_frame
        compressible, ratio_hint = _PATTERN_HINTS[self._pattern]
        payload = self._build_payload(seq, pts)
        meta: Body = {
            "frame_bytes": self._frame_bytes,
            "pattern": self._pattern,
            "capture_now": now,
            "lossless": True,
            "compressible": compressible,
            "ratio_hint": ratio_hint,
        }
        unit = MediaUnit(
            kind=UnitKind.FRAME,
            seq=seq,
            pts=pts,
            payload=payload,
            source_digest=digest_of(payload),
            meta=meta,
        )
        event = StageEvent(
            stage=self.name,
            kind=EventKind.OUT,
            seq=seq,
            pts=pts,
            now=now,
            detail={"frame_bytes": self._frame_bytes, "pattern": self._pattern},
        )
        self._seq += 1
        return StageOutcome(emit=[unit], events=[event])

    def process(self, unit: MediaUnit, now: float) -> StageOutcome:
        """The source is a head stage; it is driven via :meth:`frame`, so
        being pushed a unit is a wiring error."""
        raise RuntimeError("SyntheticSource is a head stage; call frame(now)")

    # -- internals -----------------------------------------------------------

    def _build_payload(self, seq: int, pts: int) -> bytes:
        body = self._build_body(seq)
        crc = (
            self._bar_crc if self._pattern == "bars" else zlib.crc32(body) & 0xFFFFFFFF
        )
        header = _HEADER.pack(seq, pts, crc)
        return header + body

    def _build_body(self, seq: int) -> bytes:
        n = MODEL_BODY_BYTES
        if self._pattern == "bars":
            return self._bar_body
        salt = _NOISE_SALT if self._pattern == "noise" else 0
        # Reseeding a private generator reproduces Random(seed ^ seq ^ salt)
        # exactly while avoiding a new generator allocation for every frame.
        self._rng.seed(self._seed ^ seq ^ salt)
        return self._rng.randbytes(n)
