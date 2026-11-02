"""Stable metric families expose no unbounded diagnostic labels."""

from __future__ import annotations

from dataclasses import replace

from tests.test_telemetry_model import sample
from yoke.telemetry.model import STATES, Snapshot
from yoke.telemetry.prometheus import CONTENT_TYPE, render


def test_device_metric_values_and_bounded_labels() -> None:
    snapshot = sample()
    text = render(snapshot).decode()
    assert CONTENT_TYPE == "text/plain; version=0.0.4; charset=utf-8"
    assert (
        'yoke_device_info{node_id="screen-1",kind="hdmi",role="consumer"} 1\n' in text
    )
    assert 'yoke_device_up{node_id="screen-1"} 1\n' in text
    assert 'yoke_device_last_seen_age_seconds{node_id="screen-1"} 0.5\n' in text
    assert 'yoke_device_control_frames_total{node_id="screen-1"} 3\n' in text
    assert 'yoke_device_control_bytes_total{node_id="screen-1"} 600\n' in text
    assert 'yoke_device_events_total{node_id="screen-1",event="media-up"} 1\n' in text
    assert 'yoke_observer_datagrams_total{verdict="bad-sig"} 1\n' in text
    assert "yoke_observer_uptime_seconds 12.5\n" in text
    for state in STATES:
        expected = int(state == "active")
        assert (
            f'yoke_device_state{{node_id="screen-1",state="{state}"}} {expected}\n'
            in text
        )
    assert "Den" not in text
    assert "Backend" not in text
    assert "camera-1" not in text
    assert "\x1b" not in text
    assert "\u202e" not in text


def test_host_scope_and_measured_units() -> None:
    text = render(sample()).decode()
    assert 'yoke_host_info{scope="collector-host",node_id="warden-1"} 1\n' in text
    assert "yoke_host_cpu_busy_ratio 0.25\n" in text
    assert "yoke_host_cpu_logical_processors 4\n" in text
    assert "yoke_host_cpu_logical_count" not in text
    assert "yoke_host_memory_total_bytes 1000\n" in text
    assert "yoke_host_memory_available_bytes 400\n" in text
    assert 'yoke_host_network_receive_bytes_total{interface="eth0"} 1200\n' in text
    assert 'yoke_host_network_transmit_bytes_total{interface="eth0"} 200\n' in text
    assert 'yoke_host_network_receive_errors_total{interface="eth0"} 1\n' in text
    assert 'yoke_host_network_receive_dropped_total{interface="eth0"} 3\n' in text
    assert 'yoke_host_network_receive_bytes_per_second{interface="eth0"} 100\n' in text
    assert "yoke_host_network_transmit_bytes_per_second" not in text
    assert 'yoke_host_temperature_celsius{sensor="thermal_zone0:cpu"} 40\n' in text
    assert "yoke_host_uptime_seconds 90\n" in text
    assert text.count('node_id="warden-1"') == 1
    assert "yoke_host_swap_used_ratio" not in text


def test_missing_host_metrics_are_omitted_without_fabricated_zeros() -> None:
    snapshot = replace(sample(), host=None)
    assert "yoke_host_" not in render(snapshot).decode()
    snapshot = replace(snapshot, host={"scope": "collector-host"})
    text = render(snapshot).decode()
    assert 'yoke_host_info{scope="collector-host"} 1\n' in text
    assert "yoke_host_cpu" not in text
    assert "yoke_host_temperature" not in text


def test_every_emitted_family_has_help_and_type_and_no_timestamps() -> None:
    text = render(sample()).decode()
    lines = text.splitlines()
    for line in lines:
        if line.startswith("#"):
            continue
        name = line.split("{", 1)[0].split(" ", 1)[0]
        assert any(item.startswith(f"# HELP {name} ") for item in lines)
        assert any(item.startswith(f"# TYPE {name} ") for item in lines)
        assert len(line.rsplit("}", 1)[-1].split()) == (1 if "}" in line else 2)


def test_rendering_is_deterministic_and_orders_devices() -> None:
    snapshot = sample()
    device = snapshot.devices[0]
    first = replace(device, node_id="a-first")
    last = replace(device, node_id="z-last")
    reverse = replace(snapshot, devices=(last, first))
    forward = replace(snapshot, devices=(first, last))
    assert render(reverse) == render(forward)
    assert render(snapshot) == render(snapshot)


def test_prometheus_label_escaping_and_unknown_event_omission() -> None:
    # Constructors are trusted local APIs; the renderer still escapes labels
    # defensively even though remote parsing refuses invalid node IDs.
    snapshot = sample()
    device = replace(
        snapshot.devices[0], node_id='odd"\\\nnode', events={"arbitrary-new-event": 1}
    )
    text = render(replace(snapshot, devices=(device,), host=None)).decode()
    assert 'node_id="odd\\"\\\\\\nnode"' in text
    assert "arbitrary-new-event" not in text
    assert "yoke_device_events_total" not in text


def test_empty_observer_has_only_real_uptime() -> None:
    text = render(Snapshot(0, "open-unauthenticated", (), {}, None)).decode()
    assert "yoke_observer_uptime_seconds 0\n" in text
    assert "yoke_device_" not in text
    assert "yoke_host_" not in text
