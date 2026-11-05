"""The native frame layer against the published YWP/1 authentication vectors.

Every vector in tests/vectors/ywp1-auth.json must give the same verdict in
C as the reference, accepted frames must decode to the vector's fields, and
the C encoder must reproduce every reference sender datagram byte for byte.
HOST ONLY (sanitized driver, no device).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.native_ywp.conftest import C_DIR, build_exe
from tests.native_ywp.harness import FrameTool, bits, hexed, reference
from yoke.core.frames import canonical_bytes

VECTORS_PATH = Path(__file__).resolve().parents[1] / "vectors" / "ywp1-auth.json"
DOC: dict[str, Any] = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))
KEYS: dict[str, bytes] = {name: bytes.fromhex(key) for name, key in DOC["keys"].items()}
VECTORS: list[dict[str, Any]] = DOC["vectors"]


@pytest.fixture(scope="session")
def frame_tool(native_dir: Path) -> FrameTool:
    return FrameTool(build_exe(native_dir / "frame_check", C_DIR / "frame_check.c"))


def test_all_vectors_match_expectation_and_reference(frame_tool: FrameTool) -> None:
    assert len(VECTORS) == 27
    items = [(KEYS[v["key"]], v["datagram"].encode("utf-8")) for v in VECTORS]
    observed = frame_tool.decode(items)
    for vector, (psk, raw), got in zip(VECTORS, items, observed, strict=True):
        name = vector["name"]
        assert got == reference(raw, psk), name
        if vector["expect"] != "accept":
            assert got == ("drop", vector["expect"]), name
            continue
        frame = vector["frame"]
        assert got[:5] == (
            "ok",
            frame["t"],
            frame["id"],
            str(frame["seq"]),
            bits(float(frame["ts"])),
        ), name
        assert got[5] == hexed(canonical_bytes(frame["body"])), name


def test_reference_sender_vectors_are_reproduced_by_the_encoder(
    frame_tool: FrameTool,
) -> None:
    reference_vectors = [v for v in VECTORS if v.get("reference")]
    assert {v["frame"]["t"] for v in reference_vectors} == {
        "ann",
        "int",
        "ack",
        "evt",
        "bye",
    }
    for vector in reference_vectors:
        frame = vector["frame"]
        body = canonical_bytes(frame["body"])
        (encoded,) = frame_tool.encode(
            KEYS[vector["key"]],
            [(frame["t"], frame["id"], frame["seq"], float(frame["ts"]), body)],
        )
        assert encoded == vector["datagram"].encode("utf-8"), vector["name"]
