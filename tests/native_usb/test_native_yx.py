"""YX payload codec (ADR 0028): C unit tests and an independent oracle.

``yx_test.c`` checks the C codec against itself and the ADR rules. This file
adds independent evidence: a pure-Python receiver written from ADR 0028
sections 4-5 alone (block stream, LZ4 raw-block decoding, xxh32 and the
per-direction cache rule) decodes the C encoder's wire bytes, and streams
built by hand in Python are fed to the C decoder. The format is therefore
verified against a second implementation, not only for self-consistency.
Host software evidence only; nothing touches USB hardware.
"""

from __future__ import annotations

import random
import struct
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SOURCES = [ROOT / "native/yusb/src/yx.c", ROOT / "native/third_party/lz4/lz4.c"]

BLOCK = 4096
FILL, LZ4, CACHE = 0x01, 0x02, 0x04
RAW_K, FILL_K, LZ4_K, REF_K = 0, 1, 2, 3
PROTOCOL = -1  # YUSB_ERR_PROTOCOL

C_CASES = [
    "offer",
    "accept",
    "agree",
    "bound",
    "init",
    "roundtrip",
    "large",
    "fill",
    "lz4",
    "skip",
    "cache",
    "cache_sync",
    "ref_reject",
    "violations",
    "fill_decode",
    "space",
    "split",
    "truncated",
    "determinism",
    "stats",
    "inserted",
    "collision",
    "iov",
    "frame",
    "free_safety",
]


@pytest.fixture(scope="session")
def yx_test(native_build: Callable[..., Path]) -> Path:
    return native_build("yx_test", [*SOURCES, HERE / "yx_test.c"])


@pytest.fixture(scope="session")
def yx_tool(native_build: Callable[..., Path]) -> Path:
    return native_build("yx_tool", [*SOURCES, HERE / "yx_tool.c"])


def test_c_case_list_is_complete(yx_test: Path) -> None:
    listed = subprocess.run(
        [yx_test, "list"], capture_output=True, text=True, check=True, timeout=30
    ).stdout.split()
    assert listed == C_CASES


@pytest.mark.parametrize("case", C_CASES)
def test_c(yx_test: Path, case: str) -> None:
    result = subprocess.run(
        [yx_test, case], capture_output=True, text=True, check=False, timeout=120
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"ok {case}"


# ---- the oracle: written from ADR 0028, shares no code with yx.c ----

M32 = 0xFFFFFFFF
P1, P2, P3, P4, P5 = 2654435761, 2246822519, 3266489917, 668265263, 374761393


def rotl(x: int, r: int) -> int:
    return ((x << r) | (x >> (32 - r))) & M32


def xxh32(data: bytes, seed: int = 0) -> int:
    n, i = len(data), 0
    if n >= 16:
        v = [(seed + P1 + P2) & M32, (seed + P2) & M32, seed, (seed - P1) & M32]
        while i + 16 <= n:
            lanes = struct.unpack_from("<4I", data, i)
            v = [
                rotl((a + w * P2) & M32, 13) * P1 & M32
                for a, w in zip(v, lanes, strict=True)
            ]
            i += 16
        h = rotl(v[0], 1) + rotl(v[1], 7) + rotl(v[2], 12) + rotl(v[3], 18)
    else:
        h = seed + P5
    h = (h + n) & M32
    while i + 4 <= n:
        h = rotl((h + struct.unpack_from("<I", data, i)[0] * P3) & M32, 17) * P4 & M32
        i += 4
    while i < n:
        h = rotl((h + data[i] * P5) & M32, 11) * P1 & M32
        i += 1
    h = (h ^ (h >> 15)) * P2 & M32
    h = (h ^ (h >> 13)) * P3 & M32
    return h ^ (h >> 16)


def lz4_block(src: bytes, size: int) -> bytes:
    """Decode one LZ4 raw block that must yield exactly ``size`` bytes."""
    out, i = bytearray(), 0

    def length(n: int) -> int:
        nonlocal i
        if n == 15:
            while True:
                b = src[i]
                i += 1
                n += b
                if b != 255:
                    break
        return n

    while True:
        token = src[i]
        i += 1
        lit = length(token >> 4)
        assert i + lit <= len(src)
        out += src[i : i + lit]
        i += lit
        if i == len(src):
            break
        offset = src[i] | src[i + 1] << 8
        i += 2
        match = length(token & 15) + 4
        assert 0 < offset <= len(out)
        pattern = bytes(out[-offset:])
        out += (pattern * (match // offset + 1))[:match]
        assert len(out) <= size
    assert len(out) == size
    return bytes(out)


class Receiver:
    """One direction's receiver per ADR 0028 sections 4 and 5."""

    def __init__(self, levers: int, cache_blocks: int) -> None:
        self.levers = levers
        self.slots: list[bytes | None] = [None] * cache_blocks
        self.inserted = 0

    def decode(self, wire: bytes, length: int) -> tuple[bytes, list[tuple[int, int]]]:
        out, at, kinds = bytearray(), 0, []
        while len(out) < length:
            size = min(BLOCK, length - len(out))
            kind, aux, wl = struct.unpack_from(">BBH", wire, at)
            body = wire[at + 4 : at + 4 + wl]
            at += 4 + wl
            assert aux == 0 and len(body) == wl
            if kind == RAW_K:
                assert wl == size
                block = body
            elif kind == FILL_K:
                assert self.levers & FILL and 1 <= wl <= 512 and wl <= size
                block = (body * (size // wl + 1))[:size]
            elif kind == LZ4_K:
                assert self.levers & LZ4 and 1 <= wl < size
                block = lz4_block(body, size)
            else:
                assert kind == REF_K and self.levers & CACHE and wl == 8
                assert size == BLOCK
                slot, check = struct.unpack(">II", body)
                cached = self.slots[slot]
                assert cached is not None and xxh32(cached) == check
                block = cached
            if kind in (RAW_K, LZ4_K) and size == BLOCK and self.levers & CACHE:
                self.slots[self.inserted % len(self.slots)] = block
                self.inserted += 1
            out += block
            kinds.append((kind, wl))
        assert at == len(wire)
        return bytes(out), kinds


def smallest_period(block: bytes) -> int:
    """The FILL unit the sender policy picks: a power of two below the length."""
    for u in (1, 2, 4, 8, 16, 32, 64, 128, 256, 512):
        if u < len(block) and block[u:] == block[:-u]:
            return u
    return 0


def frame(*parts: bytes) -> bytes:
    return b"".join(struct.pack(">I", len(p)) + p for p in parts)


def encode(
    tool: Path, levers: int, cache_blocks: int, payloads: list[bytes]
) -> list[bytes]:
    out = subprocess.run(
        [tool, "enc", str(levers), str(cache_blocks)],
        input=frame(*payloads),
        capture_output=True,
        check=True,
        timeout=60,
    ).stdout
    wires, at = [], 0
    while at < len(out):
        (n,) = struct.unpack_from(">I", out, at)
        wires.append(out[at + 4 : at + 4 + n])
        at += 4 + n
    return wires


def decode(
    tool: Path, levers: int, cache_blocks: int, streams: list[tuple[int, bytes]]
) -> list[tuple[int, int, bytes]]:
    data = b"".join(struct.pack(">II", n, len(w)) + w for n, w in streams)
    out = subprocess.run(
        [tool, "dec", str(levers), str(cache_blocks)],
        input=data,
        capture_output=True,
        check=True,
        timeout=60,
    ).stdout
    results, at = [], 0
    for length, _ in streams:
        if at == len(out):
            break
        status, used = struct.unpack_from(">iI", out, at)
        at += 8
        decoded = out[at : at + length] if status == 0 else b""
        at += len(decoded)
        results.append((status, used, decoded))
    return results


def text(rng: random.Random, n: int) -> bytes:
    words = b"the quick brown fox jumps over a lazy dog and USB bulk data".split()
    out = bytearray()
    while len(out) < n:
        out += rng.choice(words) + rng.choice([b" ", b" ", b"\n"])
    return bytes(out[:n])


def tiled(unit: bytes, n: int) -> bytes:
    return (unit * (n // len(unit) + 1))[:n]


def payload_class(name: str, rng: random.Random) -> list[bytes]:
    if name == "zeros":
        return [bytes(3 * BLOCK), bytes(5000), bytes(512)]
    if name == "ones":
        return [b"\xff" * (2 * BLOCK + 1)]
    if name == "periods":
        return [
            tiled(rng.randbytes(u - 1) + b"\0", 3 * BLOCK + 100) for u in (2, 4, 512)
        ]
    if name == "text":
        return [text(rng, n) for n in (10_000, 4096, 600)]
    if name == "random":
        return [rng.randbytes(n) for n in (40 * BLOCK, 100 * BLOCK + 7)]
    if name == "lengths":
        return [text(rng, n) for n in (1, 511, 512, 4095, 4096, 4097)]
    if name == "mixed":
        parts = [bytes(BLOCK), text(rng, BLOCK), rng.randbytes(BLOCK), b"\x55" * BLOCK]
        return [b"".join(rng.choice(parts) for _ in range(8)) + rng.randbytes(77)]
    assert name == "repeat"
    pool = [rng.randbytes(BLOCK) for _ in range(5)] + [text(rng, BLOCK)]
    return [b"".join(rng.choice(pool) for _ in range(6)) for _ in range(6)]


CLASSES = ["zeros", "ones", "periods", "text", "random", "lengths", "mixed", "repeat"]
LEVER_SETS = [(FILL, 0), (LZ4, 0), (FILL | LZ4, 0), (CACHE, 3), (FILL | LZ4 | CACHE, 8)]


def test_oracle_primitives() -> None:
    assert xxh32(b"") == 0x02CC5D05
    assert xxh32(b"a") == 0x550D7456
    assert xxh32(b"abc") == 0x32D153FF
    assert lz4_block(bytes([0x50]) + b"hello", 5) == b"hello"
    # "ab", then a 6-byte overlapping match at offset 2, then 5 literals.
    assert lz4_block(bytes([0x22]) + b"ab\x02\x00" + bytes([0x50]) + b"12345", 13) == (
        b"abababab12345"
    )


@pytest.mark.parametrize(("levers", "slots"), LEVER_SETS)
@pytest.mark.parametrize("name", CLASSES)
def test_encoder_output_decodes_independently(
    yx_tool: Path, levers: int, slots: int, name: str
) -> None:
    payloads = payload_class(name, random.Random(f"{name}/{levers}"))
    wires = encode(yx_tool, levers, slots, payloads)
    receiver = Receiver(levers, slots)
    refs = 0
    for payload, wire in zip(payloads, wires, strict=True):
        bound = len(payload) + 4 * -(-len(payload) // BLOCK)
        assert len(wire) <= bound
        if name == "random":
            assert len(wire) == bound  # incompressible: all RAW
        if name in ("zeros", "ones", "periods") and levers & FILL:
            assert len(wire) < len(payload) // 7
        decoded, kinds = receiver.decode(wire, len(payload))
        assert decoded == payload
        for index, (kind, wl) in enumerate(kinds):
            chunk = payload[index * BLOCK : (index + 1) * BLOCK]
            # Sender policy that is visible on the wire (ADR 0028 section 6).
            unit = smallest_period(chunk) if levers & FILL else 0
            assert (kind == FILL_K) == bool(unit) and (not unit or wl == unit)
            if kind == LZ4_K:
                assert wl < len(chunk) - len(chunk) // 8
            refs += kind == REF_K
    if name == "repeat" and levers & CACHE:
        assert refs > 0


def lz4_sequence(literals: bytes, offset: int = 0, match: int = 0) -> bytes:
    def extra(n: int) -> bytes:
        return b"\xff" * (n // 255) + bytes([n % 255])

    ml = match - 4
    token = min(len(literals), 15) << 4 | (min(ml, 15) if offset else 0)
    out = bytes([token]) + (extra(len(literals) - 15) if len(literals) >= 15 else b"")
    out += literals
    if offset:
        out += struct.pack("<H", offset) + (extra(ml - 15) if ml >= 15 else b"")
    return out


def block(kind: int, body: bytes, wire_len: int | None = None, aux: int = 0) -> bytes:
    return (
        struct.pack(">BBH", kind, aux, len(body) if wire_len is None else wire_len)
        + body
    )


def hand_stream() -> tuple[bytes, bytes]:
    """A stream built from ADR 0028 alone, with its expected decoding."""
    rng = random.Random(28)
    a = rng.randbytes(BLOCK)
    lit, tail = rng.randbytes(20), rng.randbytes(16)
    b = b"abc" * 1253 + b"a" + lit + lit[-1:] * 300 + tail
    assert len(b) == BLOCK
    lz = lz4_sequence(b"abc", 3, BLOCK - 3 - 20 - 300 - 16)
    lz += lz4_sequence(lit, 1, 300) + lz4_sequence(tail)
    end = rng.randbytes(100)
    wire = (
        block(RAW_K, a)
        + block(FILL_K, b"xyz")
        + block(LZ4_K, lz)
        + block(REF_K, struct.pack(">II", 0, xxh32(a)))
        + block(REF_K, struct.pack(">II", 1, xxh32(b)))
        + block(RAW_K, end)
    )
    return wire, a + tiled(b"xyz", BLOCK) + b + a + b + end


def test_hand_built_stream_decodes_in_c_and_in_the_oracle(yx_tool: Path) -> None:
    wire, expected = hand_stream()
    assert Receiver(FILL | LZ4 | CACHE, 2).decode(wire, len(expected))[0] == expected
    streams = [(len(expected), wire), (BLOCK, block(REF_K, wire[-8 - 104 : -104]))]
    first, second = decode(yx_tool, FILL | LZ4 | CACHE, 2, streams)
    assert first == (0, len(wire), expected)
    assert second == (0, 12, expected[4 * BLOCK : 5 * BLOCK])  # REF 1 again


# 600 x "z": one literal, an overlapping match, nine final literals.
Z600 = lz4_sequence(b"z", 1, 590) + lz4_sequence(b"z" * 9)


def test_hand_built_lz4_control(yx_tool: Path) -> None:
    assert lz4_block(Z600, 600) == b"z" * 600
    assert decode(yx_tool, LZ4, 0, [(600, block(LZ4_K, Z600))]) == [
        (0, 4 + len(Z600), b"z" * 600)
    ]


def reject_cases() -> dict[str, tuple[int, int, list[tuple[int, bytes]]]]:
    rng = random.Random(4)
    a = rng.randbytes(BLOCK)
    ref = struct.pack(">II", 0, xxh32(a))
    raw_a = (BLOCK, block(RAW_K, a))
    all_levers = FILL | LZ4 | CACHE
    return {
        "unknown kind": (all_levers, 2, [(600, block(4, b"x"))]),
        "aux": (all_levers, 2, [(600, block(RAW_K, a[:600], aux=1))]),
        "raw short": (all_levers, 2, [(600, block(RAW_K, a[:599]))]),
        "fill 513": (all_levers, 2, [(BLOCK, block(FILL_K, a[:513]))]),
        "fill > block": (all_levers, 2, [(100, block(FILL_K, a[:101]))]),
        "fill not agreed": (LZ4 | CACHE, 2, [(600, block(FILL_K, b"\0"))]),
        "lz4 not agreed": (FILL | CACHE, 2, [(600, block(LZ4_K, Z600))]),
        "lz4 not smaller": (all_levers, 2, [(600, block(LZ4_K, a[:600]))]),
        "lz4 decodes short": (all_levers, 2, [(601, block(LZ4_K, Z600))]),
        "lz4 decodes long": (all_levers, 2, [(599, block(LZ4_K, Z600))]),
        "lz4 no last literals": (
            all_levers,
            2,
            [(600, block(LZ4_K, lz4_sequence(b"z", 1, 599)))],
        ),
        "lz4 garbage": (all_levers, 2, [(600, block(LZ4_K, b"\xff" * 40))]),
        "ref empty": (all_levers, 2, [(BLOCK, block(REF_K, ref))]),
        "ref check": (
            all_levers,
            2,
            [raw_a, (BLOCK, block(REF_K, struct.pack(">II", 0, xxh32(a) ^ 1)))],
        ),
        "ref slot": (
            all_levers,
            2,
            [raw_a, (BLOCK, block(REF_K, struct.pack(">II", 2, 0)))],
        ),
        "ref unwritten": (
            all_levers,
            2,
            [raw_a, (BLOCK, block(REF_K, struct.pack(">II", 1, xxh32(bytes(BLOCK)))))],
        ),
        "ref partial": (all_levers, 2, [raw_a, (600, block(REF_K, ref))]),
        "ref not agreed": (FILL | LZ4, 0, [raw_a, (BLOCK, block(REF_K, ref))]),
        "truncated": (all_levers, 2, [(BLOCK, block(RAW_K, a)[:-1])]),
    }


@pytest.mark.parametrize("case", list(reject_cases()))
def test_c_decoder_rejects_violations(yx_tool: Path, case: str) -> None:
    levers, slots, streams = reject_cases()[case]
    results = decode(yx_tool, levers, slots, streams)
    assert [status for status, _, _ in results[:-1]] == [0] * (len(streams) - 1)
    assert results[-1][0] == PROTOCOL
