"""Wire (de)serialization for the software reference pipeline.

The sans-I/O stages pass rich metadata between each other in-process, but
only bytes cross a real socket. This module bridges the two: a PACKET
``MediaUnit`` becomes ``[12-byte RTP header][yoke trailer][fragment]`` and
back, losslessly. The RTP header stays first and standard so the
black-box snooper (``decode_rtp_header``) parses sequence/marker/timestamp
straight off the wire; the trailer carries the pipeline metadata RTP does
not (frame identity, slice/fragment indices, the end-to-end digest).

This compact trailer is the *reference/test* framing. The production
datapath is GStreamer/MTL emitting real RFC 4175 / JPEG-XS payloads (the
``stream`` driver); this format exists so the whole Python pipeline runs
end-to-end over loopback and two hosts with full fidelity and offline
tests. See doc/design/MEDIA.md.
"""

from __future__ import annotations

import struct

from yoke.media.stages.packetize import (
    RTP_HEADER_LEN,
    RtpError,
    decode_rtp_header,
)
from yoke.media.types import Body, MediaUnit, UnitKind

# frame_seq, frame_pts, slice_index, slice_count, frag_index, frag_count,
# path (0=none, 1=A, 2=B for ST 2022-7 duplicate FEC), source digest (32 B).
_TRAILER = struct.Struct("!QQHHHHB32s")
TRAILER_LEN = _TRAILER.size
_PATHS = {"": 0, "A": 1, "B": 2}
_PATHS_INV = {0: "", 1: "A", 2: "B"}


class WireError(ValueError):
    """A datagram cannot be parsed as a yoke media packet (caller drops)."""


def rtp_header_body(raw: bytes) -> Body:
    """Adapt ``decode_rtp_header`` to the dict the :class:`MediaSnooper`
    black-box decoder expects (``{seq, timestamp, marker, pt, ssrc}``).
    Inject this — not ``decode_rtp_header`` directly, which returns a
    dataclass — as the snooper's header decoder for on-wire verification.
    Raises :class:`RtpError` on a malformed header (snooper counts it)."""
    header = decode_rtp_header(raw)
    return {
        "seq": header.sequence,
        "timestamp": header.timestamp,
        "marker": header.marker,
        "pt": header.payload_type,
        "ssrc": header.ssrc,
    }


def pack_packet(unit: MediaUnit) -> bytes:
    """Serialize a PACKET ``MediaUnit`` to wire bytes.

    ``unit.payload`` is the packetizer's ``RTP header + fragment``; the
    trailer is spliced in between so the leading 12 bytes remain a valid
    RTP header on the wire.
    """
    if unit.kind is not UnitKind.PACKET:
        raise WireError(f"can only pack PACKET units, got {unit.kind}")
    if len(unit.payload) < RTP_HEADER_LEN:
        raise WireError("packet payload shorter than an RTP header")
    meta = unit.meta
    try:
        digest = bytes.fromhex(unit.source_digest)
    except ValueError as exc:
        raise WireError(f"bad source_digest: {unit.source_digest!r}") from exc
    if len(digest) != 32:
        raise WireError("source_digest is not a 32-byte sha256")
    path = _PATHS.get(str(meta.get("path", "")), 0)
    try:
        trailer = _TRAILER.pack(
            int(meta["frame_seq"]),
            int(meta["frame_pts"]),
            int(meta["slice_index"]),
            int(meta["slice_count"]),
            int(meta["frag_index"]),
            int(meta["frag_count"]),
            path,
            digest,
        )
    except (KeyError, struct.error, TypeError, ValueError) as exc:
        raise WireError(f"packet meta not serializable: {exc}") from exc
    rtp = unit.payload[:RTP_HEADER_LEN]
    fragment = unit.payload[RTP_HEADER_LEN:]
    return rtp + trailer + fragment


def unpack_packet(raw: bytes) -> MediaUnit:
    """Parse wire bytes back into a PACKET ``MediaUnit`` the receiver
    pipeline (fec-decode → jitter → depacketize) consumes. Raises
    :class:`WireError` (or :class:`RtpError`) on malformed input so the
    receiver drops it and counts it, never crashes."""
    if len(raw) < RTP_HEADER_LEN + TRAILER_LEN:
        raise WireError(f"short datagram: {len(raw)} bytes")
    header = decode_rtp_header(raw)  # RtpError on bad header
    trailer = raw[RTP_HEADER_LEN : RTP_HEADER_LEN + TRAILER_LEN]
    (
        frame_seq,
        frame_pts,
        slice_index,
        slice_count,
        frag_index,
        frag_count,
        path,
        digest,
    ) = _TRAILER.unpack(trailer)
    fragment = raw[RTP_HEADER_LEN + TRAILER_LEN :]
    # Rebuild exactly what the packetizer emitted: RTP header + fragment.
    payload = raw[:RTP_HEADER_LEN] + fragment
    meta: Body = {
        "frame_seq": frame_seq,
        "frame_pts": frame_pts,
        "slice_index": slice_index,
        "slice_count": slice_count,
        "frag_index": frag_index,
        "frag_count": frag_count,
        "rtp_seq": header.sequence,
        "marker": header.marker,
        "rtp_pt": header.payload_type,
        "rtp_ssrc": header.ssrc,
    }
    if path:
        meta["path"] = _PATHS_INV.get(path, "")
    return MediaUnit(
        kind=UnitKind.PACKET,
        seq=header.sequence,
        pts=frame_pts,
        payload=payload,
        source_digest=digest.hex(),
        meta=meta,
    )


__all__ = [
    "TRAILER_LEN",
    "RtpError",
    "WireError",
    "pack_packet",
    "rtp_header_body",
    "unpack_packet",
]
