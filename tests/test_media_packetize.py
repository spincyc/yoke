"""Tests for RTP packetization / depacketization (yoke.media.stages.packetize).

Fully deterministic: fixed pts/seq, no network, no wall clock. The RTP header
is the wire format, so header round-trips are byte-exact and reassembly is
byte-for-byte (depacketize(packetize(slice)) == slice payload).
"""

from __future__ import annotations

import random

import pytest

from yoke.media.stages.packetize import (
    RTP_HEADER_LEN,
    Depacketizer,
    Packetizer,
    RtpError,
    RtpHeader,
    decode_rtp_header,
    encode_rtp_header,
)
from yoke.media.types import (
    DEFAULT_MTU,
    PT_JPEGXS,
    PT_RAW,
    EventKind,
    MediaUnit,
    UnitKind,
)

NOW = 0.0
DIGEST = "a" * 64


def make_slice(
    *,
    seq: int,
    pts: int,
    slice_index: int,
    slice_count: int,
    payload: bytes,
) -> MediaUnit:
    return MediaUnit(
        kind=UnitKind.SLICE,
        seq=seq,
        pts=pts,
        payload=payload,
        source_digest=DIGEST,
        meta={"slice_index": slice_index, "slice_count": slice_count},
    )


def packetize(pk: Packetizer, unit: MediaUnit) -> list[MediaUnit]:
    return pk.process(unit, NOW).emit


def depacketize(dp: Depacketizer, packets: list[MediaUnit]) -> tuple[list, list]:
    emitted: list[MediaUnit] = []
    events: list = []
    for packet in packets:
        out = dp.process(packet, NOW)
        emitted.extend(out.emit)
        events.extend(out.events)
    return emitted, events


# --------------------------------------------------------------------------
# RTP header, byte-exact
# --------------------------------------------------------------------------


def test_header_layout_bytes() -> None:
    raw = encode_rtp_header(
        marker=False,
        payload_type=PT_RAW,
        sequence=0x1234,
        timestamp=0xDEADBEEF,
        ssrc=0x01020304,
    )
    assert len(raw) == RTP_HEADER_LEN
    assert raw[0] == 0x80  # V=2, P=0, X=0, CC=0
    assert raw[1] == PT_RAW  # marker clear
    assert raw[2:4] == b"\x12\x34"
    assert raw[4:8] == b"\xde\xad\xbe\xef"
    assert raw[8:12] == b"\x01\x02\x03\x04"


def test_header_marker_bit() -> None:
    raw = encode_rtp_header(
        marker=True, payload_type=PT_JPEGXS, sequence=1, timestamp=2, ssrc=3
    )
    assert raw[1] == 0x80 | PT_JPEGXS
    assert decode_rtp_header(raw).marker is True


@pytest.mark.parametrize(
    "marker,pt,seq,ts,ssrc",
    [
        (False, 96, 0, 0, 0),
        (True, 127, 0xFFFF, 0xFFFFFFFF, 0xFFFFFFFF),
        (False, 98, 0x8000, 0x7FFFFFFF, 0x12345678),
    ],
)
def test_header_round_trip(marker: bool, pt: int, seq: int, ts: int, ssrc: int) -> None:
    raw = encode_rtp_header(
        marker=marker, payload_type=pt, sequence=seq, timestamp=ts, ssrc=ssrc
    )
    hdr = decode_rtp_header(raw)
    assert hdr == RtpHeader(
        marker=marker, payload_type=pt, sequence=seq, timestamp=ts, ssrc=ssrc
    )


def test_header_wraparound_sequence() -> None:
    assert (
        decode_rtp_header(
            encode_rtp_header(
                marker=False, payload_type=96, sequence=0xFFFF, timestamp=0, ssrc=0
            )
        ).sequence
        == 0xFFFF
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"payload_type": 128, "sequence": 0, "timestamp": 0, "ssrc": 0},
        {"payload_type": -1, "sequence": 0, "timestamp": 0, "ssrc": 0},
        {"payload_type": 96, "sequence": 0x10000, "timestamp": 0, "ssrc": 0},
        {"payload_type": 96, "sequence": 0, "timestamp": 0x1_0000_0000, "ssrc": 0},
        {"payload_type": 96, "sequence": 0, "timestamp": 0, "ssrc": -1},
    ],
)
def test_header_out_of_range_rejected(kwargs: dict) -> None:
    with pytest.raises(RtpError):
        encode_rtp_header(marker=False, **kwargs)


def test_decode_rejects_short_and_bad_version() -> None:
    with pytest.raises(RtpError):
        decode_rtp_header(b"\x80\x60\x00")  # < 12 bytes
    good = encode_rtp_header(
        marker=False, payload_type=96, sequence=1, timestamp=1, ssrc=1
    )
    bad = bytes([0xC0]) + good[1:]  # version = 3
    with pytest.raises(RtpError):
        decode_rtp_header(bad)


# --------------------------------------------------------------------------
# Packetize -> depacketize round trips
# --------------------------------------------------------------------------


def test_single_fragment_round_trip() -> None:
    pk = Packetizer(mtu=DEFAULT_MTU, payload_type=PT_JPEGXS, ssrc=0xABCD)
    dp = Depacketizer()
    original = make_slice(
        seq=7, pts=9000, slice_index=0, slice_count=1, payload=b"hello world"
    )
    packets = packetize(pk, original)
    assert len(packets) == 1
    packet = packets[0]
    assert packet.kind is UnitKind.PACKET
    assert packet.seq == 0  # PACKET.seq is the rtp sequence, not the frame seq
    assert packet.meta["frame_seq"] == 7
    assert packet.meta["frame_pts"] == 9000
    hdr = decode_rtp_header(packet.payload)
    assert hdr.marker is True  # last frag of the only (last) slice
    assert hdr.timestamp == 9000
    assert hdr.ssrc == 0xABCD
    emitted, _ = depacketize(dp, packets)
    assert len(emitted) == 1
    assert emitted[0].kind is UnitKind.SLICE
    assert emitted[0].payload == original.payload
    assert emitted[0].seq == 7
    assert emitted[0].pts == 9000
    assert emitted[0].meta == {"slice_index": 0, "slice_count": 1}


def test_multi_fragment_round_trip_byte_exact() -> None:
    pk = Packetizer(mtu=RTP_HEADER_LEN + 4)  # 4-byte fragments
    dp = Depacketizer()
    payload = bytes(range(10))  # 10 bytes -> 4 + 4 + 2 = 3 packets
    original = make_slice(
        seq=1, pts=3000, slice_index=0, slice_count=1, payload=payload
    )
    packets = packetize(pk, original)
    assert len(packets) == 3
    assert [len(p.payload) - RTP_HEADER_LEN for p in packets] == [4, 4, 2]
    # marker only on the final fragment.
    assert [decode_rtp_header(p.payload).marker for p in packets] == [
        False,
        False,
        True,
    ]
    emitted, _ = depacketize(dp, packets)
    assert len(emitted) == 1
    assert emitted[0].payload == payload


def test_reordering_tolerated() -> None:
    pk = Packetizer(mtu=RTP_HEADER_LEN + 3)
    dp = Depacketizer()
    payload = bytes(range(20))
    original = make_slice(
        seq=2, pts=1234, slice_index=0, slice_count=1, payload=payload
    )
    packets = packetize(pk, original)
    rng = random.Random(42)
    shuffled = packets[:]
    rng.shuffle(shuffled)
    emitted, _ = depacketize(dp, shuffled)
    assert len(emitted) == 1
    assert emitted[0].payload == payload


def test_multi_slice_frame_completes_together() -> None:
    pk = Packetizer(mtu=RTP_HEADER_LEN + 4)
    dp = Depacketizer()
    s0 = make_slice(seq=5, pts=90, slice_index=0, slice_count=2, payload=b"AAAAAA")
    s1 = make_slice(seq=5, pts=90, slice_index=1, slice_count=2, payload=b"BBB")
    packets = packetize(pk, s0) + packetize(pk, s1)
    # only the last fragment of the last slice carries the marker.
    markers = [decode_rtp_header(p.payload).marker for p in packets]
    assert markers.count(True) == 1
    assert markers[-1] is True
    # feed all slice-0 packets first: frame not complete until slice-1 arrives.
    emitted, _ = depacketize(dp, packets[:2])
    assert emitted == []
    emitted, _ = depacketize(dp, packets[2:])
    assert len(emitted) == 2
    assert emitted[0].meta["slice_index"] == 0
    assert emitted[1].meta["slice_index"] == 1
    assert emitted[0].payload == b"AAAAAA"
    assert emitted[1].payload == b"BBB"


def test_rtp_sequence_monotonic_and_wraps() -> None:
    pk = Packetizer(mtu=RTP_HEADER_LEN + 4, start_seq=0xFFFF)
    s = make_slice(
        seq=1, pts=1, slice_index=0, slice_count=1, payload=bytes(6)
    )  # 2 frags
    packets = packetize(pk, s)
    assert [p.seq for p in packets] == [0xFFFF, 0x0000]
    more = packetize(
        pk, make_slice(seq=2, pts=2, slice_index=0, slice_count=1, payload=b"z")
    )
    assert more[0].seq == 0x0001


def test_empty_slice_produces_one_packet() -> None:
    pk = Packetizer()
    dp = Depacketizer()
    s = make_slice(seq=1, pts=1, slice_index=0, slice_count=1, payload=b"")
    packets = packetize(pk, s)
    assert len(packets) == 1
    emitted, _ = depacketize(dp, packets)
    assert emitted[0].payload == b""


# --------------------------------------------------------------------------
# Failure handling
# --------------------------------------------------------------------------


def test_missing_fragment_reported_incomplete_not_crashed() -> None:
    pk = Packetizer(mtu=RTP_HEADER_LEN + 4)
    dp = Depacketizer()
    original = make_slice(
        seq=3, pts=7, slice_index=0, slice_count=1, payload=bytes(range(12))
    )
    packets = packetize(pk, original)  # 3 packets
    assert len(packets) == 3
    lossy = [packets[0], packets[2]]  # drop the middle fragment
    emitted, _ = depacketize(dp, lossy)
    assert emitted == []  # frame never completes
    flush = dp.flush(NOW)
    assert flush.emit == []
    drops = [e for e in flush.events if e.kind is EventKind.DROP]
    assert len(drops) == 1
    assert drops[0].detail["incomplete"] is True
    assert [0, 1] in drops[0].detail["missing"]  # slice 0, frag 1 missing


def test_malformed_packet_dropped() -> None:
    pk = Packetizer()
    dp = Depacketizer()
    packet = packetize(
        pk, make_slice(seq=1, pts=1, slice_index=0, slice_count=1, payload=b"x")
    )[0]
    corrupt = MediaUnit(
        kind=UnitKind.PACKET,
        seq=packet.seq,
        pts=packet.pts,
        payload=bytes([0xC0]) + packet.payload[1:],  # bad RTP version
        source_digest=packet.source_digest,
        meta=dict(packet.meta),
    )
    out = dp.process(corrupt, NOW)
    assert out.emit == []
    drops = [e for e in out.events if e.kind is EventKind.DROP]
    assert len(drops) == 1
    assert "bad-header" in drops[0].detail["why"]


def test_non_slice_input_dropped_by_packetizer() -> None:
    pk = Packetizer()
    frame = MediaUnit(
        kind=UnitKind.FRAME, seq=1, pts=1, payload=b"x", source_digest=DIGEST
    )
    out = pk.process(frame, NOW)
    assert out.emit == []
    assert any(e.kind is EventKind.DROP for e in out.events)


def test_events_in_out_emitted() -> None:
    pk = Packetizer(mtu=RTP_HEADER_LEN + 4)
    dp = Depacketizer()
    original = make_slice(seq=1, pts=1, slice_index=0, slice_count=1, payload=bytes(8))
    packets = packetize(pk, original)
    pk_events = pk.process(
        make_slice(seq=2, pts=2, slice_index=0, slice_count=1, payload=bytes(8)), NOW
    ).events
    assert pk_events[0].kind is EventKind.IN
    assert sum(e.kind is EventKind.OUT for e in pk_events) == 2
    _, dp_events = depacketize(dp, packets)
    assert sum(e.kind is EventKind.IN for e in dp_events) == 2
    assert sum(e.kind is EventKind.OUT for e in dp_events) == 1  # one slice reassembled
