"""Deterministic Prometheus text 0.0.4 rendering of one telemetry sample."""

from __future__ import annotations

from collections.abc import Iterable

from yoke.core.types import Body
from yoke.snoop import VERDICTS
from yoke.telemetry.model import EVENTS, STATES, Snapshot

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

_Sample = tuple[dict[str, str], int | float]


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _metric(
    lines: list[str], name: str, kind: str, help_text: str, samples: Iterable[_Sample]
) -> None:
    values = list(samples)
    if not values:
        return
    lines.extend((f"# HELP {name} {help_text}", f"# TYPE {name} {kind}"))
    for labels, value in values:
        suffix = ""
        if labels:
            suffix = (
                "{"
                + ",".join(f'{key}="{_escape(item)}"' for key, item in labels.items())
                + "}"
            )
        number = str(value) if isinstance(value, int) else format(value, ".17g")
        lines.append(f"{name}{suffix} {number}")


def render(snapshot: Snapshot) -> bytes:
    """Render bounded labels, current retained state and observed counters.

    Names, driver details, configured peers and pairing versions are never
    labels. Missing host measurements produce no sample. No timestamps or
    payload-derived measurements are emitted.
    """
    lines: list[str] = []
    devices = sorted(snapshot.devices, key=lambda device: device.node_id)
    _metric(
        lines,
        "yoke_device_info",
        "gauge",
        "Accepted announcement identity for a retained device.",
        (
            (
                {"node_id": device.node_id, "kind": device.kind, "role": device.role},
                1,
            )
            for device in devices
        ),
    )
    for name, help_text, values in (
        (
            "yoke_device_up",
            "Whether an accepted announcement was received within the peer timeout.",
            (({"node_id": device.node_id}, int(device.live)) for device in devices),
        ),
        (
            "yoke_device_state",
            "Last announced state; separate from observer liveness.",
            (
                (
                    {"node_id": device.node_id, "state": state},
                    int(device.state == state),
                )
                for device in devices
                for state in STATES
            ),
        ),
        (
            "yoke_device_last_seen_age_seconds",
            "Seconds since the last accepted device announcement.",
            (
                ({"node_id": device.node_id}, device.last_seen_age_seconds)
                for device in devices
            ),
        ),
    ):
        _metric(lines, name, "gauge", help_text, values)
    _metric(
        lines,
        "yoke_device_control_frames_total",
        "counter",
        "Accepted control datagrams observed while this device was retained.",
        (({"node_id": device.node_id}, device.control_frames) for device in devices),
    )
    _metric(
        lines,
        "yoke_device_control_bytes_total",
        "counter",
        "Accepted encoded control bytes observed while this device was retained.",
        (({"node_id": device.node_id}, device.control_bytes) for device in devices),
    )
    _metric(
        lines,
        "yoke_device_events_total",
        "counter",
        "Accepted observed events; resets on process restart or peer eviction.",
        (
            ({"node_id": device.node_id, "event": event}, device.events[event])
            for device in devices
            for event in EVENTS
            if event in device.events
        ),
    )
    _metric(
        lines,
        "yoke_observer_datagrams_total",
        "counter",
        "Observed control datagrams classified by admission verdict.",
        (
            ({"verdict": verdict}, snapshot.observer[verdict])
            for verdict in VERDICTS
            if verdict in snapshot.observer
        ),
    )
    _metric(
        lines,
        "yoke_observer_uptime_seconds",
        "gauge",
        "Elapsed monotonic time since this telemetry observer started.",
        (({}, snapshot.uptime_seconds),),
    )
    if snapshot.host is not None:
        _host(lines, snapshot.host)
    return ("\n".join(lines) + "\n").encode("utf-8")


def _host(lines: list[str], host: Body) -> None:
    labels = {"scope": "collector-host"}
    if host.get("node_id") is not None:
        labels["node_id"] = host["node_id"]
    _metric(
        lines,
        "yoke_host_info",
        "gauge",
        "Host measurements belong to the collector host; node association is optional.",
        ((labels, 1),),
    )
    cpu = host.get("cpu", {})
    for key, suffix, help_text in (
        ("busy_ratio", "cpu_busy_ratio", "Collector host CPU busy fraction."),
        (
            "logical_count",
            "cpu_logical_processors",
            "Collector host logical processor count.",
        ),
        ("load1", "load1", "Collector host one-minute load average."),
    ):
        _optional(lines, f"yoke_host_{suffix}", help_text, cpu.get(key))
    _optional(
        lines,
        "yoke_host_uptime_seconds",
        "Collector host uptime in seconds.",
        host.get("uptime_seconds"),
    )
    for section, free_key in (("memory", "available_bytes"), ("swap", "free_bytes")):
        values = host.get(section)
        if values is None:
            continue
        for key in ("total_bytes", free_key, "used_ratio"):
            _optional(
                lines,
                f"yoke_host_{section}_{key}",
                f"Collector host {section} {key.replace('_', ' ')}.",
                values.get(key),
            )
    interfaces = sorted(host.get("network", []), key=lambda item: item["interface"])
    for source, suffix, kind, description in (
        ("rx_bytes", "receive_bytes_total", "counter", "received bytes"),
        ("tx_bytes", "transmit_bytes_total", "counter", "transmitted bytes"),
        ("rx_errors", "receive_errors_total", "counter", "receive errors"),
        ("tx_errors", "transmit_errors_total", "counter", "transmit errors"),
        ("rx_dropped", "receive_dropped_total", "counter", "receive drops"),
        ("tx_dropped", "transmit_dropped_total", "counter", "transmit drops"),
        (
            "rx_bytes_per_second",
            "receive_bytes_per_second",
            "gauge",
            "received bytes per second",
        ),
        (
            "tx_bytes_per_second",
            "transmit_bytes_per_second",
            "gauge",
            "transmitted bytes per second",
        ),
    ):
        _metric(
            lines,
            f"yoke_host_network_{suffix}",
            kind,
            f"Collector host network {description}.",
            (
                ({"interface": item["interface"]}, item[source])
                for item in interfaces
                if item.get(source) is not None
            ),
        )
    _metric(
        lines,
        "yoke_host_temperature_celsius",
        "gauge",
        "Collector host thermal sensor temperature in degrees Celsius.",
        (
            ({"sensor": item["sensor"]}, item["celsius"])
            for item in sorted(
                host.get("temperatures", []), key=lambda item: item["sensor"]
            )
        ),
    )


def _optional(lines: list[str], name: str, help_text: str, value: object) -> None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        _metric(lines, name, "gauge", help_text, (({}, value),))
