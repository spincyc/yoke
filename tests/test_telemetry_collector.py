"""Accepted control observations alone produce bounded device telemetry."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from tests.test_snoop import PSK, WRONG_PSK, ann_consumer, ann_producer, intent
from yoke.core.frames import encode
from yoke.core.types import PEER_FORGET, PEER_TIMEOUT, Body, FrameType
from yoke.telemetry.collector import TelemetryCollector
from yoke.telemetry.model import EVENTS, MAX_COUNTER, Snapshot


def event(origin: str, seq: int, name: str, *, psk: bytes = PSK) -> bytes:
    return encode(
        FrameType.EVT, origin, seq, 1000.0, {"event": name, "detail": {}}, psk
    )


def test_first_announce_and_accepted_frames_count_encoded_bytes() -> None:
    collector = TelemetryCollector(PSK, now=100.0, wall_time=1000.0)
    announcement = ann_consumer(producer="camera-1", version=(1, "cli-1"))
    evt = event("tv-den", 2, "media-up")
    assert collector.observe(announcement, 100.0)[0].verdict == "ok"
    collector.observe(evt, 101.0)
    sample = collector.snapshot(102.0)
    device = sample.devices[0]
    assert sample.authentication == "site-psk"
    assert sample.uptime_seconds == 2
    assert device.control_frames == 2
    assert device.control_bytes == len(announcement) + len(evt)
    assert device.events == {"media-up": 1}
    assert device.pairing_producer == "camera-1"
    assert device.last_seen_age_seconds == 2
    assert sample.host is None


def test_unauthenticated_duplicate_stale_and_malformed_do_not_mutate_devices() -> None:
    collector = TelemetryCollector(PSK, now=0.0, wall_time=1000.0)
    raw = ann_producer(seq=10)
    collector.observe(raw, 0)
    collector.observe(raw, 1)
    collector.observe(ann_producer(seq=9, state="error"), 2)
    collector.observe(ann_producer(seq=100, psk=WRONG_PSK, state="error"), 3)
    collector.observe(ann_producer(seq=11, state="incorrect"), 4)
    collector.observe(b"malformed", 5)
    sample = collector.snapshot(5)
    assert sample.devices[0].state == "active"
    assert sample.devices[0].control_frames == 1
    assert sample.devices[0].last_seen_age_seconds == 5
    assert sample.observer["dup"] == 1
    assert sample.observer["stale"] == 1
    assert sample.observer["bad-sig"] == 1
    assert sample.observer["malformed"] == 2


def test_unannounced_origins_do_not_create_device_series_or_backfill() -> None:
    collector = TelemetryCollector(PSK, now=0, wall_time=1000)
    collector.observe(intent(), 0)
    collector.observe(event("cam-1", 1, "started"), 0)
    assert collector.snapshot(0).devices == ()
    collector.observe(ann_producer(seq=2), 1)
    sample = collector.snapshot(1)
    assert len(sample.devices) == 1
    assert sample.devices[0].control_frames == 1
    assert sample.devices[0].events == {}
    assert sample.observer["ok"] == 3


def test_liveness_expires_in_silence_without_fabricating_new_state() -> None:
    collector = TelemetryCollector(PSK, now=0, wall_time=1000)
    collector.observe(ann_producer(), 0)
    assert collector.snapshot(PEER_TIMEOUT).devices[0].live
    expired = collector.snapshot(PEER_TIMEOUT + 0.1).devices[0]
    assert not expired.live
    assert expired.state == "active"
    collector.observe(event("cam-1", 2, "driver-retry"), 10)
    still_expired = collector.snapshot(10).devices[0]
    assert not still_expired.live
    assert still_expired.last_seen_age_seconds == 10
    assert still_expired.control_frames == 2
    assert collector.snapshot(PEER_FORGET + 0.1).devices == ()


@pytest.mark.parametrize("sample_before_return", [False, True])
def test_peer_eviction_resets_counters(sample_before_return: bool) -> None:
    collector = TelemetryCollector(PSK, now=0, wall_time=1000)
    collector.observe(ann_producer(), 0)
    collector.observe(event("cam-1", 2, "media-up"), 0)
    if sample_before_return:
        assert collector.snapshot(PEER_FORGET + 0.1).devices == ()
    collector.observe(ann_producer(), PEER_FORGET + 1)
    device = collector.snapshot(PEER_FORGET + 1).devices[0]
    assert device.control_frames == 1
    assert device.events == {}


def test_bye_and_fifo_eviction_remove_counters() -> None:
    collector = TelemetryCollector(PSK, now=0, wall_time=1000)
    with patch("yoke.snoop.MAX_TRACKED", 2):
        collector.observe(ann_producer("cam-1"), 0)
        collector.observe(ann_producer("cam-2"), 0)
        collector.observe(ann_producer("cam-3"), 0)
        sample = collector.snapshot(0)
        assert [device.node_id for device in sample.devices] == ["cam-2", "cam-3"]
        assert len(collector._counters) == 2
        collector.observe(encode(FrameType.BYE, "cam-2", 2, 1000.0, {}, PSK), 1)
        assert [device.node_id for device in collector.snapshot(1).devices] == ["cam-3"]
        collector.observe(ann_producer("cam-2", seq=3), 2)
        assert collector.snapshot(2).devices[0].control_frames == 1


def test_unknown_events_share_a_single_other_bucket() -> None:
    collector = TelemetryCollector(PSK, now=0, wall_time=1000)
    collector.observe(ann_producer(), 0)
    for seq in range(2, 102):
        collector.observe(event("cam-1", seq, f"extension-{seq}"), 0)
    sample = collector.snapshot(0)
    assert sample.devices[0].events == {"other": 100}
    assert len(sample.devices[0].events) <= len(EVENTS)


def test_driver_evidence_is_diagnostic_text_only_and_does_not_inspect_media() -> None:
    collector = TelemetryCollector(PSK, now=0, wall_time=1000)
    body: Body = {
        "role": "producer",
        "kind": "hdmi",
        "name": "Name\x1b",
        "state": "error",
        "err": None,
        "media": {"opaque-driver-description": "not-measured"},
        "driver_status": {"detail": "Backend selected channel\u202e"},
    }
    collector.observe(encode(FrameType.ANN, "cam-1", 1, 1000, body, PSK), 0)
    sample = collector.snapshot(0)
    assert sample.devices[0].driver_detail == "Backend selected channel\u202e"
    assert sample.devices[0].name == "Name\x1b"
    assert "opaque-driver-description" not in str(sample.to_body())
    body["err"] = "failure\n"
    body["pairing"] = {"producer": "invalid-for-producer!"}
    collector.observe(encode(FrameType.ANN, "cam-1", 2, 1000, body, PSK), 1)
    failed = collector.snapshot(1)
    assert failed.devices[0].driver_detail == "failure\n"
    assert failed.devices[0].pairing_producer is None
    assert Snapshot.from_body(failed.to_body()) == failed


def test_open_mode_and_no_per_packet_snapshot_serialization() -> None:
    collector = TelemetryCollector(b"", now=0, wall_time=1000)
    with patch.object(Snapshot, "to_body", side_effect=AssertionError("serialized")):
        collector.observe(ann_producer(psk=b""), 0)
    assert collector.snapshot(0).authentication == "open-unauthenticated"


def test_counter_saturation() -> None:
    collector = TelemetryCollector(PSK, now=0, wall_time=1000)
    collector.observe(ann_producer(), 0)
    counter = collector._counters["cam-1"]
    counter.frames = MAX_COUNTER
    counter.bytes = MAX_COUNTER
    counter.events["media-up"] = MAX_COUNTER
    collector.observe(event("cam-1", 2, "media-up"), 0)
    sample = collector.snapshot(0)
    assert sample.devices[0].control_frames == MAX_COUNTER
    assert sample.devices[0].control_bytes == MAX_COUNTER
    assert sample.devices[0].events["media-up"] == MAX_COUNTER


@pytest.mark.parametrize("now", [float("nan"), float("inf"), 1e308, True, -1])
def test_invalid_or_backwards_clocks_fail(now: float) -> None:
    collector = TelemetryCollector(PSK, now=0, wall_time=1000)
    with pytest.raises(ValueError):
        collector.snapshot(now)


def test_collector_uses_each_datagrams_wall_reading() -> None:
    # ADR 0023: telemetry/runtime.py passes time.time() per datagram.
    now, wall = 1000.0, 1_755_000_000.0
    collector = TelemetryCollector(PSK, now=now, wall_time=wall - 7200)
    raw = ann_producer(seq=int(wall) * 1_000_000_000 + 1)
    assert collector.observe(raw, now, wall=wall - 7200)[0].verdict == "stale"
    raw = ann_producer(seq=int(wall) * 1_000_000_000 + 2)
    assert collector.observe(raw, now + 1, wall=wall + 1)[0].verdict == "ok"
