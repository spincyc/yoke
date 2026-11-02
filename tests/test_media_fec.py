"""Tests for FEC: XOR block parity and ST 2022-7 duplicate paths.

Deterministic and sans-I/O. Loss/reorder is modelled by simply not feeding (or
reordering) units into the decoder -- the network sim is a separate lane.
"""

from __future__ import annotations

from yoke.media.stages.fec import (
    DuplicatePathFec,
    FecDecodeStage,
    FecEncodeStage,
    SeamlessMerger,
    XorParityDecoder,
    XorParityFec,
    xor_bytes,
)
from yoke.media.types import EventKind, MediaUnit, UnitKind

NOW = 0.0
DIGEST = "b" * 64


def make_packet(
    *, rtp_seq: int, pts: int, payload: bytes, frame_seq: int = 100
) -> MediaUnit:
    return MediaUnit(
        kind=UnitKind.PACKET,
        seq=rtp_seq,
        pts=pts,
        payload=payload,
        source_digest=DIGEST,
        meta={"frame_seq": frame_seq, "frag_index": rtp_seq},
    )


def encode_group(enc: XorParityFec, units: list[MediaUnit]) -> list[MediaUnit]:
    out: list[MediaUnit] = []
    for unit in units:
        out.extend(enc.push(unit))
    return out


# --------------------------------------------------------------------------
# xor_bytes primitive
# --------------------------------------------------------------------------


def test_xor_bytes_padding() -> None:
    assert xor_bytes([b"\x0f\x0f", b"\xf0"]) == b"\xff\x0f"
    assert xor_bytes([]) == b""
    assert xor_bytes([b"\xaa", b"\xaa"]) == b"\x00"


# --------------------------------------------------------------------------
# XOR block parity
# --------------------------------------------------------------------------


def test_encoder_fans_out_data_plus_parity() -> None:
    enc = XorParityFec(group=3)
    units = [
        make_packet(rtp_seq=i, pts=1, payload=bytes([i]) * (i + 1)) for i in range(3)
    ]
    emitted = encode_group(enc, units)
    assert len(emitted) == 4  # 3 data + 1 parity
    assert [u.kind for u in emitted[:3]] == [UnitKind.PACKET] * 3
    assert emitted[3].kind is UnitKind.PARITY
    # data units are tagged with their group/index.
    assert [u.meta["fec_index"] for u in emitted[:3]] == [0, 1, 2]
    assert all(u.meta["fec_group"] == 0 for u in emitted[:3])
    # parity payload is the XOR of the member payloads.
    assert emitted[3].payload == xor_bytes([u.payload for u in units])


def test_recover_exactly_one_missing() -> None:
    enc = XorParityFec(group=3)
    units = [
        make_packet(rtp_seq=i, pts=9, payload=bytes([i, i + 1, i + 2, i + 3]))
        for i in range(3)
    ]
    emitted = encode_group(enc, units)
    data, parity = emitted[:3], emitted[3]

    dec = XorParityDecoder()
    dec.push(data[0])
    dec.push(data[2])  # data[1] is "lost"
    feed = dec.push(parity)  # parity triggers recovery

    assert len(feed.recovered) == 1
    recovered = feed.recovered[0]
    assert feed.emit == [recovered]  # parity itself not forwarded
    assert recovered.kind is UnitKind.PACKET
    assert recovered.payload == units[1].payload  # byte-exact
    assert recovered.seq == units[1].seq
    assert recovered.pts == units[1].pts
    assert recovered.source_digest == DIGEST
    assert recovered.meta["frame_seq"] == 100  # per-member meta preserved


def test_recover_works_regardless_of_arrival_order() -> None:
    enc = XorParityFec(group=3)
    units = [
        make_packet(rtp_seq=i, pts=1, payload=bytes([9 - i]) * 3) for i in range(3)
    ]
    emitted = encode_group(enc, units)
    data, parity = emitted[:3], emitted[3]
    dec = XorParityDecoder()
    # parity arrives first, then the survivors; recovery fires once one missing.
    dec.push(parity)
    dec.push(data[0])
    feed = dec.push(data[1])  # data[2] lost; now exactly one missing
    assert len(feed.recovered) == 1
    assert feed.recovered[0].payload == units[2].payload


def test_two_missing_unrecoverable_reported() -> None:
    enc = XorParityFec(group=3)
    units = [make_packet(rtp_seq=i, pts=1, payload=bytes([i]) * 4) for i in range(3)]
    emitted = encode_group(enc, units)
    data, parity = emitted[:3], emitted[3]
    dec = XorParityDecoder()
    dec.push(data[0])  # data[1] and data[2] both lost
    feed = dec.push(parity)
    assert feed.recovered == []
    assert feed.emit == []
    report = dec.flush()
    assert len(report.reports) == 1
    assert report.reports[0]["missing"] == [1, 2]
    assert report.reports[0]["reason"] == "insufficient"


def test_lost_parity_needs_no_recovery() -> None:
    enc = XorParityFec(group=3)
    units = [make_packet(rtp_seq=i, pts=1, payload=bytes([i]) * 2) for i in range(3)]
    emitted = encode_group(enc, units)
    data = emitted[:3]  # parity emitted[3] is "lost"
    dec = XorParityDecoder()
    forwarded = []
    for d in data:
        forwarded.extend(dec.push(d).emit)
    assert [u.payload for u in forwarded] == [u.payload for u in units]
    assert dec.flush().reports == []  # all data present, nothing to report


def test_missing_data_with_no_parity_reported() -> None:
    enc = XorParityFec(group=3)
    units = [make_packet(rtp_seq=i, pts=1, payload=bytes([i])) for i in range(3)]
    emitted = encode_group(enc, units)
    data = emitted[:3]
    dec = XorParityDecoder()
    dec.push(data[0])
    dec.push(data[2])  # data[1] lost AND parity lost
    report = dec.flush()
    assert report.reports[0]["missing"] == [1]
    assert report.reports[0]["reason"] == "no-parity"


def test_partial_trailing_group_flush_emits_parity() -> None:
    enc = XorParityFec(group=4)
    units = [make_packet(rtp_seq=i, pts=1, payload=bytes([i])) for i in range(2)]
    emitted = encode_group(enc, units)  # only 2 < 4, no parity yet
    assert all(u.kind is UnitKind.PACKET for u in emitted)
    tail = enc.flush()
    assert len(tail) == 1
    assert tail[0].kind is UnitKind.PARITY
    assert tail[0].meta["size"] == 2


def test_xor_decode_stage_events() -> None:
    enc = XorParityFec(group=3)
    units = [make_packet(rtp_seq=i, pts=1, payload=bytes([i]) * 3) for i in range(3)]
    emitted = encode_group(enc, units)
    data, parity = emitted[:3], emitted[3]
    stage = FecDecodeStage(XorParityDecoder())
    events = []
    events += stage.process(data[0], NOW).events
    events += stage.process(data[2], NOW).events
    out = stage.process(parity, NOW)
    events += out.events
    assert any(e.kind is EventKind.RECOVER for e in out.events)
    assert any(e.kind is EventKind.OUT for e in events)


def test_fec_encode_stage_emits_events() -> None:
    stage = FecEncodeStage(XorParityFec(group=2))
    out = stage.process(make_packet(rtp_seq=0, pts=1, payload=b"a"), NOW)
    assert any(e.kind is EventKind.IN for e in out.events)
    out2 = stage.process(make_packet(rtp_seq=1, pts=1, payload=b"b"), NOW)
    # second push completes the group -> data + parity emitted.
    assert len(out2.emit) == 2
    assert out2.emit[-1].kind is UnitKind.PARITY


# --------------------------------------------------------------------------
# ST 2022-7 duplicate paths
# --------------------------------------------------------------------------


def test_duplicate_encoder_emits_two_paths() -> None:
    enc = DuplicatePathFec()
    out = enc.push(make_packet(rtp_seq=5, pts=1, payload=b"x"))
    assert len(out) == 2
    assert {u.meta["path"] for u in out} == {"A", "B"}
    assert all(u.seq == 5 for u in out)
    assert all(u.payload == b"x" for u in out)


def test_merger_dedups_keeping_first() -> None:
    enc = DuplicatePathFec()
    copies = enc.push(make_packet(rtp_seq=5, pts=1, payload=b"x"))
    merger = SeamlessMerger()
    first = merger.push(copies[0])
    second = merger.push(copies[1])
    assert first.emit == [copies[0]]
    assert second.emit == []
    assert second.deduped == [copies[1]]
    assert merger.flush().recovered == []  # both copies present, no recovery


def test_merger_survives_full_path_loss() -> None:
    enc = DuplicatePathFec()
    merger = SeamlessMerger()
    forwarded = []
    for seq in range(3):
        copies = enc.push(make_packet(rtp_seq=seq, pts=1, payload=bytes([seq])))
        # path A entirely lost; only path B arrives.
        path_b = next(c for c in copies if c.meta["path"] == "B")
        forwarded.extend(merger.push(path_b).emit)
    assert [u.seq for u in forwarded] == [0, 1, 2]  # nothing lost
    recovered = merger.flush().recovered
    assert {u.seq for u in recovered} == {0, 1, 2}  # each was a lone survivor


def test_merger_stage_events() -> None:
    enc = DuplicatePathFec()
    copies = enc.push(make_packet(rtp_seq=9, pts=1, payload=b"z"))
    stage = FecDecodeStage(SeamlessMerger())
    out_a = stage.process(copies[0], NOW)
    out_b = stage.process(copies[1], NOW)
    assert any(e.kind is EventKind.OUT for e in out_a.events)
    assert any(e.kind is EventKind.DROP for e in out_b.events)
    # a lone-survivor sequence surfaces as RECOVER on flush.
    lone = FecDecodeStage(SeamlessMerger())
    lone.process(copies[0], NOW)
    flush = lone.flush(NOW)
    assert any(e.kind is EventKind.RECOVER for e in flush.events)


def test_merger_does_not_drop_next_sequence_cycle_or_other_ssrc() -> None:
    merger = SeamlessMerger()
    first = make_packet(rtp_seq=5, pts=1, frame_seq=1, payload=b"a")
    next_cycle = make_packet(rtp_seq=5, pts=2, frame_seq=2, payload=b"b")
    assert merger.push(first).emit == [first]
    assert merger.push(next_cycle).emit == [next_cycle]
    other_ssrc = first.with_payload(first.payload, rtp_ssrc=9)
    assert merger.push(other_ssrc).emit == [other_ssrc]


def test_repeated_same_path_does_not_count_as_surviving_partner() -> None:
    merger = SeamlessMerger()
    unit = make_packet(rtp_seq=1, pts=1, payload=b"x").with_payload(b"x", path="A")
    merger.push(unit)
    merger.push(unit)
    assert merger.flush().recovered == [unit]


def test_merger_capacity_reports_retired_single_path_and_stays_bounded() -> None:
    merger = SeamlessMerger(max_packets=2, max_bytes=2)
    for seq in range(10):
        unit = make_packet(rtp_seq=seq, pts=seq, payload=b"x")
        feed = merger.push(unit)
        assert feed.emit == [unit]
        if seq >= 2:
            assert len(feed.recovered) == 1
        assert len(merger._first) <= 2
        assert merger._bytes <= 2
    assert len(merger.flush().recovered) == 2
    assert merger._bytes == 0


def test_malformed_xor_metadata_is_reported_without_retention() -> None:
    base = make_packet(rtp_seq=1, pts=1, payload=b"x")
    malformed = [
        base.with_payload(b"x", fec_group="bad"),
        base.with_payload(b"x", fec_group=1, fec_size=0, fec_index=0),
        base.with_payload(b"x", fec_group=1, fec_size=1025, fec_index=0),
        base.with_payload(b"x", fec_group=1, fec_size=2, fec_index=-1),
        MediaUnit(
            UnitKind.PARITY,
            0,
            0,
            b"x",
            DIGEST,
            {"group": 0, "size": 2, "members": [{}]},
        ),
        MediaUnit(
            UnitKind.PARITY,
            0,
            0,
            b"x",
            DIGEST,
            {"group": 0, "size": 1, "members": [None]},
        ),
    ]
    dec = XorParityDecoder()
    for unit in malformed:
        feed = dec.push(unit)
        assert feed.reports
        assert not feed.emit
        assert not dec._groups


def test_xor_group_capacity_and_completed_retention_are_bounded() -> None:
    dec = XorParityDecoder(max_groups=2, max_bytes=2, done_window=3)
    for group in range(10):
        data = make_packet(rtp_seq=group, pts=group, payload=b"x").with_payload(
            b"x", fec_group=group, fec_index=0, fec_size=2
        )
        feed = dec.push(data)
        assert feed.emit == [data]
        if group >= 2:
            assert feed.reports[0]["reason"] == "capacity"
        assert len(dec._groups) <= 2
        assert len(dec._done) <= 3
        assert dec._bytes <= 2
    assert len(dec.flush().reports) == 2
    assert dec._bytes == 0


def test_partial_xor_group_recovers_with_larger_advertised_data_size() -> None:
    enc = XorParityFec(4)
    first = enc.push(make_packet(rtp_seq=0, pts=1, payload=b"a"))[0]
    second = enc.push(make_packet(rtp_seq=1, pts=1, payload=b"b"))[0]
    parity = enc.flush()[0]
    dec = XorParityDecoder()
    dec.push(first)
    assert dec.push(parity).recovered == [second]
