"""Build helpers for the native USB core tests (ADR 0027).

C test programs are compiled by pytest with the host compiler, strict
warnings and AddressSanitizer/UndefinedBehaviorSanitizer, following
``tests/firmware_ir/conftest.py``. Everything here is host software
evidence; nothing touches USB hardware.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from collections.abc import Callable, Iterable
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NATIVE = ROOT / "native"
USB = NATIVE / "yusb"
INCLUDE = USB / "include"
LZ4 = NATIVE / "third_party" / "lz4"
HERE = Path(__file__).resolve().parent
WARNINGS = ("-Wall", "-Wextra", "-Werror", "-Wshadow", "-Wstrict-prototypes")
SANITIZE = ("-fsanitize=address,undefined", "-fno-sanitize-recover=all")
OUT = ROOT / ".scratch" / "native-tests"


def compiler() -> str:
    for name in (os.environ.get("CC", ""), "cc", "gcc", "clang"):
        if name and shutil.which(name):
            return name
    pytest.skip("host C compiler unavailable")


def library_sources(*, linux: bool = False) -> list[Path]:
    """The portable core (and optionally the Linux worker modules)."""
    sources = sorted((USB / "src").glob("*.c"))
    sources += [p for p in (LZ4 / "lz4.c",) if p.exists()]
    if linux:
        sources += sorted((USB / "linux").glob("*.c"))
        sources = [p for p in sources if p.name != "worker_main.c"]
    return sources


def build(
    name: str,
    sources: Iterable[Path],
    *,
    extra: Iterable[str] = (),
    sanitize: bool = True,
    std: str = "c11",
) -> Path:
    """Compile once per content hash into .scratch/native-tests."""
    files = [Path(s) for s in sources]
    flags = [
        f"-std={std}",
        "-O1",
        "-g",
        "-fno-omit-frame-pointer",
        *WARNINGS,
        *(SANITIZE if sanitize else ()),
        "-I",
        str(INCLUDE),
        "-I",
        str(USB / "linux"),
        "-I",
        str(LZ4),
        *extra,
    ]
    digest = hashlib.sha256(" ".join(flags).encode())
    for path in files:
        digest.update(path.read_bytes())
    headers = [
        *INCLUDE.glob("*.h"),
        *(USB / "src").glob("*.h"),
        *(USB / "linux").glob("*.h"),
    ]
    for header in sorted(headers):
        digest.update(header.read_bytes())
    OUT.mkdir(parents=True, exist_ok=True)
    target = OUT / f"{name}-{digest.hexdigest()[:12]}"
    if target.exists():
        return target
    result = subprocess.run(
        [compiler(), *flags, *(str(f) for f in files), "-o", str(target), "-lpthread"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise AssertionError(f"compiling {name} failed:\n{result.stderr}")
    return target


@pytest.fixture(scope="session")
def native_build() -> Callable[..., Path]:
    return build
