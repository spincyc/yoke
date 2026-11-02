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
import math
import re
from typing import Any

from yoke.core.types import (
    MAX_FRAME_BYTES,
    MAX_JSON_DEPTH,
    MAX_SEQUENCE,
    NODE_ID_RE,
    PROTOCOL_VERSION,
    Body,
    DropReason,
    Frame,
    FrameType,
)

_ENVELOPE_KEYS = {"v", "t", "id", "seq", "ts", "body", "sig"}
_HEX_OR_EMPTY = re.compile(r"^(?:[0-9a-f]{64})?$")  # SHA-256 hex, or "" open mode


class EncodeError(ValueError):
    """The frame under construction violates YWP/1 (caller bug)."""


class FrameDrop(Exception):  # noqa: N818 — a drop signal, not an error
    """Inbound datagram must be dropped; ``reason`` names the counter."""

    def __init__(self, reason: DropReason, detail: str = "") -> None:
        super().__init__(f"{reason.value}: {detail}" if detail else reason.value)
        self.reason = reason
        self.detail = detail


def _reject_floats(value: Any, where: str) -> None:
    """Senders emit integers only outside ``ts`` (PROTOCOL.md Encoding);
    decoders stay tolerant so the rule cannot brick interop."""
    if isinstance(value, float):
        raise EncodeError(f"non-integer number in {where}: {value!r}")
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_floats(item, f"{where}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_floats(item, f"{where}[{index}]")


def canonical_bytes(obj: dict[str, Any]) -> bytes:
    """Canonical JSON per PROTOCOL.md: sorted keys, compact, UTF-8."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _check_json(value: Any, *, depth: int = 0) -> None:
    """Bound recursive work and reject JSON extensions before signing."""
    stack: list[tuple[Any, int]] = [(value, depth)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, dict | list):
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise ValueError("JSON nesting exceeds limit")
            if isinstance(item, dict):
                for key, child in item.items():
                    if not isinstance(key, str):
                        raise ValueError("JSON object key is not a string")
                    key.encode("utf-8")
                    stack.append((child, depth))
            else:
                stack.extend((child, depth) for child in item)
        elif isinstance(item, str):
            item.encode("utf-8")
        elif isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError("nonfinite JSON number")
        elif item is not None and not isinstance(item, bool | int):
            raise ValueError("not a JSON value")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError(f"nonfinite JSON constant: {value}")


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
    if not isinstance(frame_type, FrameType):
        raise EncodeError("invalid frame type")
    if not isinstance(origin, str) or not NODE_ID_RE.fullmatch(origin):
        raise EncodeError(f"invalid origin id: {origin!r}")
    if type(seq) is not int or not 1 <= seq <= MAX_SEQUENCE:
        raise EncodeError(f"seq must be an int >= 1, got {seq!r}")
    try:
        if (
            isinstance(ts, bool)
            or not isinstance(ts, int | float)
            or not math.isfinite(ts)
        ):
            raise ValueError("timestamp must be finite")
        if not isinstance(body, dict):
            raise ValueError("body must be an object")
        _check_json(body, depth=1)  # include the enclosing wire envelope
        _reject_floats(body, "body")
    except (ValueError, TypeError, OverflowError, RecursionError) as exc:
        raise EncodeError(str(exc)) from exc
    unsigned: dict[str, Any] = {
        "v": PROTOCOL_VERSION,
        "t": frame_type.value,
        "id": origin,
        "seq": seq,
        "ts": ts,
        "body": body,
    }
    signed = dict(unsigned)
    try:
        signed["sig"] = _signature(unsigned, psk)
        raw = canonical_bytes(signed)
    except (ValueError, TypeError, OverflowError, RecursionError) as exc:
        raise EncodeError(str(exc)) from exc
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
        obj = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_invalid_constant,
        )
        _check_json(obj)
    except (ValueError, TypeError, OverflowError, RecursionError) as exc:
        raise FrameDrop(DropReason.MALFORMED, "not canonical json") from exc
    if not isinstance(obj, dict):
        raise FrameDrop(DropReason.MALFORMED, "not an object")
    if set(obj) != _ENVELOPE_KEYS:
        raise FrameDrop(DropReason.MALFORMED, "envelope keys")
    version = obj["v"]
    if type(version) is not int or version != PROTOCOL_VERSION:
        raise FrameDrop(DropReason.VERSION, repr(version))
    try:
        frame_type = FrameType(obj["t"])
    except (ValueError, TypeError) as exc:
        raise FrameDrop(DropReason.MALFORMED, "frame type") from exc
    origin = obj["id"]
    if not isinstance(origin, str) or not NODE_ID_RE.fullmatch(origin):
        raise FrameDrop(DropReason.MALFORMED, "origin id")
    seq = obj["seq"]
    if type(seq) is not int or not 1 <= seq <= MAX_SEQUENCE:
        raise FrameDrop(DropReason.MALFORMED, "seq")
    ts = obj["ts"]
    try:
        valid_ts = (
            isinstance(ts, int | float)
            and not isinstance(ts, bool)
            and math.isfinite(ts)
        )
    except OverflowError:
        valid_ts = False
    if not valid_ts:
        raise FrameDrop(DropReason.MALFORMED, "ts")
    body = obj["body"]
    if not isinstance(body, dict):
        raise FrameDrop(DropReason.MALFORMED, "body")
    sig = obj["sig"]
    if not isinstance(sig, str):
        raise FrameDrop(DropReason.MALFORMED, "sig")
    # sig must be SHA-256 hex (or "" in open mode). Reject any other
    # string BEFORE compare_digest: that call raises TypeError on a
    # non-ASCII str operand, and an attacker-chosen sig must never crash
    # the receive loop — it drops as auth. This precedes verification,
    # so without the guard it is a one-packet unauthenticated DoS.
    if not _HEX_OR_EMPTY.fullmatch(sig):
        raise FrameDrop(DropReason.AUTH, "sig format")
    unsigned = {k: v for k, v in obj.items() if k != "sig"}
    expected = _signature(unsigned, psk)
    if not hmac_mod.compare_digest(sig, expected):
        raise FrameDrop(DropReason.AUTH)
    return Frame(type=frame_type, origin=origin, seq=seq, ts=float(ts), body=body)
