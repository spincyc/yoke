"""Collector-host samples use bounded, offline Linux proc/sys fixtures."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

from yoke.telemetry.host import HostSampler


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _cpu(proc: Path, counters: str) -> None:
    _write(proc / "stat", f"cpu {counters}\ncpu0 1 2 3 4\ncpu1 1 2 3 4\n")


def _net(proc: Path, rx: int, tx: int, *, errors: int = 0, name: str = "eth0") -> None:
    _write(
        proc / "net" / "dev",
        f"{name}: {rx} 0 {errors} 3 0 0 0 0 {tx} 0 4 5 0 0 0 0\n",
    )


def _sampler(tmp_path: Path, **kwargs: Any) -> tuple[HostSampler, Path, Path]:
    proc = tmp_path / "proc"
    sys = tmp_path / "sys"
    return HostSampler(proc_root=proc, sys_root=sys, **kwargs), proc, sys


def test_constructor_performs_no_io_and_unsupported_host_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("constructor performed filesystem I/O")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", fail)
        patch.setattr(Path, "resolve", fail)
        sampler, _, _ = _sampler(tmp_path, local_node="collector-1")
    assert sampler.sample(0.0) == {
        "scope": "collector-host",
        "node_id": "collector-1",
        "cpu": {"busy_ratio": None, "logical_count": None, "load1": None},
        "memory": None,
        "swap": None,
        "network": [],
        "temperatures": [],
        "uptime_seconds": None,
    }


def test_cpu_guest_not_double_counted_and_zero_is_a_measurement(tmp_path: Path) -> None:
    sampler, proc, _ = _sampler(tmp_path)
    _cpu(proc, "100 20 30 200 50 10 5 5 80 10")
    assert sampler.sample(1)["cpu"] == {
        "busy_ratio": None,
        "logical_count": 2,
        "load1": None,
    }
    _cpu(proc, "110 20 40 220 60 10 5 5 90 10")
    assert sampler.sample(2)["cpu"]["busy_ratio"] == pytest.approx(0.4)
    _cpu(proc, "110 20 40 230 60 10 5 5 90 10")
    assert sampler.sample(3)["cpu"]["busy_ratio"] == 0.0
    assert sampler.sample(4)["cpu"]["busy_ratio"] is None


def test_cpu_decreasing_iowait_is_clamped_but_reset_is_unknown(tmp_path: Path) -> None:
    sampler, proc, _ = _sampler(tmp_path)
    _cpu(proc, "100 0 0 100 20 0 0 0")
    sampler.sample(1)
    _cpu(proc, "110 0 0 110 15 0 0 0")
    assert sampler.sample(2)["cpu"]["busy_ratio"] == 0.5
    _cpu(proc, "1 0 0 1 1 0 0 0")
    assert sampler.sample(3)["cpu"]["busy_ratio"] is None
    _cpu(proc, "2 0 0 2 1 0 0 0")
    assert sampler.sample(4)["cpu"]["busy_ratio"] == 0.5


@pytest.mark.parametrize("time", [1.0, 0.0, -1.0])
def test_cpu_and_network_nonprogressing_clock_is_unknown(
    tmp_path: Path, time: float
) -> None:
    sampler, proc, _ = _sampler(tmp_path)
    _cpu(proc, "100 0 0 100")
    _net(proc, 100, 200)
    sampler.sample(1)
    _cpu(proc, "110 0 0 110")
    _net(proc, 110, 220)
    sample = sampler.sample(time)
    assert sample["cpu"]["busy_ratio"] is None
    assert sample["network"][0]["rx_bytes_per_second"] is None
    assert sample["network"][0]["tx_bytes_per_second"] is None


def test_network_rates_zero_reset_and_counter_rollover(tmp_path: Path) -> None:
    sampler, proc, _ = _sampler(tmp_path)
    _net(proc, 100, 200, errors=2)
    assert sampler.sample(1)["network"][0] == {
        "interface": "eth0",
        "rx_bytes": 100,
        "tx_bytes": 200,
        "rx_errors": 2,
        "tx_errors": 4,
        "rx_dropped": 3,
        "tx_dropped": 5,
        "rx_bytes_per_second": None,
        "tx_bytes_per_second": None,
    }
    _net(proc, 130, 200, errors=2)
    sample = sampler.sample(3)["network"][0]
    assert sample["rx_bytes_per_second"] == 15.0
    assert sample["tx_bytes_per_second"] == 0.0
    # Reset of any counter fences both interface rates.
    _net(proc, 150, 220, errors=0)
    sample = sampler.sample(4)["network"][0]
    assert sample["rx_bytes_per_second"] is None
    assert sample["tx_bytes_per_second"] is None
    _net(proc, (1 << 64) - 1, (1 << 64) - 1)
    sampler.sample(5)
    _net(proc, 10, 10)
    sample = sampler.sample(6)["network"][0]
    assert sample["rx_bytes"] == 10
    assert sample["rx_bytes_per_second"] is None
    assert sample["tx_bytes_per_second"] is None


def test_missing_sources_clear_rate_baselines_and_never_cache_values(
    tmp_path: Path,
) -> None:
    sampler, proc, _ = _sampler(tmp_path)
    _cpu(proc, "1 2 3 4")
    _net(proc, 1, 2)
    sampler.sample(1)
    (proc / "stat").unlink()
    (proc / "net" / "dev").unlink()
    sample = sampler.sample(2)
    assert sample["cpu"]["busy_ratio"] is None
    assert sample["cpu"]["logical_count"] is None
    assert sample["network"] == []
    _cpu(proc, "10 20 30 40")
    _net(proc, 10, 20)
    sample = sampler.sample(3)
    assert sample["cpu"]["busy_ratio"] is None
    assert sample["network"][0]["rx_bytes_per_second"] is None


def test_memory_uses_available_and_swap_has_real_zero(tmp_path: Path) -> None:
    sampler, proc, _ = _sampler(tmp_path)
    _write(
        proc / "meminfo",
        "MemTotal: 1000 kB\nMemFree: 1 kB\nMemAvailable: 250 kB\n"
        "SwapTotal: 100 kB\nSwapFree: 60 kB\n",
    )
    sample = sampler.sample(1)
    assert sample["memory"] == {
        "total_bytes": 1024000,
        "available_bytes": 256000,
        "used_ratio": 0.75,
    }
    assert sample["swap"] == {
        "total_bytes": 102400,
        "free_bytes": 61440,
        "used_ratio": 0.4,
    }
    _write(
        proc / "meminfo",
        "MemTotal: 100 kB\nMemFree: 10 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n",
    )
    sample = sampler.sample(2)
    assert sample["memory"] is None
    assert sample["swap"] == {"total_bytes": 0, "free_bytes": 0, "used_ratio": None}


@pytest.mark.parametrize(
    "text",
    [
        "MemTotal: 0 kB\nMemAvailable: 0 kB\n",
        "MemTotal: 10 kB\nMemAvailable: 11 kB\n",
        "MemTotal: -10 kB\nMemAvailable: 1 kB\n",
        "MemTotal: 10 MB\nMemAvailable: 1 MB\n",
        "MemTotal: 18446744073709551615 kB\nMemAvailable: 1 kB\n",
        "MemTotal: True kB\nMemAvailable: 1 kB\n",
        "MemTotal: 10 kB\nMemAvailable: nan kB\n",
    ],
)
def test_invalid_memory_is_unknown(tmp_path: Path, text: str) -> None:
    sampler, proc, _ = _sampler(tmp_path)
    _write(proc / "meminfo", text)
    assert sampler.sample(1)["memory"] is None


def test_load_and_uptime_are_finite_and_refresh_independently(tmp_path: Path) -> None:
    sampler, proc, _ = _sampler(tmp_path)
    _write(proc / "loadavg", "1.25 2.0 3.0 1/2 3\n")
    _write(proc / "uptime", "123.5 200.0\n")
    sample = sampler.sample(1)
    assert sample["cpu"]["load1"] == 1.25
    assert sample["uptime_seconds"] == 123.5
    for text in ["NaN", "inf", "-1", "broken", "", "true"]:
        _write(proc / "loadavg", text)
        _write(proc / "uptime", text)
        sample = sampler.sample(2)
        assert sample["cpu"]["load1"] is None
        assert sample["uptime_seconds"] is None
    _write(proc / "loadavg", "1e100")
    assert sampler.sample(3)["cpu"]["load1"] is None


def test_thermal_class_symlinks_are_safe_and_sensor_names_are_bounded(
    tmp_path: Path,
) -> None:
    sampler, _, sys = _sampler(tmp_path)
    zone = sys / "devices" / "virtual" / "thermal" / "thermal_zone0"
    _write(zone / "temp", "42500\n")
    _write(zone / "type", "CPU \x1b[2J\n" + "x" * 100)
    thermal = sys / "class" / "thermal"
    thermal.mkdir(parents=True)
    (thermal / "thermal_zone0").symlink_to(zone, target_is_directory=True)
    outside = tmp_path / "outside"
    _write(outside / "temp", "50000")
    (thermal / "thermal_zone1").symlink_to(outside, target_is_directory=True)
    _write(thermal / "thermal_zone2" / "temp", "nan")
    _write(thermal / "thermal_zone3" / "temp", "-273151")
    _write(thermal / "thermal_zone4" / "temp", "250001")
    _write(thermal / "thermal_zone5" / "type", "outside")
    (thermal / "thermal_zone5" / "temp").symlink_to(outside / "temp")
    values = sampler.sample(1)["temperatures"]
    assert len(values) == 1
    assert values[0]["celsius"] == 42.5
    assert values[0]["sensor"].startswith("thermal_zone0:CPU___2J_")
    assert len(values[0]["sensor"]) <= 96
    assert "\x1b" not in values[0]["sensor"]


def test_sys_class_directory_cannot_escape_root(tmp_path: Path) -> None:
    sampler, _, sys = _sampler(tmp_path)
    outside = tmp_path / "outside"
    _write(outside / "thermal_zone0" / "temp", "40000")
    (sys / "class").mkdir(parents=True)
    (sys / "class" / "thermal").symlink_to(outside, target_is_directory=True)
    assert sampler.sample(1)["temperatures"] == []


def test_interface_and_thermal_counts_are_bounded(tmp_path: Path) -> None:
    sampler, proc, sys = _sampler(tmp_path)
    _write(
        proc / "net" / "dev",
        "".join(f"eth{i}: 1 0 0 0 0 0 0 0 2 0 0 0 0 0 0 0\n" for i in range(200)),
    )
    for i in range(40):
        _write(sys / "class" / "thermal" / f"thermal_zone{i}" / "temp", "42000")
    sample = sampler.sample(1)
    assert len(sample["network"]) == 64
    assert len(sample["temperatures"]) == 16


def test_oversized_files_and_malformed_counters_are_unavailable(tmp_path: Path) -> None:
    sampler, proc, _ = _sampler(tmp_path)
    limits = {
        "stat": 256 * 1024,
        "meminfo": 16 * 1024,
        "net/dev": 64 * 1024,
        "loadavg": 256,
        "uptime": 256,
    }
    for name, limit in limits.items():
        _write(proc / name, "x" * (limit + 1))
    sample = sampler.sample(1)
    assert sample["cpu"] == {"busy_ratio": None, "logical_count": None, "load1": None}
    assert sample["memory"] is None
    assert sample["swap"] is None
    assert sample["network"] == []
    assert sample["uptime_seconds"] is None
    _cpu(proc, "1 2 -3 4")
    _net(proc, 1 << 64, 100)
    assert sampler.sample(2)["cpu"]["busy_ratio"] is None
    assert sampler.sample(3)["network"] == []
    _write(proc / "net" / "dev", 'bad"name: 1 0 0 0 0 0 0 0 2 0 0 0 0 0 0 0\n')
    assert sampler.sample(4)["network"] == []


@pytest.mark.parametrize(
    "now", [True, False, math.nan, math.inf, -math.inf, "1", None, 10**1000]
)
def test_invalid_clock_is_rejected_before_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, now: Any
) -> None:
    sampler, _, _ = _sampler(tmp_path)

    def fail(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("invalid clock caused I/O")

    monkeypatch.setattr(Path, "open", fail)
    with pytest.raises(ValueError, match="finite monotonic"):
        sampler.sample(now)


def test_extreme_finite_times_do_not_export_nonfinite_rates(tmp_path: Path) -> None:
    sampler, proc, _ = _sampler(tmp_path)
    _net(proc, 0, 0)
    sampler.sample(-1e308)
    _net(proc, 1, 1)
    sample = sampler.sample(1e308)
    assert sample["network"][0]["rx_bytes_per_second"] is None
    _net(proc, 0, 0)
    sampler.sample(0.0)
    _net(proc, 1, 1)
    sample = sampler.sample(5e-324)
    assert sample["network"][0]["rx_bytes_per_second"] is None
    json.dumps(sample, allow_nan=False)


@pytest.mark.parametrize("node", ["", "with spaces", "x\n", "A", "a" * 33, 5, True])
def test_invalid_local_node_is_rejected(tmp_path: Path, node: Any) -> None:
    with pytest.raises(ValueError, match="valid Yoke node ID"):
        _sampler(tmp_path, local_node=node)


def test_malformed_logical_cpu_lines_make_count_unknown(tmp_path: Path) -> None:
    sampler, proc, _ = _sampler(tmp_path)
    _write(proc / "stat", "cpu 1 2 3 4\ncpu0 1 2 3 4\ncpu1 broken\n")
    assert sampler.sample(1)["cpu"]["logical_count"] is None
    _write(proc / "stat", "cpu 2 3 4 5\ncpu0 1 2 3 4\ncpu0 1 2 3 4\n")
    assert sampler.sample(2)["cpu"]["logical_count"] is None


def test_permission_errors_do_not_block_other_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sampler, proc, _ = _sampler(tmp_path)
    _cpu(proc, "1 2 3 4")
    _write(proc / "uptime", "123 0")
    original = Path.open

    def open_file(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path.name in {"stat", "meminfo", "dev"}:
            raise PermissionError("fixture denies access")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_file)
    sample = sampler.sample(1)
    assert sample["cpu"]["logical_count"] is None
    assert sample["memory"] is None
    assert sample["network"] == []
    assert sample["uptime_seconds"] == 123.0


def test_rate_and_uptime_export_limits(tmp_path: Path) -> None:
    sampler, proc, _ = _sampler(tmp_path)
    _write(proc / "uptime", "1000000000001 0")
    _net(proc, 0, 0)
    assert sampler.sample(1)["uptime_seconds"] is None
    _net(proc, (1 << 64) - 1, 1)
    sample = sampler.sample(1.5)
    assert sample["network"][0]["rx_bytes_per_second"] is None
    assert sample["network"][0]["tx_bytes_per_second"] == 2.0
