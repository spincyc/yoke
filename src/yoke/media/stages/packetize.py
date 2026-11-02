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
from collections import OrderedDict
from dataclasses import dataclass, field

from yoke.media.pipeline import StageBase
from yoke.media.types import (
    DEFAULT_MTU,
    MAX_BUFFER_BYTES,
    MAX_FRAME_BYTES,
    MAX_FRAME_FRAGMENTS,
    MAX_PENDING_FRAMES,
    MAX_UDP_BYTES,
    PT_GENERIC,
    REFERENCE_TRAILER_LEN,
    Body,
    EventKind,
    MediaUnit,
    StageEvent,
    StageOutcome,
    UnitKind,
    is_digest,
    require_uint,
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
    if type(marker) is not bool:
        raise RtpError("marker must be a boolean")
    try:
        require_uint(payload_type, "payload_type", _PT_MASK)
        require_uint(sequence, "sequence", _SEQ_MASK)
        require_uint(timestamp, "timestamp", _TS_MASK)
        require_uint(ssrc, "ssrc", _SSRC_MASK)
    except ValueError as exc:
        raise RtpError(str(exc)) from exc
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
    if b0 & 0x3F:
        raise RtpError("padding, extensions and CSRCs are unsupported")
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


@dataclass(frozen=True)
class PacketIdentity:
    frame_seq: int
    frame_pts: int
    slice_index: int
    slice_count: int
    frag_index: int
    frag_count: int


def packet_identity(unit: MediaUnit, header: RtpHeader) -> PacketIdentity:
    """Validate the fixed-header/trailer contract, before allocating state."""
    values = [
        require_uint(unit.meta[name], name, maximum)
        for name, maximum in (
            ("frame_seq", 0xFFFF_FFFF_FFFF_FFFF),
            ("frame_pts", 0xFFFF_FFFF_FFFF_FFFF),
            ("slice_index", _SEQ_MASK),
            ("slice_count", _SEQ_MASK),
            ("frag_index", _SEQ_MASK),
            ("frag_count", _SEQ_MASK),
        )
    ]
    identity = PacketIdentity(*values)
    if not 0 <= identity.slice_index < identity.slice_count:
        raise ValueError("slice index/count out of range")
    if not 0 <= identity.frag_index < identity.frag_count:
        raise ValueError("fragment index/count out of range")
    if (
        type(unit.seq) is not int
        or unit.seq != header.sequence
        or type(unit.pts) is not int
        or unit.pts != identity.frame_pts
        or header.timestamp != (identity.frame_pts & _TS_MASK)
    ):
        raise ValueError("packet/header identity mismatch")
    marker = (
        identity.slice_index == identity.slice_count - 1
        and identity.frag_index == identity.frag_count - 1
    )
    if header.marker != marker:
        raise ValueError("marker disagrees with final fragment")
    for key, expected in (
        ("rtp_seq", header.sequence),
        ("rtp_pt", header.payload_type),
        ("rtp_ssrc", header.ssrc),
        ("marker", header.marker),
    ):
        if key in unit.meta and (
            type(unit.meta[key]) is not type(expected) or unit.meta[key] != expected
        ):
            raise ValueError(f"{key} disagrees with RTP header")
    if not is_digest(unit.source_digest):
        raise ValueError("source_digest is not a SHA-256 digest")
    if unit.meta.get("path", "") not in {"", "A", "B"}:
        raise ValueError("invalid duplicate path")
    return identity


class Packetizer(StageBase):
    """Split each ``SLICE`` into ``PACKET`` units (RTP header + fragment).

    ``mtu`` here is the internal RTP packet byte budget, including the
    12-byte RTP header, excluding the reference trailer and IP/UDP headers.
    It can be tiny in offline fragmentation tests. ``build_sender``
    reserves every wrapping header from ``MediaConfig.mtu`` (path MTU)
    before constructing this low-level stage.

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
        require_uint(mtu, "mtu", MAX_UDP_BYTES - REFERENCE_TRAILER_LEN)
        if mtu <= RTP_HEADER_LEN:
            raise ValueError(f"mtu {mtu} must exceed RTP header ({RTP_HEADER_LEN})")
        require_uint(ssrc, "ssrc", _SSRC_MASK)
        require_uint(payload_type, "payload_type", _PT_MASK)
        require_uint(start_seq, "start_seq", 0xFFFF_FFFF_FFFF_FFFF)
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
            slice_index = require_uint(
                unit.meta["slice_index"], "slice_index", _SEQ_MASK
            )
            slice_count = require_uint(
                unit.meta["slice_count"], "slice_count", _SEQ_MASK
            )
            require_uint(unit.seq, "frame_seq", 0xFFFF_FFFF_FFFF_FFFF)
            require_uint(unit.pts, "frame_pts", 0xFFFF_FFFF_FFFF_FFFF)
            if not is_digest(unit.source_digest):
                raise ValueError("source_digest is not a SHA-256 digest")
            if not 0 <= slice_index < slice_count:
                raise ValueError("slice index/count out of range")
            if len(unit.payload) > MAX_FRAME_BYTES:
                raise ValueError("slice exceeds frame byte budget")
            if (
                max(
                    1,
                    (len(unit.payload) + self._max_fragment - 1) // self._max_fragment,
                )
                > _SEQ_MASK
            ):
                raise ValueError("too many fragments")
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
    frag_count: int
    frags: dict[int, tuple[int, bytes]] = field(default_factory=dict)


@dataclass
class _FrameBuf:
    slice_count: int
    source_digest: str
    ssrc: int
    payload_type: int
    marker_seen: bool = False
    slices: dict[int, _SliceBuf] = field(default_factory=dict)
    bytes: int = 0
    declared_fragments: int = 0
    complete_slices: int = 0


class Depacketizer(StageBase):
    """Byte-exact reassembly with explicit frame, byte and fragment budgets.

    Oldest incomplete frames are retired at capacity. Recently retired
    identities are remembered in a bounded window, so duplicates cannot
    revive them. Conflicting identity/count/fragment data retires a frame.
    Missing-fragment diagnostics contain at most 256 sample entries.
    """

    name = "depacketize"

    def __init__(
        self,
        *,
        max_frames: int = MAX_PENDING_FRAMES,
        max_bytes: int = MAX_BUFFER_BYTES,
        max_frame_bytes: int = MAX_FRAME_BYTES,
        max_fragments: int = MAX_FRAME_FRAGMENTS,
        done_window: int = 1024,
    ) -> None:
        for name, value in (
            ("max_frames", max_frames),
            ("max_bytes", max_bytes),
            ("max_frame_bytes", max_frame_bytes),
            ("max_fragments", max_fragments),
            ("done_window", done_window),
        ):
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        self._max_frames = max_frames
        self._max_bytes = max_bytes
        self._max_frame_bytes = max_frame_bytes
        self._max_fragments = max_fragments
        self._done_window = done_window
        self._frames: OrderedDict[tuple[int, int], _FrameBuf] = OrderedDict()
        self._done: OrderedDict[tuple[int, int], None] = OrderedDict()
        self._bytes = 0

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
            identity = packet_identity(unit, header)
        except (KeyError, TypeError, ValueError) as exc:
            out.events.append(
                self._event(
                    EventKind.DROP, unit.seq, unit.pts, now, why=f"bad-packet: {exc}"
                )
            )
            return out
        frame_seq, frame_pts = identity.frame_seq, identity.frame_pts
        key = (frame_seq, frame_pts)
        out.events.append(self._event(EventKind.IN, unit.seq, frame_pts, now))
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
        if len(fragment) > min(self._max_frame_bytes, self._max_bytes):
            out.events.append(
                self._event(
                    EventKind.DROP, unit.seq, frame_pts, now, why="frame-byte-budget"
                )
            )
            return out
        frame = self._frames.get(key)
        if frame is None:
            while len(self._frames) >= self._max_frames:
                self._retire(next(iter(self._frames)), now, out, "frame-capacity")
            frame = _FrameBuf(
                identity.slice_count,
                unit.source_digest,
                header.ssrc,
                header.payload_type,
            )
            self._frames[key] = frame
        if (frame.slice_count, frame.source_digest, frame.ssrc, frame.payload_type) != (
            identity.slice_count,
            unit.source_digest,
            header.ssrc,
            header.payload_type,
        ):
            self._retire(key, now, out, "conflicting-frame-meta")
            return out
        sbuf = frame.slices.get(identity.slice_index)
        if sbuf is not None:
            if sbuf.frag_count != identity.frag_count:
                self._retire(key, now, out, "conflicting-fragment-count")
                return out
            previous = sbuf.frags.get(identity.frag_index)
            if previous is not None:
                if previous != (header.sequence, fragment):
                    self._retire(key, now, out, "conflicting-fragment")
                return out
        if frame.bytes + len(fragment) > self._max_frame_bytes:
            self._retire(key, now, out, "frame-byte-budget")
            return out
        if sbuf is None:
            if frame.declared_fragments + identity.frag_count > self._max_fragments:
                self._retire(key, now, out, "fragment-budget")
                return out
            sbuf = _SliceBuf(identity.frag_count)
            frame.slices[identity.slice_index] = sbuf
            frame.declared_fragments += identity.frag_count
        while self._bytes + len(fragment) > self._max_bytes:
            victim = next(iter(self._frames))
            self._retire(victim, now, out, "buffer-byte-budget")
            if victim == key:
                return out
        sbuf.frags[identity.frag_index] = (header.sequence, fragment)
        frame.bytes += len(fragment)
        self._bytes += len(fragment)
        frame.marker_seen |= header.marker
        if len(sbuf.frags) == sbuf.frag_count:
            frame.complete_slices += 1
        if frame.complete_slices == frame.slice_count:
            for slice_index in range(frame.slice_count):
                buf = frame.slices[slice_index]
                payload = b"".join(buf.frags[f][1] for f in range(buf.frag_count))
                out.emit.append(
                    MediaUnit(
                        kind=UnitKind.SLICE,
                        seq=frame_seq,
                        pts=frame_pts,
                        payload=payload,
                        source_digest=frame.source_digest,
                        meta={
                            "slice_index": slice_index,
                            "slice_count": frame.slice_count,
                        },
                    )
                )
                out.events.append(
                    self._event(
                        EventKind.OUT,
                        frame_seq,
                        frame_pts,
                        now,
                        slice_index=slice_index,
                    )
                )
            self._remove(key)
        return out

    def flush(self, now: float) -> StageOutcome:
        out = StageOutcome()
        while self._frames:
            self._retire(next(iter(self._frames)), now, out, "incomplete")
        return out

    def _remove(self, key: tuple[int, int]) -> None:
        self._bytes -= self._frames.pop(key).bytes
        self._done[key] = None
        while len(self._done) > self._done_window:
            self._done.popitem(last=False)

    def _retire(
        self, key: tuple[int, int], now: float, out: StageOutcome, why: str
    ) -> None:
        frame = self._frames[key]
        missing: list[list[int]] = []
        missing_total = frame.slice_count - len(frame.slices)
        missing_total += sum(b.frag_count - len(b.frags) for b in frame.slices.values())
        for index in range(frame.slice_count):
            if len(missing) >= 256:
                break
            buf = frame.slices.get(index)
            if buf is None:
                missing.append([index, -1])
            else:
                for frag in range(buf.frag_count):
                    if frag not in buf.frags:
                        missing.append([index, frag])
                        if len(missing) >= 256:
                            break
        out.events.append(
            self._event(
                EventKind.DROP,
                key[0],
                key[1],
                now,
                why=why,
                incomplete=True,
                missing=missing,
                missing_total=missing_total,
                marker_seen=frame.marker_seen,
            )
        )
        self._remove(key)

    def _event(
        self, kind: EventKind, seq: int, pts: int, now: float, **detail: object
    ) -> StageEvent:
        return StageEvent(
            stage=self.name, kind=kind, seq=seq, pts=pts, now=now, detail=dict(detail)
        )
