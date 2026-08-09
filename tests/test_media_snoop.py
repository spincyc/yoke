"""Tests for ``yoke.media.snoop`` — the passive media verifier.

Deterministic and sans-I/O: ``StageEvent`` taps and ``PACKET`` MediaUnits
are built directly and fed into the snooper with explicit timestamps. The
black-box RTP header decoder is a small fake injected at construction
(production injects ``yoke.media.stages.packetize.decode_rtp_header``).
"""

from __future__ import annotations

import json

from yoke.media.snoop import MediaSnooper, SnoopRecord, render_json, render_text
from yoke.media.types import EventKind, MediaUnit, StageEvent, UnitKind

# -- a fake wire format + injected header decoder ---------------------------


def make_packet(
    seq: int,
    ts: int = 0,
    *,
    marker: bool = False,
    ssrc: int = 0xDEAD,
    pt: int = 96,
    payload_len: int = 100,
) -> bytes:
    """Encode a fake RTP-ish packet: 2-byte header length, JSON header,
    then an opaque payload of the requested length."""
    head = json.dumps(
        {"seq": seq, "timestamp": ts, "marker": marker, "pt": pt, "ssrc": ssrc}
    ).encode()
    return len(head).to_bytes(2, "big") + head + (b"\x00" * payload_len)


def fake_decoder(raw: bytes) -> dict[str, object]:
    """The injected ``Callable[[bytes], Body]`` (fake ``decode_rtp_header``)."""
    if len(raw) < 2:
        raise ValueError("runt")
    n = int.from_bytes(raw[:2], "big")
    if len(raw) < 2 + n:
        raise ValueError("truncated header")
    obj = json.loads(raw[2 : 2 + n].decode())
    if not isinstance(obj, dict) or "seq" not in obj:
        raise ValueError("no seq")
    return obj


SSRC = 0xDEAD
STREAM = f"{SSRC:08x}"


def _snooper() -> MediaSnooper:
    return MediaSnooper(fake_decoder)


def ev(
    stage: str,
    kind: EventKind,
    seq: int = 0,
    pts: int = 0,
    **detail: object,
) -> StageEvent:
    return StageEvent(stage=stage, kind=kind, seq=seq, pts=pts, now=0.0, detail=detail)


# -- white-box ---------------------------------------------------------------


def test_white_box_reconstructs_stage_path() -> None:
    snooper = _snooper()
    stages = ["source", "codec", "packetize", "fec", "jitter", "sink"]
    for t, stage in enumerate(stages):
        rec = snooper.observe_event(
            ev(stage, EventKind.OUT, seq=1, stream="s0"), now=float(t)
        )
        assert isinstance(rec, SnoopRecord)
        assert rec.verdict == "ok"
        assert rec.stage == stage
    topo = snooper.topology()
    assert topo["streams"]["s0"]["stages"] == stages


def test_white_box_drop_late_recover_integrity_verdicts() -> None:
    snooper = _snooper()
    assert (
        snooper.observe_event(ev("jitter", EventKind.DROP, stream="s0"), 0.0).verdict
        == "loss"
    )
    assert (
        snooper.observe_event(ev("jitter", EventKind.LATE, stream="s0"), 0.1).verdict
        == "loss"
    )
    assert (
        snooper.observe_event(ev("fec", EventKind.RECOVER, stream="s0"), 0.2).verdict
        == "recovered"
    )
    assert (
        snooper.observe_event(
            ev("sink", EventKind.INTEGRITY, stream="s0", ok=True), 0.3
        ).verdict
        == "ok"
    )
    assert (
        snooper.observe_event(
            ev("sink", EventKind.INTEGRITY, stream="s0", ok=False), 0.4
        ).verdict
        == "integrity-fail"
    )
    assert snooper.counts["loss"] == 2
    assert snooper.counts["recovered"] == 1
    assert snooper.counts["integrity-fail"] == 1


def test_white_box_feeds_internal_metrics() -> None:
    snooper = _snooper()
    snooper.observe_event(
        ev("source", EventKind.OUT, stream="s0", frame_bytes=100), 0.0
    )
    snap = snooper.metrics.snapshot()
    assert snap["streams"]["s0"]["frames_sourced"] == 1


# -- black-box ---------------------------------------------------------------


def test_black_box_detects_loss_from_seq_gap() -> None:
    snooper = _snooper()
    verdicts = [
        snooper.observe_packet(make_packet(seq), now=float(i))
        for i, seq in enumerate([100, 101, 103, 104])
    ]
    assert [r.verdict for r in verdicts] == ["ok", "ok", "loss", "ok"]
    assert snooper.topology()["streams"][STREAM]["wire"]["loss"] == 1


def test_black_box_detects_reorder_fills_gap() -> None:
    snooper = _snooper()
    seqs = [100, 102, 101]
    verdicts = [
        snooper.observe_packet(make_packet(seq), now=float(i))
        for i, seq in enumerate(seqs)
    ]
    assert [r.verdict for r in verdicts] == ["ok", "loss", "reorder"]
    # The late seq 101 filled the gap, so net loss is back to zero.
    assert snooper.topology()["streams"][STREAM]["wire"]["loss"] == 0


def test_black_box_detects_dup() -> None:
    snooper = _snooper()
    assert snooper.observe_packet(make_packet(100), 0.0).verdict == "ok"
    assert snooper.observe_packet(make_packet(100), 1.0).verdict == "dup"
    assert snooper.topology()["streams"][STREAM]["wire"]["dups"] == 1


def test_black_box_counts_frames_from_timestamp_and_marker() -> None:
    snooper = _snooper()
    # Two frames: ts 0 (2 packets, marker on last) then ts 3000 (1 packet).
    snooper.observe_packet(make_packet(1, ts=0), 0.0)
    snooper.observe_packet(make_packet(2, ts=0, marker=True), 0.01)
    snooper.observe_packet(make_packet(3, ts=3000, marker=True), 0.02)
    wire = snooper.topology()["streams"][STREAM]["wire"]
    assert wire["frames"] == 2
    assert wire["marker_frames"] == 2


def test_black_box_estimates_bitrate() -> None:
    snooper = _snooper()
    snooper.observe_packet(make_packet(1, payload_len=100), now=0.0)
    last = snooper.observe_packet(make_packet(2, payload_len=100), now=1.0)
    # Two packets, header+100B payload each, over 1 second.
    total_bytes = last.detail["bytes"]  # per-packet size
    assert isinstance(total_bytes, int)
    bitrate = snooper.topology()["streams"][STREAM]["wire"]["bitrate_bps"]
    assert bitrate > 0
    # bytes*8 / elapsed, elapsed = 1.0s.
    assert bitrate == snooper._wire[STREAM].bytes * 8 / 1.0


def test_black_box_malformed_packet() -> None:
    snooper = _snooper()
    rec = snooper.observe_packet(b"\x00", now=0.0)
    assert rec.verdict == "malformed"
    assert rec.stage == "wire"
    assert rec.seq == -1


def test_black_box_accepts_media_unit_payload() -> None:
    snooper = _snooper()
    unit = MediaUnit(
        kind=UnitKind.PACKET,
        seq=100,
        pts=0,
        payload=make_packet(100),
        source_digest="d",
    )
    rec = snooper.observe_packet(unit, now=0.0)
    assert rec.verdict == "ok"
    assert rec.stream_id == STREAM


# -- topology ----------------------------------------------------------------


def test_topology_matches_injected_ground_truth() -> None:
    snooper = _snooper()
    # White-box: a frame traverses the whole pipeline.
    for stage in ["source", "codec", "fec", "jitter", "sink"]:
        snooper.observe_event(ev(stage, EventKind.OUT, seq=1, stream=STREAM), now=0.0)
    snooper.observe_event(
        ev("sink", EventKind.INTEGRITY, seq=1, stream=STREAM, ok=True), now=0.1
    )
    # Black-box: three packets on the wire, one lost (102).
    for i, seq in enumerate([100, 101, 103]):
        snooper.observe_packet(make_packet(seq, ssrc=SSRC), now=float(i))
    entry = snooper.topology()["streams"][STREAM]
    assert entry["stages"] == ["source", "codec", "fec", "jitter", "sink"]
    assert entry["frames_ok"] == 1
    assert entry["wire"]["packets"] == 3
    assert entry["wire"]["loss"] == 1


# -- oracle property: white-box and black-box agree on loss -----------------


def test_white_box_and_black_box_agree_on_loss() -> None:
    """The conformance-oracle invariant: the same stream measured two
    independent ways (in-process taps vs wire packets) reports the same
    packet loss."""
    snooper = _snooper()
    # White-box: 10 packets launched (fec OUT), 8 received (jitter IN).
    for i in range(10):
        snooper.observe_event(ev("fec", EventKind.OUT, seq=i, stream=STREAM), now=0.0)
    for i in range(8):
        snooper.observe_event(ev("jitter", EventKind.IN, seq=i, stream=STREAM), now=0.0)
    # Black-box: 8 of 10 contiguous seqs arrive (3 and 7 lost).
    arrived = [s for s in range(10) if s not in (3, 7)]
    for i, seq in enumerate(arrived):
        snooper.observe_packet(make_packet(seq, ssrc=SSRC), now=float(i))
    entry = snooper.topology()["streams"][STREAM]
    assert entry["packet_loss"] == 2  # white-box: sent - received
    assert entry["wire"]["loss"] == 2  # black-box: seq gaps
    assert entry["packet_loss"] == entry["wire"]["loss"]


# -- rendering ---------------------------------------------------------------


def test_render_text_is_one_line() -> None:
    snooper = _snooper()
    snooper.observe_packet(make_packet(99), now=0.0)  # anchors relative time
    rec = snooper.observe_packet(make_packet(100), now=5.25)
    line = render_text(rec)
    assert "\n" not in line
    assert STREAM in line
    assert "wire" in line
    assert "[ok]" in line
    assert "+   5.250s" in line


def test_render_json_single_line_sorted() -> None:
    snooper = _snooper()
    records = [
        snooper.observe_event(
            ev("sink", EventKind.INTEGRITY, stream="s0", ok=True), 0.0
        ),
        snooper.observe_packet(make_packet(100), 1.0),
        snooper.observe_packet(b"\x00", 2.0),
    ]
    for rec in records:
        line = render_json(rec)
        assert "\n" not in line
        obj = json.loads(line)
        assert line == json.dumps(obj, sort_keys=True, separators=(",", ":"))
        assert obj["verdict"] == rec.verdict
        assert obj["stream"] == rec.stream_id


# -- non-interference: never mutates or returns a unit -----------------------


def test_observing_never_mutates_or_returns_units() -> None:
    snooper = _snooper()
    unit = MediaUnit(
        kind=UnitKind.PACKET,
        seq=100,
        pts=0,
        payload=make_packet(100),
        source_digest="d",
        meta={"k": "v"},
    )
    before = (unit.kind, unit.seq, unit.pts, unit.payload, unit.source_digest)
    before_meta = dict(unit.meta)
    results: list[object] = []
    for i in range(5):
        results.append(snooper.observe_packet(unit, now=float(i)))
        results.append(
            snooper.observe_event(
                ev("codec", EventKind.OUT, seq=1, stream="s0"), float(i)
            )
        )
    # Nothing returned is a MediaUnit.
    assert all(isinstance(r, SnoopRecord) for r in results)
    assert not any(isinstance(r, MediaUnit) for r in results)
    # The observed unit is untouched.
    assert (unit.kind, unit.seq, unit.pts, unit.payload, unit.source_digest) == before
    assert unit.meta == before_meta


def test_snooper_has_no_send_path() -> None:
    snooper = _snooper()
    for name in ("send", "sendto", "socket", "sock", "transmit", "write", "emit"):
        assert not hasattr(snooper, name)


def test_black_box_without_decoder_raises() -> None:
    snooper = MediaSnooper()  # no decoder injected
    try:
        snooper.observe_packet(make_packet(1), now=0.0)
    except RuntimeError:
        return
    raise AssertionError("expected RuntimeError without a header decoder")
