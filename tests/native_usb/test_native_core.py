"""Deterministic C unit tests of the native core (core_test.c) under ASan/UBSan.

Covers what the simulator cannot pin exactly: fence backoff arithmetic,
unlink grace and tombstones on a virtual clock, IN-stall parking backoff
(50, 100, 200 ms), the DMA buffer layout hook, the export core's
resubmission of URBs a flushing backend ended, and the Raw Gadget ABI
against <linux/usb/raw_gadget.h> when the host has it.
"""

from __future__ import annotations

import subprocess

import pytest
from conftest import HERE, USB, build, library_sources

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


@pytest.mark.parametrize("sizing", ["linux", "mcu"])
def test_core_unit_tests_pass(sizing: str) -> None:
    sources = [
        HERE / "core_test.c",
        *library_sources(),
        USB / "linux" / "rawgadget.c",
    ]
    extra = MCU if sizing == "mcu" else ()
    binary = build(f"core-test-{sizing}", sources, std="gnu11", extra=extra)
    result = subprocess.run(
        [str(binary)], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "all core tests passed" in result.stdout
    print(result.stdout.splitlines()[-2])  # the footprint line, for the record
