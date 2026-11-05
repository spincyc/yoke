"""Build helpers for the native YWP/1 library (native/ywp) host tests.

Executables are compiled with the host C compiler, warnings as errors and
AddressSanitizer plus UndefinedBehaviorSanitizer (any finding aborts).
The ctypes shared library used by the Python-side harnesses cannot carry
ASan into an uninstrumented interpreter, so it is built with UBSan only;
the same inputs also run through sanitized executables. HOST ONLY: nothing
here touches ESP-IDF, mbedTLS or a device.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NATIVE = ROOT / "native" / "ywp"
INCLUDE = NATIVE / "include"
HERE = Path(__file__).resolve().parent
C_DIR = HERE / "c"
WARNINGS = (
    "-Wall",
    "-Wextra",
    "-Werror",
    "-Wshadow",
    "-Wpedantic",
    "-Wstrict-prototypes",
    "-Wmissing-prototypes",
    "-Wvla",
)
SANITIZE = (
    "-fsanitize=address,undefined",
    "-fno-sanitize-recover=all",
    "-fno-omit-frame-pointer",
)


def library_sources() -> list[Path]:
    return sorted((NATIVE / "src").glob("*.c"))


def compiler() -> str:
    found = shutil.which("cc")
    if found is None:
        pytest.skip("host C compiler unavailable")
    return found


#: One build per distinct command line and session; later requests reuse it.
_BUILT: dict[tuple[str, ...], Path] = {}


def _compile(args: list[str], target: Path) -> Path:
    key = tuple(args[:-1])  # everything but the output path
    built = _BUILT.get(key)
    if built is not None and built.exists():
        return built
    result = subprocess.run(
        [*args, str(target)], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise AssertionError(
            "native build failed:\n" + " ".join(args) + "\n" + result.stderr
        )
    _BUILT[key] = target
    return target


def build_exe(
    target: Path,
    *mains: Path,
    sanitize: bool = True,
    optimize: str = "-O1",
    flags: Sequence[str] = (),
) -> Path:
    """Compile ``mains`` with every library source into ``target``."""
    args = [
        compiler(),
        "-std=c11",
        optimize,
        "-g",
        *WARNINGS,
        *(SANITIZE if sanitize else ()),
        *flags,
        "-I",
        str(INCLUDE),
        *(str(path) for path in mains),
        *(str(path) for path in library_sources()),
        "-o",
    ]
    return _compile(args, target)


def build_shared(target: Path, *, flags: Sequence[str] = ()) -> Path:
    """The library as a shared object for ctypes (UBSan, no ASan)."""
    args = [
        compiler(),
        "-std=c11",
        "-O2",
        "-g",
        "-fPIC",
        "-shared",
        *WARNINGS,
        "-fsanitize=undefined",
        "-fno-sanitize-recover=all",
        *flags,
        "-I",
        str(INCLUDE),
        *(str(path) for path in library_sources()),
        "-o",
    ]
    return _compile(args, target)


@pytest.fixture(scope="session")
def native_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("native-ywp")
