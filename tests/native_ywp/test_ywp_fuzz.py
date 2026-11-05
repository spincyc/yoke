"""Bounded libFuzzer runs of the native library, then a Python differential.

``fuzz_decode`` (frame decode and canonical writer invariants) and
``fuzz_node`` (the state machine under arbitrary input programs) run under
ASan and UBSan for YWP_FUZZ_SECONDS each (default 5; `make ywp-fuzz` runs
longer). The decode corpus, seeded with signed reference frames and grown by
the fuzzer, is then decoded by C and by yoke.core.frames under the fuzz key
and in open mode: every verdict and field must agree. HOST ONLY; skipped
without clang's libFuzzer.
"""

from __future__ import annotations

import os
import random
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.native_ywp import gen
from tests.native_ywp.conftest import C_DIR, HERE, INCLUDE, build_exe, library_sources
from tests.native_ywp.harness import FrameTool, reference

FUZZ_KEY = b"fuzz-site-key-0123456789abcdef!!"
SECONDS = int(os.environ.get("YWP_FUZZ_SECONDS", "5"))


def _build(target: str, out: Path) -> Path:
    clang = shutil.which("clang")
    if clang is None:
        pytest.skip("clang with libFuzzer unavailable")
    binary = out / target
    result = subprocess.run(
        [
            clang,
            "-std=c11",
            "-g",
            "-O1",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-fsanitize=fuzzer,address,undefined",
            "-fno-sanitize-recover=all",
            "-I",
            str(INCLUDE),
            str(HERE / "fuzz" / f"{target}.c"),
            *(str(path) for path in library_sources()),
            "-o",
            str(binary),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        if "fuzzer" in result.stderr and "not found" in result.stderr:
            pytest.skip("libFuzzer runtime unavailable")
        raise AssertionError(result.stderr)
    return binary


def _fuzz(binary: Path, corpus: Path, max_len: int) -> None:
    result = subprocess.run(
        [
            str(binary),
            f"-max_total_time={SECONDS}",
            f"-max_len={max_len}",
            "-print_final_stats=1",
            f"-artifact_prefix={corpus.parent}/",
            str(corpus),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=SECONDS + 120,
    )
    crashes = sorted(corpus.parent.glob("crash-*")) + sorted(
        corpus.parent.glob("leak-*")
    )
    found = "".join(f"\n{path.name}: {path.read_bytes().hex()}" for path in crashes)
    assert result.returncode == 0 and not crashes, result.stderr[-4000:] + found


def test_fuzz_decode_then_differential(native_dir: Path) -> None:
    out = native_dir / "fuzz-decode"
    corpus = out / "corpus"
    corpus.mkdir(parents=True)
    rng = random.Random(29)
    for index in range(400):
        raw = gen.frame(rng, rng.choice((FUZZ_KEY, b"")))
        (corpus / f"seed-{index}").write_bytes(
            gen.mutate(rng, raw) if index % 3 else raw
        )
    _fuzz(_build("fuzz_decode", out), corpus, 1400)

    tool = FrameTool(
        build_exe(native_dir / "frame_check_fuzz", C_DIR / "frame_check.c")
    )
    inputs = [path.read_bytes() for path in sorted(corpus.iterdir())]
    assert len(inputs) >= 400
    cases = [(key, raw) for key in (FUZZ_KEY, b"") for raw in inputs]
    observed = tool.decode(cases)
    bad = [
        (raw, got)
        for (key, raw), got in zip(cases, observed, strict=True)
        if got != reference(raw, key)
    ]
    assert not bad, f"{len(bad)} C/Python disagreements, first: {bad[0]!r}"[:2000]
    accepted = sum(1 for got in observed if got[0] == "ok")
    assert accepted > 100, accepted


def test_fuzz_node(native_dir: Path) -> None:
    out = native_dir / "fuzz-node"
    corpus = out / "corpus"
    corpus.mkdir(parents=True)
    rng = random.Random(31)
    for index in range(64):
        (corpus / f"seed-{index}").write_bytes(rng.randbytes(rng.randrange(16, 2048)))
    _fuzz(_build("fuzz_node", out), corpus, 4096)
