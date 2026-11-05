"""Deterministic C unit tests of the export core (export_test.c).

Run under ASan/UBSan at Linux and ESP32-S3 sizing, and once more without a
sanitizer and with the allocator wrapped (``-DCOUNT_ALLOCS``): live heap
bytes are then counted against ``yusb_export_footprint()`` and the YX codec
footprints, and allocation failures are injected into the YX setup.
"""

from __future__ import annotations

import subprocess

import pytest
from conftest import HERE, build, library_sources

# The ESP32-S3 firmware's compile-time bounds (lane C), applied to every unit.
MCU = (
    "-DMCU_SIZED",
    "-DYUSB_MAX_CONFIGS=4",
    "-DYUSB_MAX_ALTS=32",
    "-DYUSB_MAX_CONFIG_EPS=32",
    "-DYUSB_MAX_STRINGS=48",
    "-DYUSB_PRESENTATION_ARENA=16384",
    "-DYUSB_MISS_ARENA=1024",
    "-DYUSB_MAX_BOTS=2",
)

WRAP = "-Wl,--wrap=malloc,--wrap=calloc,--wrap=free,--wrap=aligned_alloc,--wrap=realloc"


def run(name: str, extra: tuple[str, ...], *, sanitize: bool = True) -> str:
    sources = [HERE / "export_test.c", *library_sources()]
    binary = build(name, sources, std="gnu11", extra=extra, sanitize=sanitize)
    result = subprocess.run(
        [str(binary)], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "all export tests passed" in result.stdout
    return result.stdout


@pytest.mark.parametrize("sizing", ["linux", "mcu"])
def test_export_unit_tests_pass(sizing: str) -> None:
    stdout = run(f"export-test-{sizing}", MCU if sizing == "mcu" else ())
    print(stdout.splitlines()[-2])  # the footprint line, for the record


def test_export_footprint_matches_counted_allocations() -> None:
    stdout = run("export-test-counted", ("-DCOUNT_ALLOCS", WRAP), sanitize=False)
    counted = [line for line in stdout.splitlines() if line.startswith("counted ")]
    assert len(counted) == 10
    print("\n".join(counted))
