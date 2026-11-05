"""Differential tests of the native frame layer against yoke.core.frames.

For random and mutated datagrams the C decoder must reach the reference's
verdict (accept or the same drop counter) and, when it accepts, the same
type, origin, seq, ts bits, canonical body and replay fingerprint. The C
encoder and canonical writer must be byte-identical to the reference.
HOST ONLY: sanitized driver, deterministic seeds, bounded run time.
"""

from __future__ import annotations

import json
import random
from collections import Counter
from pathlib import Path

import pytest

from tests.native_ywp import gen
from tests.native_ywp.conftest import C_DIR, build_exe
from tests.native_ywp.harness import FrameTool, reference
from yoke.core.frames import EncodeError, canonical_bytes, encode
from yoke.core.types import FrameType


@pytest.fixture(scope="session")
def frame_tool(native_dir: Path) -> FrameTool:
    return FrameTool(build_exe(native_dir / "frame_check", C_DIR / "frame_check.c"))


def _assert_agree(
    frame_tool: FrameTool, cases: list[tuple[bytes, bytes]]
) -> Counter[str]:
    got = frame_tool.decode(cases)
    verdicts: Counter[str] = Counter()
    bad = []
    for (psk, raw), observed in zip(cases, got, strict=True):
        expected = reference(raw, psk)
        verdicts[expected[1] if expected[0] == "drop" else "accept"] += 1
        if observed != expected:
            bad.append((raw, observed, expected))
    shown = "\n".join(f"{r[:200]!r}\n  C  {o}\n  py {e}" for r, o, e in bad[:3])
    assert not bad, f"{len(bad)} of {len(cases)} disagree:\n{shown}"
    return verdicts


def test_random_and_mutated_frames_agree(frame_tool: FrameTool) -> None:
    verdicts = _assert_agree(frame_tool, gen.corpus(seed=2026, count=20000))
    # The corpus must exercise every verdict, or agreement proves little.
    for verdict in ("accept", "malformed", "version", "auth", "oversize"):
        assert verdicts[verdict] > 50, verdicts


def test_edge_cases_agree(frame_tool: FrameTool) -> None:
    base = gen.frame(random.Random(1), gen.PSK)
    envelope = (
        '{"v":1,"t":"bye","id":"thirdparty","seq":1793000000000000015,"ts":%s,'
        '"body":%s,"sig":""}'
    )
    texts = [
        envelope % (ts, body)
        for ts in (
            "0",
            "-0",
            "1e308",
            "1e309",
            "-1.0e309",
            "1" + "0" * 400,
            "1" + "0" * 308,
            "1797693134862315807" + "0" * 290,
            "5e-324",
            "1E-400",
            "true",
            '"1"',
            "null",
            "1.5",
            "17930000001e-1",
        )
        for body in (
            "{}",
            "[]",
            '{"a":1,"a":2}',
            '{"a":1,"\\u0061":2}',
            "[" * 31 + "]" * 31,
            "[" * 30 + "]" * 30,
            '{"x":NaN}',
            '{"x":"\\ud800"}',
            '{"x":"\\ud83d\\ude00"}',
            '{"x":"\\udc00"}',
            '{"\\u00e9":1,"\\u00E9":2}',
            '{"x":-0.0}',
            '{"x":1e400}',
        )
    ]
    texts += [
        '{"v":1,"v":1,"t":"bye"}',
        "",
        " ",
        "{}",
        "[]",
        '"x"',
        "﻿{}",
        '{"v":true,"t":"bye","id":"a","seq":1,"ts":0,"body":{},"sig":""}',
        '{"v":1.0,"t":"bye","id":"a","seq":1,"ts":0,"body":{},"sig":""}',
        '{"v":2,"t":"nope","id":"a","seq":1,"ts":0,"body":{},"sig":""}',
        '{"v":1,"t":"bye","id":"A","seq":1,"ts":0,"body":{},"sig":""}',
        '{"v":1,"t":"bye","id":"a","seq":0,"ts":0,"body":{},"sig":""}',
        '{"v":1,"t":"bye","id":"a","seq":9223372036854775808,"ts":0,"body":{},"sig":""}',
        '{"v":1,"t":"bye","id":"a","seq":true,"ts":0,"body":{},"sig":""}',
        '{"v":1,"t":"\\u0062ye","id":"\\u0061","seq":1,"ts":0,"body":{},"sig":""}',
        '{"v":1,"t":"bye","id":"a","seq":1,"ts":0,"body":{},"sig":1}',
        '{"v":1,"t":"bye","id":"a","seq":1,"ts":0,"body":{},"sig":"\\u0041"}',
        '{"v":1,"t":"bye","id":"a","seq":1,"ts":0,"body":{},"sig":"' + "A" * 64 + '"}',
        '{"v":1,"t":"bye","id":"a","seq":1,"ts":0,"body":{},"sig":""} x',
        '{"v":1,"t":"bye","id":"a","seq":1,"ts":0,"body":{},"sig":""}\n',
    ]
    cases = [(b"", text.encode("utf-8")) for text in texts]
    cases += [(gen.PSK, text.encode("utf-8")) for text in texts]
    cases += [(gen.PSK, base[:cut]) for cut in range(len(base))]
    cases += [
        (gen.PSK, b" " * (1200 - len(base)) + base),
        (gen.PSK, b" " * (1201 - len(base)) + base),
    ]
    _assert_agree(frame_tool, cases)


def test_encoder_is_byte_identical(frame_tool: FrameTool) -> None:
    rng = random.Random(7)
    items = []
    for _ in range(3000):
        frame_type = rng.choice(gen.TYPES)
        body = {
            gen.random_text(rng, 5): gen.random_value(rng, 1)
            for _ in range(rng.randrange(4))
        }
        ts = gen.random_float(rng)
        origin = rng.choice(("rack-1", "a", "z" * 32, "Bad", "-x"))
        seq = rng.choice((1, 2**63 - 1, 2**63, 0, rng.randrange(1, 2**63)))
        text = gen.dump(rng, body, rng.choice(("canonical", "pretty", "escapes")))
        items.append((frame_type, origin, seq, ts, text.encode("utf-8"), body))
    for psk in (gen.PSK, b""):
        got = frame_tool.encode(psk, [item[:5] for item in items])
        for (frame_type, origin, seq, ts, _, body), out in zip(items, got, strict=True):
            try:
                want: bytes | None = encode(
                    FrameType(frame_type), origin, seq, ts, body, psk
                )
            except EncodeError:
                want = None
            assert out == want, (frame_type, origin, seq, ts, body)


def test_canonical_writer_is_byte_identical(frame_tool: FrameTool) -> None:
    rng = random.Random(11)
    values = [gen.random_value(rng) for _ in range(5000)]
    texts = [
        gen.dump(
            rng,
            value,
            rng.choice(("ascii", "escapes", "pretty", "unsorted")),
            sort=False,
        ).encode("utf-8")
        for value in values
    ]
    got = frame_tool.canonical(texts)
    for text, out in zip(texts, got, strict=True):
        assert out == canonical_bytes(json.loads(text)), text[:200]


def test_overhead_matches_the_reference_announce_budget(frame_tool: FrameTool) -> None:
    for origin in ("a", "rack-1", "x" * 32):
        unsigned = {
            "v": 1,
            "t": "ann",
            "id": origin,
            "seq": 2**63 - 1,
            "ts": -1.2345678901234567e308,
            "body": {},
            "sig": "0" * 64,
        }
        assert frame_tool.overhead(origin) == len(canonical_bytes(unsigned)) - 2
