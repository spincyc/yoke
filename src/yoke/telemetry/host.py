"""Bounded Linux collector-host samples; no I/O until :meth:`sample`."""

from __future__ import annotations

import math
import os
import re
from itertools import islice
from pathlib import Path

from yoke.core.types import NODE_ID_RE, Body

_MAX_COUNTER = (1 << 64) - 1
_MAX_INTERFACES = 64
_MAX_SENSORS = 16
_INTERFACE_RE = re.compile(r"[A-Za-z0-9_.:-]{1,15}")
_ZONE_RE = re.compile(r"thermal_zone[0-9]{1,8}")
_CPU_RE = re.compile(r"cpu[0-9]{1,8}")
_UINT_RE = re.compile(r"[0-9]{1,20}")
_INT_RE = re.compile(r"-?[0-9]{1,20}")


def _read_text(path: Path, limit: int) -> str | None:
    try:
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
    except (OSError, ValueError):
        return None
    if len(data) > limit:
        return None
    return data.decode("utf-8", errors="replace")


def _counter(text: str) -> int | None:
    if _UINT_RE.fullmatch(text) is None:
        return None
    number = int(text)
    return number if number <= _MAX_COUNTER else None


def _first_float(text: str | None, *, maximum: float = math.inf) -> float | None:
    if text is None:
        return None
    fields = text.split()
    if not fields:
        return None
    try:
        number = float(fields[0])
    except ValueError:
        return None
    return number if math.isfinite(number) and 0 <= number <= maximum else None


def _cpu_counters(fields: list[str]) -> tuple[int, ...] | None:
    if len(fields) < 4:
        return None
    counters = [_counter(value) for value in fields[:8]]
    if any(value is None for value in counters):
        return None
    return tuple(value for value in counters if value is not None)


def _read_cpu(path: Path) -> tuple[tuple[int, ...] | None, int | None]:
    text = _read_text(path, 256 * 1024)
    if text is None:
        return None, None
    aggregate: tuple[int, ...] | None = None
    logical_ids: set[str] = set()
    valid_count = True
    for line in text.splitlines():
        fields = line.split()
        if not fields:
            continue
        if fields[0] == "cpu":
            aggregate = _cpu_counters(fields[1:])
        elif _CPU_RE.fullmatch(fields[0]) is not None:
            if fields[0] in logical_ids or _cpu_counters(fields[1:]) is None:
                valid_count = False
            logical_ids.add(fields[0])
    count = len(logical_ids) if valid_count and logical_ids else None
    return aggregate, count


def _memory(path: Path) -> tuple[Body | None, Body | None]:
    text = _read_text(path, 16 * 1024)
    if text is None:
        return None, None
    values: dict[str, int] = {}
    wanted = {"MemTotal:", "MemAvailable:", "SwapTotal:", "SwapFree:"}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) != 3 or fields[0] not in wanted or fields[2] != "kB":
            continue
        value = _counter(fields[1])
        if value is not None and value <= _MAX_COUNTER // 1024:
            values[fields[0]] = value * 1024
    memory: Body | None = None
    total = values.get("MemTotal:")
    available = values.get("MemAvailable:")
    if total is not None and total > 0 and available is not None and available <= total:
        memory = {
            "total_bytes": total,
            "available_bytes": available,
            "used_ratio": (total - available) / total,
        }
    swap: Body | None = None
    swap_total = values.get("SwapTotal:")
    swap_free = values.get("SwapFree:")
    if swap_total is not None and swap_free is not None and swap_free <= swap_total:
        swap = {
            "total_bytes": swap_total,
            "free_bytes": swap_free,
            "used_ratio": (swap_total - swap_free) / swap_total if swap_total else None,
        }
    return memory, swap


def _network(path: Path) -> dict[str, tuple[int, ...]]:
    text = _read_text(path, 64 * 1024)
    if text is None:
        return {}
    interfaces: dict[str, tuple[int, ...]] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        name, data = line.rsplit(":", 1)
        name = name.strip()
        if _INTERFACE_RE.fullmatch(name) is None or name in interfaces:
            continue
        fields = data.split()
        if len(fields) != 16:
            continue
        values = [_counter(value) for value in fields]
        if any(value is None for value in values):
            continue
        # Linux net/dev puts the eight receive counters before transmit counters.
        selected = (values[0], values[8], values[2], values[10], values[3], values[11])
        interfaces[name] = tuple(value for value in selected if value is not None)
        if len(interfaces) == _MAX_INTERFACES:
            break
    return interfaces


def _within_root(path: Path, root: Path) -> Path | None:
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    return resolved if resolved.is_relative_to(root) else None


def _temperatures(sys_root: Path) -> list[Body]:
    try:
        root = sys_root.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return []
    thermal = _within_root(root / "class" / "thermal", root)
    if thermal is None:
        return []
    temperatures: list[Body] = []
    zones = 0
    try:
        with os.scandir(thermal) as entries:
            # Bound traversal too: cooling-device directories need no inspection.
            for entry in islice(entries, 128):
                if _ZONE_RE.fullmatch(entry.name) is None:
                    continue
                zones += 1
                zone = _within_root(Path(entry.path), root)
                if zone is not None:
                    temp_path = _within_root(zone / "temp", root)
                    text = _read_text(temp_path, 128) if temp_path is not None else None
                    value = text.strip() if text is not None else ""
                    if _INT_RE.fullmatch(value) is not None:
                        millicelsius = int(value)
                        if -273150 <= millicelsius <= 250000:
                            type_path = _within_root(zone / "type", root)
                            kind = (
                                _read_text(type_path, 256)
                                if type_path is not None
                                else None
                            )
                            safe_type = (
                                re.sub(r"[^A-Za-z0-9_.-]", "_", kind.strip())[:64]
                                if kind is not None
                                else ""
                            )
                            sensor = entry.name + (":" + safe_type if safe_type else "")
                            temperatures.append(
                                {"sensor": sensor, "celsius": millicelsius / 1000}
                            )
                if zones == _MAX_SENSORS:
                    break
    except (OSError, ValueError):
        return []
    return temperatures


class HostSampler:
    """Sample a small Linux host subset, preserving unknown/reset intervals.

    The caller supplies monotonic time. CPU guest time is already included in
    user/nice, so only the first eight aggregate counters participate. Linux
    iowait may decrease; its delta is clamped to zero while other decreases
    invalidate an interval. CPU and network baselines clear when a source is
    unavailable, so recovery never presents a rate across an unobserved gap.
    """

    def __init__(
        self,
        *,
        proc_root: Path = Path("/proc"),
        sys_root: Path = Path("/sys"),
        local_node: str | None = None,
    ) -> None:
        if local_node is not None and (
            not isinstance(local_node, str) or NODE_ID_RE.fullmatch(local_node) is None
        ):
            raise ValueError("local_node must be a valid Yoke node ID")
        self._proc_root = Path(proc_root)
        self._sys_root = Path(sys_root)
        self._local_node = local_node
        self._cpu_previous: tuple[float, tuple[int, ...]] | None = None
        self._network_previous: tuple[float, dict[str, tuple[int, ...]]] | None = None

    def sample(self, now: float) -> Body:
        if isinstance(now, bool) or not isinstance(now, (int, float)):
            raise ValueError("now must be a finite monotonic timestamp")
        try:
            finite = math.isfinite(now)
        except OverflowError:
            finite = False
        if not finite:
            raise ValueError("now must be a finite monotonic timestamp")
        now = float(now)
        counters, count = _read_cpu(self._proc_root / "stat")
        busy_ratio = self._cpu_busy(counters, now)
        memory, swap = _memory(self._proc_root / "meminfo")
        network = self._network_sample(now)
        return {
            "scope": "collector-host",
            "node_id": self._local_node,
            "cpu": {
                "busy_ratio": busy_ratio,
                "logical_count": count,
                "load1": _first_float(
                    _read_text(self._proc_root / "loadavg", 256), maximum=1e9
                ),
            },
            "memory": memory,
            "swap": swap,
            "network": network,
            "temperatures": _temperatures(self._sys_root),
            "uptime_seconds": _first_float(
                _read_text(self._proc_root / "uptime", 256), maximum=1e12
            ),
        }

    def _cpu_busy(self, counters: tuple[int, ...] | None, now: float) -> float | None:
        previous = self._cpu_previous
        self._cpu_previous = (now, counters) if counters is not None else None
        if (
            counters is None
            or previous is None
            or now <= previous[0]
            or len(counters) != len(previous[1])
        ):
            return None
        deltas = [value - old for value, old in zip(counters, previous[1], strict=True)]
        if any(value < 0 for index, value in enumerate(deltas) if index != 4):
            return None
        if len(deltas) > 4:
            deltas[4] = max(0, deltas[4])
        total = sum(deltas)
        if total <= 0:
            return None
        idle = deltas[3] + (deltas[4] if len(deltas) > 4 else 0)
        return (total - idle) / total

    def _network_sample(self, now: float) -> list[Body]:
        current = _network(self._proc_root / "net" / "dev")
        previous = self._network_previous
        self._network_previous = (now, current) if current else None
        result: list[Body] = []
        for name, values in current.items():
            rx_rate: float | None = None
            tx_rate: float | None = None
            old = previous[1].get(name) if previous is not None else None
            if previous is not None and old is not None and now > previous[0]:
                deltas = [
                    value - prior for value, prior in zip(values, old, strict=True)
                ]
                if all(delta >= 0 for delta in deltas):
                    elapsed = now - previous[0]
                    if math.isfinite(elapsed):
                        rx = deltas[0] / elapsed
                        tx = deltas[1] / elapsed
                        rx_rate = (
                            rx if math.isfinite(rx) and rx <= _MAX_COUNTER else None
                        )
                        tx_rate = (
                            tx if math.isfinite(tx) and tx <= _MAX_COUNTER else None
                        )
            result.append(
                {
                    "interface": name,
                    "rx_bytes": values[0],
                    "tx_bytes": values[1],
                    "rx_errors": values[2],
                    "tx_errors": values[3],
                    "rx_dropped": values[4],
                    "tx_dropped": values[5],
                    "rx_bytes_per_second": rx_rate,
                    "tx_bytes_per_second": tx_rate,
                }
            )
        return result
