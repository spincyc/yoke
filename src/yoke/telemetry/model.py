"""Bounded, versioned telemetry data shared by HTTP and terminal clients.

Device counters describe retained observations, rather than device lifetime
totals. Host measurements always describe the collector's host. Parsing is
strict about known fields and ignores bounded unknown fields for compatibility.
"""

from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any

from yoke.core.types import MAX_TRACKED, NODE_ID_RE, Body
from yoke.snoop import VERDICTS

SCHEMA_VERSION = 1
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
MAX_COUNTER = (1 << 64) - 1
MAX_SECONDS = 1e12
MAX_DETAIL_LENGTH = 1024
MAX_INTERFACES = 64
MAX_TEMPERATURES = 16
EVENTS = (
    "pairing-applied",
    "media-up",
    "media-pending",
    "media-down",
    "driver-error",
    "driver-retry",
    "peer-lost",
    "peer-returned",
    "rx-drop",
    "started",
    "stopping",
    "other",
)
STATES = ("idle", "pending", "active", "error")
AUTHENTICATION = ("site-psk", "open-unauthenticated", "demo")
_INTERFACE_RE = re.compile(r"[a-zA-Z0-9_.:-]{1,15}\Z")
_SENSOR_RE = re.compile(r"[a-zA-Z0-9_.:-]{1,96}\Z")


@dataclass(frozen=True)
class DeviceSnapshot:
    """The last accepted announcement plus observation-relative counters."""

    node_id: str
    name: str
    kind: str
    role: str
    state: str
    live: bool
    last_seen_age_seconds: float
    pairing_producer: str | None
    driver_detail: str | None
    control_frames: int
    control_bytes: int
    events: dict[str, int]


@dataclass(frozen=True)
class Snapshot:
    """One coherent collection sample; no clock or source I/O is performed."""

    uptime_seconds: float
    authentication: str
    devices: tuple[DeviceSnapshot, ...]
    observer: dict[str, int]
    host: Body | None = None
    sample_interval_seconds: float = 1.0

    def __post_init__(self) -> None:
        _number(self.sample_interval_seconds, "sample_interval_seconds", 60.0, 0.2)

    def to_body(self) -> Body:
        """Return a detached JSON-compatible mapping."""
        return {
            "schema_version": SCHEMA_VERSION,
            "uptime_seconds": self.uptime_seconds,
            "authentication": self.authentication,
            "devices": [asdict(device) for device in self.devices],
            "observer": dict(self.observer),
            "host": deepcopy(self.host),
            "sample_interval_seconds": self.sample_interval_seconds,
        }

    def to_json(self) -> bytes:
        """Encode a deterministic UTF-8 snapshot without nonfinite values."""
        encoded = json.dumps(
            self.to_body(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        if len(encoded) > MAX_SNAPSHOT_BYTES:
            raise ValueError("telemetry snapshot exceeds response size limit")
        return encoded

    @classmethod
    def from_body(cls, value: object) -> Snapshot:
        """Validate an untrusted remote sample, including bounded extensions.

        Callers decoding bytes must additionally bound the response before JSON
        parsing and reject duplicate keys; :data:`MAX_SNAPSHOT_BYTES` is the
        common transport bound.
        """
        _bounded_json(value)
        body = _object(value, "snapshot")
        if type(body.get("schema_version")) is not int or body["schema_version"] != 1:
            raise ValueError("unsupported telemetry schema_version")
        devices = _array(body.get("devices"), MAX_TRACKED, "devices")
        parsed = tuple(_device(device) for device in devices)
        if len({device.node_id for device in parsed}) != len(parsed):
            raise ValueError("duplicate telemetry node_id")
        host = body.get("host")
        return cls(
            uptime_seconds=_number(body.get("uptime_seconds"), "uptime_seconds"),
            authentication=_enum(
                body.get("authentication"), AUTHENTICATION, "authentication"
            ),
            devices=parsed,
            observer=_counts(body.get("observer"), VERDICTS, "observer"),
            host=_host(host) if host is not None else None,
            # Older producers did not advertise cadence. Allow their slowest
            # supported cadence before declaring an unchanged sample stale.
            sample_interval_seconds=_number(
                body.get("sample_interval_seconds", 60.0),
                "sample_interval_seconds",
                60.0,
                0.2,
            ),
        )


def _bounded_json(value: object) -> None:
    """Bound work even in unknown extension fields, without recursive parsing."""
    pending = [(value, 0)]
    items = 0
    text_bytes = 0
    while pending:
        item, depth = pending.pop()
        items += 1
        if items > 500_000 or depth > 8:
            raise ValueError("telemetry JSON is too complex")
        if isinstance(item, str):
            if len(item) > 4096:
                raise ValueError("telemetry string exceeds limit")
            try:
                text_bytes += len(item.encode("utf-8"))
            except UnicodeError as error:
                raise ValueError("telemetry string is not valid Unicode") from error
        elif isinstance(item, dict):
            if len(item) > 64 or not all(isinstance(key, str) for key in item):
                raise ValueError("telemetry object is too large or has invalid keys")
            pending.extend((child, depth + 1) for child in item.values())
            pending.extend((key, depth + 1) for key in item)
        elif isinstance(item, list):
            if len(item) > MAX_TRACKED:
                raise ValueError("telemetry array exceeds limit")
            pending.extend((child, depth + 1) for child in item)
        elif type(item) is int:
            if not -MAX_COUNTER <= item <= MAX_COUNTER:
                raise ValueError("telemetry integer exceeds limit")
        elif isinstance(item, float):
            if not math.isfinite(item) or abs(item) > MAX_COUNTER:
                raise ValueError("telemetry number exceeds limit")
        elif item is not None and not isinstance(item, bool):
            raise ValueError("telemetry value is not JSON data")
        if text_bytes > MAX_SNAPSHOT_BYTES:
            raise ValueError("telemetry text exceeds limit")


def _object(value: object, field: str) -> Body:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _array(value: object, maximum: int, field: str) -> list[Any]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"{field} must be a bounded array")
    return value


def _text(value: object, maximum: int, field: str) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f"{field} must be a bounded string")
    return value


def _enum(value: object, choices: tuple[str, ...], field: str) -> str:
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"{field} has an unsupported value")
    return value


def _node_id(value: object) -> str:
    if not isinstance(value, str) or NODE_ID_RE.fullmatch(value) is None:
        raise ValueError("telemetry node_id is invalid")
    return value


def _counter(value: object, field: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_COUNTER:
        raise ValueError(f"{field} must be an unsigned 64-bit integer")
    return value


def _number(
    value: object, field: str, maximum: float = MAX_SECONDS, minimum: float = 0.0
) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not minimum <= value <= maximum
    ):
        raise ValueError(f"{field} must be a finite number in range")
    return float(value)


def _optional_number(
    value: object, field: str, maximum: float = MAX_SECONDS
) -> float | None:
    return None if value is None else _number(value, field, maximum)


def _counts(value: object, choices: tuple[str, ...], field: str) -> dict[str, int]:
    counts = _object(value, field)
    return {
        key: _counter(count, field) for key, count in counts.items() if key in choices
    }


def _device(value: object) -> DeviceSnapshot:
    body = _object(value, "device")
    live = body.get("live")
    if not isinstance(live, bool):
        raise ValueError("device live must be a boolean")
    producer = body.get("pairing_producer")
    detail = body.get("driver_detail")
    return DeviceSnapshot(
        node_id=_node_id(body.get("node_id")),
        name=_text(body.get("name"), 64, "name"),
        kind=_enum(body.get("kind"), ("hdmi", "usb"), "kind"),
        role=_enum(body.get("role"), ("producer", "consumer"), "role"),
        state=_enum(body.get("state"), STATES, "state"),
        live=live,
        last_seen_age_seconds=_number(body.get("last_seen_age_seconds"), "age"),
        pairing_producer=_node_id(producer) if producer is not None else None,
        driver_detail=_text(detail, MAX_DETAIL_LENGTH, "driver_detail")
        if detail is not None
        else None,
        control_frames=_counter(body.get("control_frames"), "control_frames"),
        control_bytes=_counter(body.get("control_bytes"), "control_bytes"),
        events=_counts(body.get("events"), EVENTS, "events"),
    )


def _host(value: object) -> Body:
    body = _object(value, "host")
    if body.get("scope") != "collector-host":
        raise ValueError("host scope must be collector-host")
    node = body.get("node_id")
    cpu = _object(body.get("cpu"), "cpu")
    logical_count = cpu.get("logical_count")
    if logical_count is not None:
        logical_count = _counter(logical_count, "logical_count")
        if not 1 <= logical_count <= 1_048_576:
            raise ValueError("logical_count is out of range")
    memory = _memory(body.get("memory"), swap=False)
    swap = _memory(body.get("swap"), swap=True)
    network = [
        _network(item)
        for item in _array(body.get("network"), MAX_INTERFACES, "network")
    ]
    temperatures = [
        _temperature(item)
        for item in _array(body.get("temperatures"), MAX_TEMPERATURES, "temperatures")
    ]
    if len({item["interface"] for item in network}) != len(network):
        raise ValueError("duplicate network interface")
    if len({item["sensor"] for item in temperatures}) != len(temperatures):
        raise ValueError("duplicate temperature sensor")
    return {
        "scope": "collector-host",
        "node_id": _node_id(node) if node is not None else None,
        "cpu": {
            "busy_ratio": _optional_number(cpu.get("busy_ratio"), "busy_ratio", 1.0),
            "logical_count": logical_count,
            "load1": _optional_number(cpu.get("load1"), "load1", 1e9),
        },
        "memory": memory,
        "swap": swap,
        "network": network,
        "temperatures": temperatures,
        "uptime_seconds": _optional_number(body.get("uptime_seconds"), "host uptime"),
    }


def _memory(value: object, *, swap: bool) -> Body | None:
    if value is None:
        return None
    body = _object(value, "swap" if swap else "memory")
    free_key = "free_bytes" if swap else "available_bytes"
    total = _counter(body.get("total_bytes"), "total_bytes")
    free = _counter(body.get(free_key), free_key)
    if free > total:
        raise ValueError("available memory exceeds total")
    ratio = body.get("used_ratio")
    return {
        "total_bytes": total,
        free_key: free,
        "used_ratio": _optional_number(ratio, "used_ratio", 1.0)
        if swap
        else _number(ratio, "used_ratio", 1.0),
    }


def _network(value: object) -> Body:
    body = _object(value, "network interface")
    interface = _text(body.get("interface"), 15, "interface")
    if _INTERFACE_RE.fullmatch(interface) is None:
        raise ValueError("invalid network interface label")
    return {
        "interface": interface,
        **{
            key: _counter(body.get(key), key)
            for key in (
                "rx_bytes",
                "tx_bytes",
                "rx_errors",
                "tx_errors",
                "rx_dropped",
                "tx_dropped",
            )
        },
        **{
            key: _optional_number(body.get(key), key, float(MAX_COUNTER))
            for key in ("rx_bytes_per_second", "tx_bytes_per_second")
        },
    }


def _temperature(value: object) -> Body:
    body = _object(value, "temperature")
    sensor = _text(body.get("sensor"), 96, "sensor")
    if _SENSOR_RE.fullmatch(sensor) is None:
        raise ValueError("invalid temperature sensor label")
    return {
        "sensor": sensor,
        "celsius": _number(body.get("celsius"), "celsius", 250.0, -273.15),
    }
