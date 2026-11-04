"""Simulated USB data-path benchmark; prints one JSON document (or Markdown).

Run from the checkout with ``make benchmark-usb``. The full path runs in one
Python process:

    host model ↔ simulated UDC ↔ proxy ↔ USB/IP ↔ exporter ↔ device model

Injected one-way delay models the Ethernet hop. Every number is simulated,
software-only evidence of this implementation's overhead. It is not USB bus
timing, not hardware, and not a throughput promise for the ZERO 3E.
``baseline`` disables the optimizations (pipelining, coalescing, batched
submits, BOT sizing, descriptor cache); ``optimized`` is the default policy.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.usbsim.classes import MassStorageHost
from tests.usbsim.devices import (
    CompositeDevice,
    HidKeyboard,
    Loopback,
    MassStorage,
)
from tests.usbsim.harness import Rig, Transport, make_rig

from yoke.usb.ch9 import Setup
from yoke.usb.policy import ProxyPolicy

EVIDENCE = "simulated, software-only (Python model in one process); not hardware"
BASELINE = ProxyPolicy(
    in_depth_bulk=1,
    in_depth_interrupt=1,
    coalesce_in=False,
    bot_sizing=False,
    batch_submit=False,
    descriptor_cache=False,
)
OPTIMIZED = ProxyPolicy()
POLICIES = {"baseline": BASELINE, "optimized": OPTIMIZED}


def _rig(device: Any, policy: ProxyPolicy, latency: float, profile: str) -> Rig:
    rig = make_rig(device, policy=policy, transport=Transport(latency=latency, seed=1))
    rig.host.attach()
    rig.host.enumerate(profile)
    return rig


def enumeration(policy: ProxyPolicy, latency: float) -> dict[str, Any]:
    rig = make_rig(
        CompositeDevice(), policy=policy, transport=Transport(latency=latency, seed=1)
    )
    try:
        rig.host.attach()
        start = time.perf_counter()
        rig.host.enumerate("windows")
        elapsed = time.perf_counter() - start
        worst = max(r.elapsed for r in rig.host.records)
        assert rig.proxy is not None
        return {
            "enumeration_ms": round(elapsed * 1000, 2),
            "worst_request_ms": round(worst * 1000, 2),
            "forwarded_requests": rig.proxy.counters.forwarded,
            "deadline_violations": len(rig.host.deadline_misses),
        }
    finally:
        rig.close()


def interrupt_latency(
    policy: ProxyPolicy, latency: float, reports: int
) -> dict[str, Any]:
    device = HidKeyboard()
    rig = _rig(device, policy, latency, "linux")
    samples = []
    try:
        for index in range(reports):
            start = time.perf_counter()
            device.press(4 + index % 20) if index % 2 == 0 else device.release()
            rig.host.transfer_in(0x81, 8)
            samples.append(time.perf_counter() - start)
        samples.sort()
        return {
            "median_ms": round(statistics.median(samples) * 1000, 3),
            "p95_ms": round(samples[int(0.95 * (len(samples) - 1))] * 1000, 3),
        }
    finally:
        rig.close()


def bulk_in(policy: ProxyPolicy, latency: float, total: int) -> dict[str, Any]:
    device = Loopback()
    rig = _rig(device, policy, latency, "linux")
    try:
        device.emit(bytes(total))
        start = time.perf_counter()
        got = 0
        while got < total:
            got += len(rig.host.transfer_in(0x81, min(16384, total - got)))
        return _throughput(total, time.perf_counter() - start, rig)
    finally:
        rig.close()


def bulk_out(policy: ProxyPolicy, latency: float, total: int) -> dict[str, Any]:
    device = Loopback()
    rig = _rig(device, policy, latency, "linux")
    try:
        start = time.perf_counter()
        for offset in range(0, total, 16384):
            rig.host.transfer_out(0x02, bytes(min(16384, total - offset)))
        deadline = time.monotonic() + 30
        while len(device.received) < total and time.monotonic() < deadline:
            time.sleep(0.0005)
        return _throughput(total, time.perf_counter() - start, rig)
    finally:
        rig.close()


def msc(policy: ProxyPolicy, latency: float, total: int, write: bool) -> dict[str, Any]:
    blocks = max(total // 512, 128)
    device = MassStorage(blocks=blocks)
    rig = _rig(device, policy, latency, "linux")
    storage = MassStorageHost(rig.host)
    per_command = 128  # 64 KiB per SCSI command
    try:
        start = time.perf_counter()
        for lba in range(0, total // 512, per_command):
            count = min(per_command, total // 512 - lba)
            if write:
                storage.write10(lba, bytes(count * 512))
            else:
                storage.read10(lba, count)
        return _throughput(total, time.perf_counter() - start, rig)
    finally:
        rig.close()


def control_latency(policy: ProxyPolicy, latency: float, count: int) -> dict[str, Any]:
    rig = _rig(CompositeDevice(), policy, latency, "linux")
    samples = []
    try:
        for _ in range(count):
            start = time.perf_counter()
            rig.host.control(Setup(0xC0, 0x01, 0, 0, 12))
            samples.append(time.perf_counter() - start)
        return {"median_ms": round(statistics.median(samples) * 1000, 3)}
    finally:
        rig.close()


def _throughput(total: int, elapsed: float, rig: Rig) -> dict[str, Any]:
    assert rig.proxy is not None
    counters = rig.proxy.counters
    return {
        "mb_per_s": round(total / elapsed / 1e6, 3),
        "gadget_writes": counters.gadget_writes,
        "gadget_reads": counters.gadget_reads,
        "urbs": counters.in_urbs + counters.out_urbs,
        "usbip_writes": rig.proxy.conn.sends,
    }


def run(quick: bool) -> dict[str, Any]:
    latencies = [0.0, 0.0005] if quick else [0.0, 0.0005, 0.002]
    size = 64 * 1024 if quick else 1024 * 1024
    reports = 10 if quick else 100
    results: list[dict[str, Any]] = []

    def record(
        scenario: str, policy: str, latency: float, data: dict[str, Any], **extra: Any
    ) -> None:
        results.append(
            {
                "scenario": scenario,
                "policy": policy,
                "one_way_latency_ms": latency * 1000,
                **extra,
                **data,
            }
        )

    for latency in latencies:
        for name, policy in POLICIES.items():
            record("enumeration", name, latency, enumeration(policy, latency))
            record(
                "interrupt_in",
                name,
                latency,
                interrupt_latency(policy, latency, reports),
            )
            record(
                "control_vendor",
                name,
                latency,
                control_latency(policy, latency, reports),
            )
            record("bulk_in", name, latency, bulk_in(policy, latency, size))
            record("bulk_out", name, latency, bulk_out(policy, latency, size))
            record("msc_read", name, latency, msc(policy, latency, size, False))
            record("msc_write", name, latency, msc(policy, latency, size, True))
        record(
            "bulk_out",
            "optimized+out_window4",
            latency,
            bulk_out(replace(OPTIMIZED, out_window=4), latency, size),
        )
        for depth in (1, 4, 16, 32):
            record(
                "bulk_in_depth",
                "optimized",
                latency,
                bulk_in(replace(OPTIMIZED, in_depth_bulk=depth), latency, size),
                depth=depth,
            )
    return {
        "schema_version": 1,
        "evidence": EVIDENCE,
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
        },
        "parameters": {"bytes_per_throughput_run": size, "latency_samples": reports},
        "results": results,
    }


def _describe(row: dict[str, Any]) -> str:
    scenario = row["scenario"]
    if scenario == "enumeration":
        return (
            f"{row['enumeration_ms']} ms, {row['forwarded_requests']} forwarded, "
            f"{row['deadline_violations']} deadline misses"
        )
    if scenario == "interrupt_in":
        return f"median {row['median_ms']} ms, p95 {row['p95_ms']} ms"
    if scenario == "control_vendor":
        return f"median {row['median_ms']} ms"
    calls = row["gadget_writes"] + row["gadget_reads"]
    return (
        f"{row['mb_per_s']} MB/s ({calls} gadget calls, "
        f"{row['usbip_writes']} USB/IP writes)"
    )


def markdown(document: dict[str, Any]) -> str:
    rows = ["| Scenario | Policy | One-way delay | Result |", "|---|---|---|---|"]
    for row in document["results"]:
        name = row["scenario"] + (f" depth {row['depth']}" if "depth" in row else "")
        delay = f"{row['one_way_latency_ms']:g} ms"
        rows.append(f"| {name} | {row['policy']} | {delay} | {_describe(row)} |")
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--quick", action="store_true", help="small sizes (tests)")
    parser.add_argument("--markdown", action="store_true", help="print a table")
    args = parser.parse_args(argv)
    document = run(args.quick)
    if args.markdown:
        print(f"Evidence: {EVIDENCE}.\n")
        print(markdown(document))
    else:
        print(json.dumps(document, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
