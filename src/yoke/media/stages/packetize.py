"""RTP packetization / depacketization (RFC 3550), sans-I/O and byte-exact.

The packetizer turns codec ``SLICE`` units into ``PACKET`` units carrying a
12-byte RTP header + a payload fragment, each ``<= mtu``. The depacketizer
buffers packets per frame, reassembles fragments in order, and reconstructs
the original ``SLICE`` units byte-for-byte once a frame is complete. Both are
pure transforms: no sockets, no clock (``now`` is supplied by the shell/tests).

RTP header (RFC 3550 sec. 5.1), 12 bytes, no CSRCs / extension / padding::

     0                   1                   2                   3
     0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
    +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
    |V=2|P|X|  CC   |M|     PT      |       sequence number         |
    +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
    |                           timestamp                           |
    +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
    |           synchronization source (SSRC) identifier            |
    +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+

We always send V=2, P=0, X=0, CC=0. The marker bit (M) is set on the last
fragment of the last slice of a frame (its "end of frame" signal). The 16-bit
RTP sequence number is the packetizer's OWN monotonic counter (wrapping mod
2**16), distinct from ``MediaUnit.seq`` on the source's frames/slices.

Decision: a ``PACKET``'s ``MediaUnit.seq`` IS its RTP sequence number, so
downstream jitter/dedup key on the wire sequence directly; the originating
frame's seq and pts are preserved in ``meta`` (``frame_seq`` / ``frame_pts``).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from yoke.media.pipeline import StageBase
from yoke.media.types import (
    DEFAULT_MTU,
    PT_GENERIC,
    Body,
    EventKind,
    MediaUnit,
    StageEvent,
    StageOutcome,
    UnitKind,
)

RTP_VERSION = 2
RTP_HEADER_LEN = 12
_SEQ_MASK = 0xFFFF
_TS_MASK = 0xFFFF_FFFF
_SSRC_MASK = 0xFFFF_FFFF
_PT_MASK = 0x7F
# struct: version/pad/ext/cc byte, marker/pt byte, u16 seq, u32 ts, u32 ssrc.
_HEADER = struct.Struct("!BBHII")


class RtpError(ValueError):
    """A datagram is not a well-formed RTP header we accept (caller drops it)."""


@dataclass(frozen=True)
class RtpHeader:
    """The parsed 12-byte fixed RTP header (no CSRC/extension)."""

    marker: bool
    payload_type: int
    sequence: int
    timestamp: int
    ssrc: int
    version: int = RTP_VERSION
    padding: bool = False
    extension: bool = False
    csrc_count: int = 0


def encode_rtp_header(
    *,
    marker: bool,
    payload_type: int,
    sequence: int,
    timestamp: int,
    ssrc: int,
) -> bytes:
    """Serialize a fixed 12-byte RTP header (V=2, P=0, X=0, CC=0).

    Ranges are validated so a caller bug is caught rather than silently
    truncated. ``sequence`` must already be reduced mod 2**16 and
    ``timestamp``/``ssrc`` mod 2**32 by the caller (they are modular fields).
    """
    if not 0 <= payload_type <= _PT_MASK:
        raise RtpError(f"payload_type out of range: {payload_type}")
    if not 0 <= sequence <= _SEQ_MASK:
        raise RtpError(f"sequence out of range: {sequence}")
    if not 0 <= timestamp <= _TS_MASK:
        raise RtpError(f"timestamp out of range: {timestamp}")
    if not 0 <= ssrc <= _SSRC_MASK:
        raise RtpError(f"ssrc out of range: {ssrc}")
    b0 = RTP_VERSION << 6  # P=0, X=0, CC=0
    b1 = (0x80 if marker else 0x00) | payload_type
    return _HEADER.pack(b0, b1, sequence, timestamp, ssrc)


def decode_rtp_header(data: bytes) -> RtpHeader:
    """Parse a 12-byte RTP header. Raise :class:`RtpError` on anything we
    do not accept (short buffer, wrong version) so the depacketizer can drop
    a malformed packet without crashing."""
    if len(data) < RTP_HEADER_LEN:
        raise RtpError(f"short header: {len(data)} bytes")
    b0, b1, sequence, timestamp, ssrc = _HEADER.unpack(data[:RTP_HEADER_LEN])
    version = b0 >> 6
    if version != RTP_VERSION:
        raise RtpError(f"bad version: {version}")
    return RtpHeader(
        marker=bool(b1 & 0x80),
        payload_type=b1 & _PT_MASK,
        sequence=sequence,
        timestamp=timestamp,
        ssrc=ssrc,
        version=version,
        padding=bool(b0 & 0x20),
        extension=bool(b0 & 0x10),
        csrc_count=b0 & 0x0F,
    )


class Packetizer(StageBase):
    """Split each ``SLICE`` into ``PACKET`` units (RTP header + fragment).

    A slice's ``meta`` carries ``slice_index`` / ``slice_count`` (all slices
    of a frame share the frame's ``seq`` and ``pts``); the marker bit is set
    on the last fragment of the last slice so the receiver knows the frame's
    final packet. The RTP sequence number is monotonic across the whole
    session and wraps mod 2**16.
    """

    name = "packetize"

    def __init__(
        self,
        mtu: int = DEFAULT_MTU,
        *,
        payload_type: int = PT_GENERIC,
        ssrc: int = 0,
        start_seq: int = 0,
    ) -> None:
        if mtu <= RTP_HEADER_LEN:
            raise ValueError(f"mtu {mtu} must exceed RTP header ({RTP_HEADER_LEN})")
        if not 0 <= ssrc <= _SSRC_MASK:
            raise ValueError(f"ssrc out of range: {ssrc}")
        self._mtu = mtu
        self._max_fragment = mtu - RTP_HEADER_LEN
        self._pt = payload_type
        self._ssrc = ssrc
        self._rtp_seq = start_seq & _SEQ_MASK

    def process(self, unit: MediaUnit, now: float) -> StageOutcome:
        out = StageOutcome()
        if unit.kind is not UnitKind.SLICE:
            out.events.append(
                self._event(EventKind.DROP, unit.seq, unit.pts, now, why="not-a-slice")
            )
            return out

        try:
            slice_index = int(unit.meta["slice_index"])
            slice_count = int(unit.meta["slice_count"])
        except (KeyError, TypeError, ValueError):
            out.events.append(
                self._event(
                    EventKind.DROP, unit.seq, unit.pts, now, why="no-slice-meta"
                )
            )
            return out

        out.events.append(
            self._event(
                EventKind.IN,
                unit.seq,
                unit.pts,
                now,
                slice_index=slice_index,
                bytes=len(unit.payload),
            )
        )

        fragments = self._fragment(unit.payload)
        frag_count = len(fragments)
        is_last_slice = slice_index == slice_count - 1
        ts = unit.pts & _TS_MASK
        for frag_index, fragment in enumerate(fragments):
            marker = is_last_slice and frag_index == frag_count - 1
            rtp_seq = self._rtp_seq
            header = encode_rtp_header(
                marker=marker,
                payload_type=self._pt,
                sequence=rtp_seq,
                timestamp=ts,
                ssrc=self._ssrc,
            )
            meta: Body = {
                "frame_seq": unit.seq,
                "frame_pts": unit.pts,
                "slice_index": slice_index,
                "slice_count": slice_count,
                "frag_index": frag_index,
                "frag_count": frag_count,
                "rtp_seq": rtp_seq,
                "marker": marker,
                "rtp_pt": self._pt,
                "rtp_ssrc": self._ssrc,
            }
            packet = MediaUnit(
                kind=UnitKind.PACKET,
                seq=rtp_seq,
                pts=unit.pts,
                payload=header + fragment,
                source_digest=unit.source_digest,
                meta=meta,
            )
            out.emit.append(packet)
            out.events.append(
                self._event(
                    EventKind.OUT,
                    rtp_seq,
                    unit.pts,
                    now,
                    frag_index=frag_index,
                    frag_count=frag_count,
                    marker=marker,
                )
            )
            self._rtp_seq = (self._rtp_seq + 1) & _SEQ_MASK
        return out

    def _fragment(self, payload: bytes) -> list[bytes]:
        if not payload:
            return [b""]
        step = self._max_fragment
        return [payload[i : i + step] for i in range(0, len(payload), step)]

    def _event(
        self, kind: EventKind, seq: int, pts: int, now: float, **detail: object
    ) -> StageEvent:
        return StageEvent(
            stage=self.name, kind=kind, seq=seq, pts=pts, now=now, detail=dict(detail)
        )


@dataclass
class _SliceBuf:
    frag_count: int | None = None
    frags: dict[int, bytes] = field(default_factory=dict)


@dataclass
class _FrameBuf:
    slice_count: int | None = None
    source_digest: str = ""
    marker_seen: bool = False
    slices: dict[int, _SliceBuf] = field(default_factory=dict)


class Depacketizer(StageBase):
    """Reassemble ``PACKET`` units back into byte-exact ``SLICE`` units.

    Packets are buffered per frame (keyed by ``(frame_seq, frame_pts)`` from
    meta) and per slice; fragments are ordered by ``frag_index`` so arbitrary
    reordering is tolerated. When every slice of a frame has all its fragments
    the slices are emitted (in slice order) and the frame is retired. Malformed
    packets (bad RTP header / missing meta) are dropped with a DROP event, never
    crash. ``flush`` reports any frame still missing fragments as incomplete.
    """

    name = "depacketize"

    def __init__(self) -> None:
        self._frames: dict[tuple[int, int], _FrameBuf] = {}
        self._done: set[tuple[int, int]] = set()

    def process(self, unit: MediaUnit, now: float) -> StageOutcome:
        out = StageOutcome()
        if unit.kind is not UnitKind.PACKET:
            out.events.append(
                self._event(EventKind.DROP, unit.seq, unit.pts, now, why="not-a-packet")
            )
            return out
        try:
            header = decode_rtp_header(unit.payload)
        except RtpError as exc:
            out.events.append(
                self._event(
                    EventKind.DROP, unit.seq, unit.pts, now, why=f"bad-header: {exc}"
                )
            )
            return out
        try:
            frame_seq = int(unit.meta["frame_seq"])
            frame_pts = int(unit.meta["frame_pts"])
            slice_index = int(unit.meta["slice_index"])
            slice_count = int(unit.meta["slice_count"])
            frag_index = int(unit.meta["frag_index"])
            frag_count = int(unit.meta["frag_count"])
        except (KeyError, TypeError, ValueError):
            out.events.append(
                self._event(EventKind.DROP, unit.seq, unit.pts, now, why="bad-meta")
            )
            return out

        out.events.append(self._event(EventKind.IN, unit.seq, frame_pts, now))
        key = (frame_seq, frame_pts)
        if key in self._done:
            out.events.append(
                self._event(
                    EventKind.DROP,
                    unit.seq,
                    frame_pts,
                    now,
                    why="frame-already-emitted",
                )
            )
            return out

        fragment = unit.payload[RTP_HEADER_LEN:]
        frame = self._frames.setdefault(key, _FrameBuf())
        frame.slice_count = slice_count
        frame.source_digest = unit.source_digest
        if header.marker:
            frame.marker_seen = True
        sbuf = frame.slices.setdefault(slice_index, _SliceBuf())
        sbuf.frag_count = frag_count
        sbuf.frags[frag_index] = fragment

        if self._complete(frame):
            out.extend(self._emit_frame(key, frame, now))
        return out

    def flush(self, now: float) -> StageOutcome:
        """Report every still-buffered frame as incomplete (fragments lost),
        then clear the buffers. Emits a DROP event per incomplete frame."""
        out = StageOutcome()
        for (frame_seq, frame_pts), frame in self._frames.items():
            missing = self._missing(frame)
            out.events.append(
                self._event(
                    EventKind.DROP,
                    frame_seq,
                    frame_pts,
                    now,
                    incomplete=True,
                    missing=missing,
                    marker_seen=frame.marker_seen,
                )
            )
            self._done.add((frame_seq, frame_pts))
        self._frames.clear()
        return out

    def _emit_frame(
        self, key: tuple[int, int], frame: _FrameBuf, now: float
    ) -> StageOutcome:
        out = StageOutcome()
        frame_seq, frame_pts = key
        assert frame.slice_count is not None
        for slice_index in range(frame.slice_count):
            sbuf = frame.slices[slice_index]
            assert sbuf.frag_count is not None
            payload = b"".join(sbuf.frags[f] for f in range(sbuf.frag_count))
            out.emit.append(
                MediaUnit(
                    kind=UnitKind.SLICE,
                    seq=frame_seq,
                    pts=frame_pts,
                    payload=payload,
                    source_digest=frame.source_digest,
                    meta={"slice_index": slice_index, "slice_count": frame.slice_count},
                )
            )
            out.events.append(
                self._event(
                    EventKind.OUT, frame_seq, frame_pts, now, slice_index=slice_index
                )
            )
        self._done.add(key)
        del self._frames[key]
        return out

    @staticmethod
    def _complete(frame: _FrameBuf) -> bool:
        if frame.slice_count is None or len(frame.slices) < frame.slice_count:
            return False
        for slice_index in range(frame.slice_count):
            sbuf = frame.slices.get(slice_index)
            if sbuf is None or sbuf.frag_count is None:
                return False
            if any(f not in sbuf.frags for f in range(sbuf.frag_count)):
                return False
        return True

    @staticmethod
    def _missing(frame: _FrameBuf) -> list[list[int]]:
        missing: list[list[int]] = []
        if frame.slice_count is None:
            return missing
        for slice_index in range(frame.slice_count):
            sbuf = frame.slices.get(slice_index)
            if sbuf is None or sbuf.frag_count is None:
                missing.append([slice_index, -1])  # whole slice absent
                continue
            missing.extend(
                [slice_index, f] for f in range(sbuf.frag_count) if f not in sbuf.frags
            )
        return missing

    def _event(
        self, kind: EventKind, seq: int, pts: int, now: float, **detail: object
    ) -> StageEvent:
        return StageEvent(
            stage=self.name, kind=kind, seq=seq, pts=pts, now=now, detail=dict(detail)
        )
