"""Deterministic synthetic telemetry; no clocks, sockets or hardware access."""

from __future__ import annotations

import math

from yoke.telemetry.model import DeviceSnapshot, Snapshot


def demo_snapshot(now: float) -> Snapshot:
    """Build a repeatable example from elapsed seconds supplied by the caller."""
    if not math.isfinite(now):
        raise ValueError("demo elapsed time must be finite")
    elapsed = max(0.0, now)
    tick = int(elapsed)
    cpu = 0.19 + 0.12 * math.sin(elapsed / 5) + 0.035 * math.cos(elapsed / 1.7)
    memory = 0.43 + 0.025 * math.sin(elapsed / 19)
    rx = 312_000 + 190_000 * (1 + math.sin(elapsed / 7))
    tx = 76_000 + 48_000 * (1 + math.cos(elapsed / 11))
    definitions = (
        (
            "camera-east",
            "East camera (demo)",
            "hdmi",
            "producer",
            "active",
            True,
            None,
            "Demo backend reports ready",
        ),
        (
            "display-den",
            "Den display (demo)",
            "hdmi",
            "consumer",
            "active",
            True,
            "camera-east",
            "Demo IGMP membership applied",
        ),
        (
            "display-studio",
            "Studio display (demo)",
            "hdmi",
            "consumer",
            "pending",
            True,
            "camera-east",
            "Demo backend awaiting attachment",
        ),
        (
            "usb-desk",
            "Desk USB exporter (demo)",
            "usb",
            "producer",
            "active",
            True,
            None,
            "Demo USB/IP export ready",
        ),
        (
            "usb-workstation",
            "Workstation USB (demo)",
            "usb",
            "consumer",
            "active",
            True,
            "usb-desk",
            "Demo USB/IP imports attached",
        ),
        (
            "display-spare",
            "Spare display (demo)",
            "hdmi",
            "consumer",
            "idle",
            False,
            None,
            "Demo endpoint last seen before silence",
        ),
    )
    devices = tuple(
        DeviceSnapshot(
            node_id=node_id,
            name=name,
            kind=kind,
            role=role,
            state=state,
            live=live,
            last_seen_age_seconds=(elapsed % 2 + index / 10) if live else 48 + elapsed,
            pairing_producer=pair,
            driver_detail=detail,
            control_frames=150 + tick // 2 + index * 11,
            control_bytes=(150 + tick // 2 + index * 11) * 368,
            events={"media-up": 1, "pairing-applied": 1} if pair else {},
        )
        for index, (node_id, name, kind, role, state, live, pair, detail) in enumerate(
            definitions
        )
    )
    total_memory = 8 * 1024**3
    return Snapshot(
        uptime_seconds=elapsed,
        authentication="demo",
        devices=devices,
        observer={"ok": 1048 + tick * 3, "bad-sig": 0, "malformed": 0},
        host={
            "scope": "collector-host",
            "node_id": None,
            "cpu": {"busy_ratio": cpu, "logical_count": 4, "load1": 0.74 + cpu},
            "memory": {
                "total_bytes": total_memory,
                "available_bytes": int(total_memory * (1 - memory)),
                "used_ratio": memory,
            },
            "swap": {
                "total_bytes": 2 * 1024**3,
                "free_bytes": 2 * 1024**3,
                "used_ratio": 0.0,
            },
            "network": [
                {
                    "interface": "demo0",
                    "rx_bytes": 400_000_000 + tick * 502_000,
                    "tx_bytes": 120_000_000 + tick * 124_000,
                    "rx_errors": 0,
                    "tx_errors": 0,
                    "rx_dropped": 0,
                    "tx_dropped": 0,
                    "rx_bytes_per_second": rx,
                    "tx_bytes_per_second": tx,
                }
            ],
            "temperatures": [{"sensor": "demo-package", "celsius": 44 + cpu * 12}],
            "uptime_seconds": 18_000 + elapsed,
        },
    )
