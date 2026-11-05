"""C unit tests of native/ywp, compiled with ASan and UBSan (tests/native_ywp/c).

The C program checks crypto known answers, the durable state blob, JSON
accessors, the jitter generator, the driver route queue and node API
contracts; it exits nonzero on any failed check or sanitizer report.
HOST ONLY.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from tests.native_ywp.conftest import C_DIR, build_exe


def test_c_unit_suite(native_dir: Path) -> None:
    binary = build_exe(native_dir / "unit", C_DIR / "unit.c")
    result = subprocess.run(
        [str(binary)], capture_output=True, text=True, check=False, timeout=120
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().endswith("0 failures"), result.stdout
