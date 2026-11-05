"""Bounded decode cost of forged frames (cold review 2026-11-05).

A keyless host can send 1200-byte frames whose bodies are arrays of the
costliest number literals; every one is rejected (bad signature) only after
the legacy canonical message is computed. Each such frame must cost at most
a small multiple of the same frame filled with integers, so number
conversion cannot amplify a flood. Measured with the optimized benchmark
(tests/native_ywp/c/bench.c); a ratio, so the bound holds on any host.
HOST ONLY.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from tests.native_ywp.conftest import C_DIR, build_exe

#: Worst float-packed frame over the integer-packed frame, with margin for
#: timing noise; the measured ratio is in native/ywp/README.md.
MAX_RATIO = 3.0


def test_forged_float_frames_cost_a_small_multiple_of_integer_frames(
    native_dir: Path,
) -> None:
    binary = build_exe(
        native_dir / "bench",
        C_DIR / "bench.c",
        sanitize=False,
        optimize="-O2",
        flags=("-D_DEFAULT_SOURCE",),
    )
    ratios: dict[str, float] = {}
    for _ in range(3):  # the best of three runs damps scheduler noise
        result = subprocess.run(
            [str(binary)], capture_output=True, text=True, check=True, timeout=300
        )
        forged = json.loads(result.stdout)["forged_ns"]
        base = forged.pop("0")
        for literal, ns in forged.items():
            ratios[literal] = min(ratios.get(literal, float("inf")), ns / base)
    assert max(ratios.values()) <= MAX_RATIO, ratios
