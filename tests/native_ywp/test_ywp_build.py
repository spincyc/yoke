"""The native library builds as a plain CMake library and registers as an
ESP-IDF component (with a stand-in for idf_component_register; the real
ESP-IDF build is the firmware lane's). HOST ONLY; skipped without cmake.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.native_ywp.conftest import HERE, NATIVE, library_sources


def _cmake(*args: str) -> None:
    cmake = shutil.which("cmake")
    if cmake is None:
        pytest.skip("cmake unavailable")
    result = subprocess.run([cmake, *args], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


def test_plain_cmake_static_library(tmp_path: Path) -> None:
    _cmake("-S", str(NATIVE), "-B", str(tmp_path), "-DCMAKE_BUILD_TYPE=Release")
    _cmake("--build", str(tmp_path), "--parallel")
    assert (tmp_path / "libywp.a").is_file()


def test_esp_idf_component_registers_every_source(tmp_path: Path) -> None:
    _cmake("-S", str(HERE / "cmake"), "-B", str(tmp_path), f"-DYWP_DIR={NATIVE}")
    recorded = dict(
        line.split("=", 1)
        for line in (tmp_path / "registered.txt").read_text().splitlines()
    )
    sources = {NATIVE / path for path in recorded["SRCS"].split(";")}
    assert sources == set(library_sources())
    assert recorded["INCLUDE_DIRS"] == "include"
