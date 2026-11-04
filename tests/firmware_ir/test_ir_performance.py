"""Host-only cost of parsing, compiling and encoding (HOST SIMULATION).

Bounds are deliberately loose (orders of magnitude above measured values)
so CI noise cannot fail them; they catch accidental quadratic behaviour.
The figures say nothing about ESP32-S3 timing, which is a bench item.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

LIMITS_NS = {
    "parse_nec_72": 200_000,
    "compile_nec_72": 200_000,
    "parse_compile_max_520": 2_000_000,
    "encode_per_symbol": 1_000,
    "line_to_first_symbol": 500_000,
    "smooth_repeat_fold": 200_000,
    "getversion_line": 100_000,
    "boot_selftest": 2_000_000,
}


def test_host_costs_are_bounded(ir_bench_binary: Path) -> None:
    result = subprocess.run(
        [str(ir_bench_binary)], check=True, capture_output=True, text=True, timeout=120
    )
    report = json.loads(result.stdout)
    assert report["label"].startswith("HOST SIMULATION")
    for name, limit in LIMITS_NS.items():
        measured = report["results"][name]
        assert measured["iterations"] >= 1000
        assert measured["median_ns"] < limit, (name, measured)
    # The smooth-repeat fast path skips compilation entirely.
    results = report["results"]
    assert (
        results["smooth_repeat_fold"]["median_ns"]
        < results["line_to_first_symbol"]["median_ns"]
    )
    assert report["program_bytes"] < 4096
