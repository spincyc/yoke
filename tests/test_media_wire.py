"""Deterministic malformed reference-wire inputs never enter reassembly."""

from __future__ import annotations

import random
import struct
from contextlib import suppress
from dataclasses import replace

import pytest

from yoke.media.assembly import MAX_PATH_MTU, MediaConfig, build_sender
from yoke.media.stages.packetize import Depacketizer, Packetizer, RtpError
from yoke.media.types import (
    IPV4_UDP_HEADER_LEN,
    MAX_UDP_BYTES,
    MediaUnit,
    UnitKind,
    digest_of,
)
from yoke.media.wire import TRAILER_LEN, WireError, pack_packet, unpack_packet


def packet() -> MediaUnit:
    unit = MediaUnit(
        UnitKind.SLICE,
        9,
        0x1_0000_0001,
        b"payload",
        "a" * 64,
        {"slice_index": 0, "slice_count": 1},
    )
    return Packetizer(ssrc=7, start_seq=65535).process(unit, 0.0).emit[0]


@pytest.mark.parametrize("path", ["", "A", "B"])
def test_reference_wire_roundtrip_retains_wrapped_identity(path: str) -> None:
    original = packet()
    if path:
        original = original.with_payload(original.payload, path=path)
    raw = pack_packet(original)
    assert len(raw) == len(original.payload) + TRAILER_LEN
    assert unpack_packet(raw) == original


@pytest.mark.parametrize("flag", [0x20, 0x10, 0x01, 0x0F])
def test_fixed_header_rejects_other_rtp_layouts(flag: int) -> None:
    raw = bytearray(pack_packet(packet()))
    raw[0] |= flag
    with pytest.raises(RtpError):
        unpack_packet(bytes(raw))


@pytest.mark.parametrize("offset,value", [(16, 1), (18, 0), (20, 1), (22, 0)])
def test_trailer_rejects_out_of_range_indices_and_counts(
    offset: int, value: int
) -> None:
    raw = bytearray(pack_packet(packet()))
    struct.pack_into("!H", raw, 12 + offset, value)
    with pytest.raises(WireError):
        unpack_packet(bytes(raw))


def test_trailer_rejects_unknown_path_and_timestamp_conflict() -> None:
    raw = bytearray(pack_packet(packet()))
    raw[12 + 24] = 3
    with pytest.raises(WireError, match="path"):
        unpack_packet(bytes(raw))
    raw = bytearray(pack_packet(packet()))
    raw[7] ^= 1
    with pytest.raises(WireError, match="identity"):
        unpack_packet(bytes(raw))


@pytest.mark.parametrize(
    "meta", [{"frag_count": 0}, {"path": "C"}, {"slice_index": 1.0}, {"rtp_seq": -1}]
)
def test_serializer_rejects_inconsistent_metadata(meta: dict) -> None:
    original = packet()
    with pytest.raises(WireError):
        pack_packet(original.with_payload(original.payload, **meta))


def test_wire_rejects_oversize_udp_payload_before_copying() -> None:
    original = packet()
    with pytest.raises(WireError, match="maximum"):
        pack_packet(
            replace(original, payload=original.payload[:12] + bytes(MAX_UDP_BYTES))
        )
    with pytest.raises(WireError, match="maximum"):
        unpack_packet(bytes(MAX_UDP_BYTES + 1))


def test_wire_rejects_offline_xor_metadata_instead_of_losing_it() -> None:
    original = packet()
    with pytest.raises(WireError, match="offline-only"):
        pack_packet(
            original.with_payload(
                original.payload, fec_group=1, fec_index=0, fec_size=8
            )
        )


def test_seeded_garbage_has_only_documented_parse_errors() -> None:
    rng = random.Random(7)
    for _ in range(1000):
        raw = rng.randbytes(rng.randrange(256))
        with suppress(WireError, RtpError):
            unpack_packet(raw)


@pytest.mark.parametrize("mtu", [1500, 9000, MAX_PATH_MTU])
@pytest.mark.parametrize("fec", ["none", "dup"])
def test_configured_path_mtu_bounds_complete_wire_packets_and_reassembles(
    mtu: int, fec: str
) -> None:
    config = MediaConfig(mtu=mtu, n_slices=1, fec=fec)
    sender = build_sender(config)
    # Use enough real bytes to fill both Ethernet and jumbo packets; the
    # normal synthetic frame models the raster with only a tiny payload.
    payload = bytes(range(256)) * 600
    original = MediaUnit(UnitKind.FRAME, 7, 9000, payload, digest_of(payload))
    packets = sender.pipeline.push(original, 0.0).emitted
    datagrams = [pack_packet(unit) for unit in packets]
    assert max(len(raw) + IPV4_UDP_HEADER_LEN for raw in datagrams) == mtu
    assert all(len(raw) + IPV4_UDP_HEADER_LEN <= mtu for raw in datagrams)
    depacketizer = Depacketizer()
    slices = []
    for raw in reversed(datagrams):
        slices.extend(depacketizer.process(unpack_packet(raw), 0.0).emit)
    assert len(slices) == 1
    assert slices[0].payload == payload
    assert slices[0].source_digest == original.source_digest
