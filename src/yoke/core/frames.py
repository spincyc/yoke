"""YWP/1 wire format: canonical encoding, HMAC authentication, validation.

Normative reference: doc/design/PROTOCOL.md. This module is pure: no
sockets, no clock, no global state. Per-origin replay tracking lives in
``node.py`` because it is stateful; this module handles everything that
can be judged from a single datagram.
"""

from __future__ import annotations

import hashlib
import hmac as hmac_mod
import json
from typing import Any

from yoke.core.types import (
    MAX_FRAME_BYTES,
    NODE_ID_RE,
    PROTOCOL_VERSION,
    Body,
    DropReason,
    Frame,
    FrameType,
)

_ENVELOPE_KEYS = {"v", "t", "id", "seq", "ts", "body", "sig"}


class EncodeError(ValueError):
    """The frame under construction violates YWP/1 (caller bug)."""


class FrameDrop(Exception):
    """Inbound datagram must be dropped; ``reason`` names the counter."""

    def __init__(self, reason: DropReason, detail: str = "") -> None:
        super().__init__(f"{reason.value}: {detail}" if detail else reason.value)
        self.reason = reason
        self.detail = detail


def canonical_bytes(obj: dict[str, Any]) -> bytes:
    """Canonical JSON per PROTOCOL.md: sorted keys, compact, UTF-8."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _signature(unsigned: dict[str, Any], psk: bytes) -> str:
    if not psk:
        return ""
    return hmac_mod.new(psk, canonical_bytes(unsigned), hashlib.sha256).hexdigest()


def encode(
    frame_type: FrameType,
    origin: str,
    seq: int,
    ts: float,
    body: Body,
    psk: bytes,
) -> bytes:
    """Build, sign, and serialize one frame. Raises EncodeError on misuse."""
    if not NODE_ID_RE.match(origin):
        raise EncodeError(f"invalid origin id: {origin!r}")
    if seq < 1:
        raise EncodeError(f"seq must be >= 1, got {seq}")
    unsigned: dict[str, Any] = {
        "v": PROTOCOL_VERSION,
        "t": frame_type.value,
        "id": origin,
        "seq": seq,
        "ts": ts,
        "body": body,
    }
    signed = dict(unsigned)
    signed["sig"] = _signature(unsigned, psk)
    raw = canonical_bytes(signed)
    if len(raw) > MAX_FRAME_BYTES:
        raise EncodeError(f"frame is {len(raw)} bytes, max {MAX_FRAME_BYTES}")
    return raw


def decode(raw: bytes, psk: bytes) -> Frame:
    """Validate and authenticate one datagram.

    Raises FrameDrop with the counter slug from PROTOCOL.md on any
    violation. Replay (stale/dup) is judged by the caller, which owns
    per-origin state.
    """
    if len(raw) > MAX_FRAME_BYTES:
        raise FrameDrop(DropReason.OVERSIZE, str(len(raw)))
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FrameDrop(DropReason.MALFORMED, "not canonical json") from exc
    if not isinstance(obj, dict):
        raise FrameDrop(DropReason.MALFORMED, "not an object")
    if set(obj) != _ENVELOPE_KEYS:
        raise FrameDrop(DropReason.MALFORMED, "envelope keys")
    if obj["v"] != PROTOCOL_VERSION:
        raise FrameDrop(DropReason.VERSION, repr(obj["v"]))
    try:
        frame_type = FrameType(obj["t"])
    except ValueError as exc:
        raise FrameDrop(DropReason.MALFORMED, "frame type") from exc
    origin = obj["id"]
    if not isinstance(origin, str) or not NODE_ID_RE.match(origin):
        raise FrameDrop(DropReason.MALFORMED, "origin id")
    seq = obj["seq"]
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
        raise FrameDrop(DropReason.MALFORMED, "seq")
    ts = obj["ts"]
    if not isinstance(ts, int | float) or isinstance(ts, bool):
        raise FrameDrop(DropReason.MALFORMED, "ts")
    body = obj["body"]
    if not isinstance(body, dict):
        raise FrameDrop(DropReason.MALFORMED, "body")
    sig = obj["sig"]
    if not isinstance(sig, str):
        raise FrameDrop(DropReason.MALFORMED, "sig")
    unsigned = {k: v for k, v in obj.items() if k != "sig"}
    expected = _signature(unsigned, psk)
    if not hmac_mod.compare_digest(sig, expected):
        raise FrameDrop(DropReason.AUTH)
    return Frame(type=frame_type, origin=origin, seq=seq, ts=float(ts), body=body)
