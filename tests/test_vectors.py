"""Conformance check for the published YWP/1 authentication vectors.

``tests/vectors/ywp1-auth.json`` is the interop contract for third-party
implementers (doc/design/PROTOCOL.md, Authentication). This suite proves
the reference implementation agrees with every vector, and that the
reference vectors cannot drift from the reference sender.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

import pytest

from yoke.core.frames import FrameDrop, canonical_bytes, decode, encode, signed_message
from yoke.core.types import DropReason, Frame, FrameType

VECTORS_PATH = Path(__file__).parent / "vectors" / "ywp1-auth.json"
DOC: dict[str, Any] = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))
KEYS: dict[str, bytes] = {name: bytes.fromhex(key) for name, key in DOC["keys"].items()}
VECTORS: list[dict[str, Any]] = DOC["vectors"]
VERDICTS = {"accept"} | {reason.value for reason in DropReason}


def _verdict(raw: bytes, psk: bytes) -> str:
    try:
        decode(raw, psk)
    except FrameDrop as drop:
        return drop.reason.value
    return "accept"


def test_vector_file_shape() -> None:
    assert DOC["schema"] == 1
    assert KEYS["open"] == b""
    names = [vector["name"] for vector in VECTORS]
    assert len(names) == len(set(names))
    for vector in VECTORS:
        assert vector["key"] in KEYS
        assert vector["expect"] in VERDICTS
        assert isinstance(vector["datagram"], str)
        assert vector["note"]
    expects = {vector["expect"] for vector in VECTORS}
    assert {"accept", "auth", "malformed", "oversize"} <= expects
    paths = {vector.get("path") for vector in VECTORS if vector["expect"] == "accept"}
    assert {"transmitted", "legacy-canonical"} <= paths
    reference_types = {
        vector["frame"]["t"] for vector in VECTORS if vector.get("reference")
    }
    assert reference_types == {t.value for t in FrameType}


@pytest.mark.parametrize("vector", VECTORS, ids=[v["name"] for v in VECTORS])
def test_vector(vector: dict[str, Any]) -> None:
    raw = vector["datagram"].encode("utf-8")
    psk = KEYS[vector["key"]]
    assert _verdict(raw, psk) == vector["expect"]
    if vector["expect"] != "accept":
        assert "signed_message" not in vector
        return
    expected = vector["frame"]
    assert decode(raw, psk) == Frame(
        type=FrameType(expected["t"]),
        origin=expected["id"],
        seq=expected["seq"],
        ts=float(expected["ts"]),
        body=expected["body"],
    )
    sig = json.loads(raw.decode("utf-8"))["sig"]
    if not psk:
        assert sig == ""
        assert vector["signed_message"] is None
        assert vector["path"] is None
        return
    message = vector["signed_message"].encode("utf-8")
    assert hmac.new(psk, message, hashlib.sha256).hexdigest() == sig
    unsigned = json.loads(raw.decode("utf-8"))
    del unsigned["sig"]
    if vector["path"] == "transmitted":
        assert signed_message(raw) == message
    else:
        assert vector["path"] == "legacy-canonical"
        assert canonical_bytes(unsigned) == message
        assert signed_message(raw) != message


@pytest.mark.parametrize(
    "vector",
    [v for v in VECTORS if v.get("reference")],
    ids=[v["name"] for v in VECTORS if v.get("reference")],
)
def test_reference_vector_matches_encoder(vector: dict[str, Any]) -> None:
    frame = vector["frame"]
    raw = encode(
        FrameType(frame["t"]),
        frame["id"],
        frame["seq"],
        frame["ts"],
        frame["body"],
        KEYS[vector["key"]],
    )
    assert raw == vector["datagram"].encode("utf-8")
