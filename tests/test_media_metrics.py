"""Tests for ``yoke.media.metrics`` — the white-box health aggregator.

Fully deterministic and sans-I/O: ``StageEvent`` taps are built directly
and fed into ``MetricsCollector.observe`` with explicit timestamps. No
sockets, no sleeps, no clock.
"""

from __future__ import annotations

import json

from yoke.media.metrics import DEFAULT_STREAM, MetricsCollector
from yoke.media.types import EventKind, StageEvent

S0 = "s0"


def ev(
    stage: str,
    kind: EventKind,
    seq: int = 0,
    pts: int = 0,
    now: float = 0.0,
    **detail: object,
) -> StageEvent:
    return StageEvent(stage=stage, kind=kind, seq=seq, pts=pts, now=now, detail=detail)


def _synthetic_stream() -> list[StageEvent]:
    """source -> codec -> packetize -> fec -> jitter -> sink, one stream,
    two frames, 10 packets sent / 8 received, 1 FEC recovery, 1 ok + 1 bad
    frame at the sink."""
    events: list[StageEvent] = []
    # source: two frames, 10000 uncompressed bytes each.
    events.append(ev("source", EventKind.OUT, 0, 0, 0.0, stream=S0, frame_bytes=10000))
    events.append(
        ev("source", EventKind.OUT, 1, 3000, 0.033, stream=S0, frame_bytes=10000)
    )
    # codec: 4 slices/frame, each reporting the applied ratio 2.5 (as the
    # real CodecEncodeStage does in its OUT detail).
    for i in range(4):
        events.append(
            ev("codec", EventKind.OUT, i, 0, 0.001, stream=S0, bytes=1000, ratio=2.5)
        )
    for i in range(4):
        events.append(
            ev("codec", EventKind.OUT, i, 3000, 0.034, stream=S0, bytes=1000, ratio=2.5)
        )
    # packetize: 10 packets.
    for i in range(10):
        events.append(ev("packetize", EventKind.OUT, i, 0, 0.002, stream=S0, bytes=200))
    # fec (wire egress on the sender): 10 packets launched + 1 recovery.
    for i in range(10):
        events.append(ev("fec", EventKind.OUT, i, 0, 0.003, stream=S0, bytes=200))
    events.append(ev("fec", EventKind.RECOVER, 5, 0, 0.040, stream=S0))
    # receiver: 8 packets survive jitter (HOLD) through to reassembly
    # (depacketize IN); 1 arrives late (jitter LATE, never reassembled).
    for i in range(8):
        events.append(ev("jitter", EventKind.HOLD, i, 0, 0.041, stream=S0))
        events.append(ev("depacketize", EventKind.IN, i, 0, 0.042, stream=S0))
    events.append(ev("jitter", EventKind.LATE, 9, 0, 0.045, stream=S0))
    # ratecontrol: target ratio adjustment.
    events.append(
        ev("ratecontrol", EventKind.ADJUST, 0, 0, 0.046, stream=S0, target=2.5)
    )
    # sink: frame 0 ok (derived latency), frame 1 bad (explicit latency).
    events.append(ev("sink", EventKind.INTEGRITY, 0, 0, 0.050, stream=S0, ok=True))
    events.append(
        ev(
            "sink",
            EventKind.INTEGRITY,
            1,
            3000,
            0.090,
            stream=S0,
            ok=False,
            latency=0.042,
        )
    )
    events.append(ev("sink", EventKind.STAT, 0, 0, 0.091, stream=S0, buffered=3))
    return events


def _collect(events: list[StageEvent]) -> MetricsCollector:
    collector = MetricsCollector()
    for event in events:
        collector.observe(event)
    return collector


def test_snapshot_is_json_serializable_and_sorted() -> None:
    snap = _collect(_synthetic_stream()).snapshot()
    line = json.dumps(snap, sort_keys=True, separators=(",", ":"))
    assert json.loads(line) == snap
    assert set(snap) == {"stages", "streams", "totals"}


def test_end_to_end_stream_aggregates() -> None:
    stream = _collect(_synthetic_stream()).snapshot()["streams"][S0]
    assert stream["frames_sourced"] == 2
    assert stream["frames_ok"] == 1
    assert stream["frames_bad"] == 1
    assert stream["packets_sent"] == 10
    assert stream["packets_recv"] == 8
    assert stream["packet_loss"] == 2
    assert stream["packet_loss_rate"] == 0.2


def test_effective_ratio_from_frame_bytes() -> None:
    stream = _collect(_synthetic_stream()).snapshot()["streams"][S0]
    # 20000 uncompressed / 8000 compressed.
    assert stream["ratio"] == 2.5
    assert stream["target_ratio"] == 2.5


def test_fec_recovery_counted_with_rate() -> None:
    stream = _collect(_synthetic_stream()).snapshot()["streams"][S0]
    assert stream["fec_recovered"] == 1
    # 1 recovered out of (1 recovered + 2 lost).
    assert stream["fec_recovery_rate"] == round(1 / 3, 6)


def test_latency_derived_and_explicit() -> None:
    stream = _collect(_synthetic_stream()).snapshot()["streams"][S0]
    latency = stream["latency"]
    assert latency == {"samples": 2, "last": 0.042, "mean": 0.046, "max": 0.05}


def test_per_stage_counters() -> None:
    collector = _collect(_synthetic_stream())
    assert collector.stage_report("source")["out"] == 2
    assert collector.stage_report("codec")["out"] == 8
    assert collector.stage_report("packetize")["out"] == 10
    fec = collector.stage_report("fec")
    assert fec["out"] == 10
    assert fec["recover"] == 1
    jitter = collector.stage_report("jitter")
    assert jitter["hold"] == 8
    assert jitter["late"] == 1
    assert collector.stage_report("depacketize")["in"] == 8
    assert collector.stage_report("ratecontrol")["adjust"] == 1
    sink = collector.stage_report("sink")
    assert sink["integrity_ok"] == 1
    assert sink["integrity_bad"] == 1
    assert sink["stat"]["buffered"] == 3


def test_totals_across_streams() -> None:
    totals = _collect(_synthetic_stream()).snapshot()["totals"]
    assert totals == {
        "frames_sourced": 2,
        "frames_ok": 1,
        "frames_bad": 1,
        "packets_sent": 10,
        "packets_recv": 8,
        "packet_loss": 2,
        "fec_recovered": 1,
    }


def test_ratio_none_without_codec_events() -> None:
    collector = MetricsCollector()
    collector.observe(
        ev("source", EventKind.OUT, 0, 0, 0.0, stream=S0, frame_bytes=100)
    )
    assert collector.snapshot()["streams"][S0]["ratio"] is None


def test_untagged_events_fall_into_default_stream() -> None:
    collector = MetricsCollector()
    collector.observe(ev("source", EventKind.OUT, 0, 0, 0.0, frame_bytes=100))
    assert DEFAULT_STREAM in collector.snapshot()["streams"]


def test_missing_stage_report_is_zeroed() -> None:
    report = MetricsCollector().stage_report("nope")
    assert report["in"] == 0 and report["out"] == 0
    assert "stat" not in report
