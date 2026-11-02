"""Remote telemetry input is bounded independently of the control protocol."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from typing import Any

import pytest

from yoke.core.types import MAX_TRACKED
from yoke.telemetry.model import (
    MAX_COUNTER,
    MAX_DETAIL_LENGTH,
    DeviceSnapshot,
    Snapshot,
)


def sample() -> Snapshot:
    return Snapshot(
        uptime_seconds=12.5,
        authentication="site-psk",
        devices=(
            DeviceSnapshot(
                "screen-1",
                "Den\n\x1b TV",
                "hdmi",
                "consumer",
                "active",
                True,
                0.5,
                "camera-1",
                "Backend ready\u202e",
                3,
                600,
                {"media-up": 1},
            ),
        ),
        observer={"ok": 3, "bad-sig": 1},
        host=host_sample(),
    )


def host_sample() -> dict[str, Any]:
    return {
        "scope": "collector-host",
        "node_id": "warden-1",
        "cpu": {"busy_ratio": 0.25, "logical_count": 4, "load1": 0.5},
        "memory": {"total_bytes": 1000, "available_bytes": 400, "used_ratio": 0.6},
        "swap": {"total_bytes": 0, "free_bytes": 0, "used_ratio": None},
        "network": [
            {
                "interface": "eth0",
                "rx_bytes": 1200,
                "tx_bytes": 200,
                "rx_errors": 1,
                "tx_errors": 2,
                "rx_dropped": 3,
                "tx_dropped": 4,
                "rx_bytes_per_second": 100.0,
                "tx_bytes_per_second": None,
            }
        ],
        "temperatures": [{"sensor": "thermal_zone0:cpu", "celsius": 40.0}],
        "uptime_seconds": 90.0,
    }


def test_round_trip_preserves_unicode_and_detaches_data() -> None:
    initial = sample()
    encoded = initial.to_json()
    body = json.loads(encoded)
    assert body["schema_version"] == 1
    assert body["sample_interval_seconds"] == 1.0
    assert body["devices"][0]["name"] == "Den\n\x1b TV"
    assert Snapshot.from_body(body) == initial
    assert encoded == initial.to_json()
    body["devices"][0]["events"]["media-up"] = 900
    body["host"]["network"][0]["rx_bytes"] = 900
    assert initial.devices[0].events == {"media-up": 1}
    assert initial.host is not None
    assert initial.host["network"][0]["rx_bytes"] == 1200
    with pytest.raises(FrozenInstanceError):
        initial.uptime_seconds = 2.0  # type: ignore[misc]


def test_bounded_extensions_are_ignored_at_each_level() -> None:
    body = sample().to_body()
    body["future"] = {"nested": [1, "ok", True, None]}
    body["devices"][0]["future"] = [False]
    body["devices"][0]["events"]["new-event"] = 42
    body["observer"]["future"] = 42
    body["host"]["future"] = "ok"
    body["host"]["network"][0]["future"] = "ok"
    assert Snapshot.from_body(body) == sample()


@pytest.mark.parametrize("authentication", ["demo", "open-unauthenticated", "site-psk"])
def test_authentication_modes(authentication: str) -> None:
    body = sample().to_body()
    body["authentication"] = authentication
    assert Snapshot.from_body(body).authentication == authentication


@pytest.mark.parametrize("interval", [0.2, 1.0, 60.0])
def test_sample_cadence_round_trip(interval: float) -> None:
    snapshot = replace(sample(), sample_interval_seconds=interval)
    body = json.loads(snapshot.to_json())
    assert body["sample_interval_seconds"] == interval
    assert Snapshot.from_body(body) == snapshot


def test_older_snapshot_cadence_defaults_conservatively() -> None:
    original = sample()
    body = original.to_body()
    del body["sample_interval_seconds"]
    assert Snapshot.from_body(body) == replace(original, sample_interval_seconds=60.0)
    # Existing local constructors retain their original positional arguments.
    assert Snapshot(0.0, "site-psk", (), {}, None).sample_interval_seconds == 1.0


@pytest.mark.parametrize(
    "interval",
    [True, False, None, "1.0", 0.199, 60.001, float("nan"), float("inf")],
)
def test_invalid_sample_cadence_is_rejected(interval: Any) -> None:
    body = sample().to_body()
    body["sample_interval_seconds"] = interval
    with pytest.raises(ValueError):
        Snapshot.from_body(body)
    with pytest.raises(ValueError):
        replace(sample(), sample_interval_seconds=interval)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("schema_version", True),
        ("schema_version", 2),
        ("uptime_seconds", True),
        ("uptime_seconds", -1),
        ("uptime_seconds", float("nan")),
        ("uptime_seconds", float("inf")),
        ("authentication", "verified"),
        ("devices", {}),
        ("observer", []),
    ],
)
def test_invalid_snapshot_fields(key: str, value: Any) -> None:
    body = sample().to_body()
    body[key] = value
    with pytest.raises(ValueError):
        Snapshot.from_body(body)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("node_id", "bad\nnode"),
        ("name", "x" * 65),
        ("kind", "ir"),
        ("role", "controller"),
        ("state", "ready"),
        ("state", []),
        ("live", 1),
        ("last_seen_age_seconds", float("inf")),
        ("pairing_producer", ""),
        ("driver_detail", "x" * (MAX_DETAIL_LENGTH + 1)),
        ("control_frames", True),
        ("control_frames", -1),
        ("control_bytes", MAX_COUNTER + 1),
        ("events", {"media-up": 1.0}),
        ("events", {"media-up": True}),
    ],
)
def test_invalid_device_fields(key: str, value: Any) -> None:
    body = sample().to_body()
    body["devices"][0][key] = value
    with pytest.raises(ValueError):
        Snapshot.from_body(body)


def test_device_count_and_unique_ids() -> None:
    body = sample().to_body()
    body["devices"] *= 2
    with pytest.raises(ValueError, match="duplicate"):
        Snapshot.from_body(body)
    body["devices"] = [{}] * (MAX_TRACKED + 1)
    with pytest.raises(ValueError):
        Snapshot.from_body(body)


def test_maximum_retained_devices_round_trip() -> None:
    body = sample().to_body()
    device = body["devices"][0]
    body["devices"] = [dict(device, node_id=f"device-{i}") for i in range(MAX_TRACKED)]
    parsed = Snapshot.from_body(body)
    assert len(parsed.devices) == MAX_TRACKED
    assert len(parsed.to_json()) < 8 * 1024 * 1024


@pytest.mark.parametrize(
    "extension",
    [
        {"large": "x" * 4097},
        {"bad-unicode": "\ud800"},
        {"nested": [[[[[[[[[1]]]]]]]]]},
        {"huge": MAX_COUNTER + 1},
        {"bad-float": float("nan")},
        {"invalid": object()},
        dict.fromkeys(range(65)),
    ],
)
def test_unknown_extensions_still_have_work_and_data_bounds(extension: Any) -> None:
    body = sample().to_body()
    body["future"] = extension
    with pytest.raises(ValueError):
        Snapshot.from_body(body)


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("cpu", "busy_ratio", 1.1),
        ("cpu", "busy_ratio", True),
        ("cpu", "logical_count", 0),
        ("cpu", "logical_count", True),
        ("memory", "available_bytes", 1001),
        ("memory", "used_ratio", None),
        ("swap", "used_ratio", -0.1),
    ],
)
def test_host_measurements_are_validated(section: str, key: str, value: Any) -> None:
    body = sample().to_body()
    body["host"][section][key] = value
    with pytest.raises(ValueError):
        Snapshot.from_body(body)


def test_host_unknowns_and_unavailable_metrics() -> None:
    body = sample().to_body()
    body["host"] = {
        "scope": "collector-host",
        "node_id": None,
        "cpu": {"busy_ratio": None, "logical_count": None, "load1": None},
        "memory": None,
        "swap": None,
        "network": [],
        "temperatures": [],
        "uptime_seconds": None,
    }
    parsed = Snapshot.from_body(body)
    assert parsed.host == body["host"]
    body["host"]["scope"] = "appliance"
    with pytest.raises(ValueError, match="scope"):
        Snapshot.from_body(body)


@pytest.mark.parametrize("field", ["network", "temperatures"])
def test_host_labels_are_bounded_and_unique(field: str) -> None:
    body = sample().to_body()
    body["host"][field] *= 2
    with pytest.raises(ValueError, match="duplicate"):
        Snapshot.from_body(body)
    body = sample().to_body()
    label = "interface" if field == "network" else "sensor"
    body["host"][field][0][label] = "bad\nlabel"
    with pytest.raises(ValueError):
        Snapshot.from_body(body)
    body = sample().to_body()
    body["host"][field] = deepcopy(body["host"][field]) * 65
    with pytest.raises(ValueError):
        Snapshot.from_body(body)
