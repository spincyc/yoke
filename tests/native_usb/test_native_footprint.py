"""yusb_proxy_footprint against counted allocations (footprint_test.c).

Every heap byte the core holds is counted through linker wrapping (no
sanitizer), at Linux and at ESP32-S3 (lane C) sizing, with and without a YX
offer the exporter accepts; an injected allocation failure in the YX set-up
must leak nothing and end the session as YUSB_END_YX_UNAVAILABLE.
"""

from __future__ import annotations

import subprocess

import pytest
from conftest import HERE, build, library_sources
from test_native_core import MCU

WRAP = "-Wl,--wrap=malloc,--wrap=calloc,--wrap=aligned_alloc,--wrap=realloc,--wrap=free"


@pytest.mark.parametrize("sizing", ["linux", "mcu"])
def test_proxy_footprint_matches_counted_allocations(sizing: str) -> None:
    sources = [HERE / "footprint_test.c", *library_sources()]
    extra = (*(MCU if sizing == "mcu" else ()), WRAP)
    binary = build(
        f"footprint-test-{sizing}", sources, std="gnu11", sanitize=False, extra=extra
    )
    result = subprocess.run(
        [str(binary)], capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "all footprint tests passed" in result.stdout
    print(result.stdout)
