"""YWP/1 wire format: canonical encoding, HMAC authentication, validation.

Normative reference: doc/design/PROTOCOL.md. This module is pure: no
sockets, no clock, no global state. Per-origin replay tracking lives in
``node.py`` because it is stateful; this module handles everything that
can be judged from a single datagram.

Senders (``encode``) emit canonical JSON and sign its re-serialization
without ``sig``. Receivers (``decode``) authenticate the transmitted
bytes: the signed message is the datagram with its top-level ``sig``
member excised (``signed_message``). For compatibility they also accept
a signature over the legacy canonical re-serialization. For canonical
frames the two messages are byte-identical, so both rules agree.
Conformance vectors: ``tests/vectors/ywp1-auth.json``.
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


class SignedMessageError(ValueError):
    """The ``sig`` member cannot be located for excision (structural)."""


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


# One JSON token with its leading insignificant whitespace (RFC 8259 ws
# only). Group 1: string; group 2: structural char; group 3: scalar run.
# Structural bytes are ASCII and never occur inside a multibyte UTF-8
# sequence, so scanning bytes is safe.
_TOKEN = re.compile(
    rb'[ \t\n\r]*(?:("[^"\\]*(?:\\.[^"\\]*)*")|([{}\[\]:,])|([^"{}\[\]:, \t\n\r]+))',
    re.DOTALL,
)
_TRAILING_WS = re.compile(rb"[ \t\n\r]*")


def signed_message(raw: bytes) -> bytes:
    """Return ``raw`` with its top-level ``sig`` member excised.

    This is the YWP/1 signed message for transmitted-bytes authentication
    (PROTOCOL.md Authentication). Let K be the offset of the opening quote
    of the top-level key that unescapes to ``sig`` and E the offset just
    past the closing quote of its string value. If the member is followed
    by another, bytes [K, C+1) are removed, C being the terminating ``,``;
    if it is the last member, bytes [P, E) are removed, P being the
    preceding ``,``. All other bytes, whitespace included, are kept. For
    canonical input the result equals ``canonical_bytes`` of the object
    without ``sig``.

    Precondition: ``raw`` is a JSON object that ``decode`` would parse; this
    is a locator, not a validator. Raises SignedMessageError when the
    top-level structure is unbalanced or there is not exactly one
    top-level ``sig`` member with a string value and a sibling member.
    """
    depth = 0
    pos = 0
    end = len(raw)
    expect_key = False
    prev_comma = -1  # offset of the last top-level ',' seen
    key_comma = -1  # that offset when the sig key was seen
    key_at = -1  # K
    value_end = -1  # E
    state = 0  # 0 scanning, 1 after sig key, 2 after ':', 3 after value
    result: bytes | None = None
    closed = False
    while pos < end:
        match = _TOKEN.match(raw, pos)
        if match is None:
            if _TRAILING_WS.fullmatch(raw, pos):
                break
            raise SignedMessageError("unscannable byte")
        pos = match.end()
        if closed:
            raise SignedMessageError("data after top-level object")
        text, punct = match.group(1), match.group(2)
        if state == 1:
            if punct != b":":
                raise SignedMessageError("sig key without ':'")
            state = 2
            continue
        if state == 2:
            if text is None:
                raise SignedMessageError("sig value is not a string")
            value_end = match.end(1)
            state = 3
            continue
        if state == 3:
            if punct == b",":
                if result is not None:
                    raise SignedMessageError("duplicate sig member")
                result = raw[:key_at] + raw[match.end(2) :]
            elif punct == b"}":
                if key_comma < 0:
                    raise SignedMessageError("sig is the only member")
                if result is not None:
                    raise SignedMessageError("duplicate sig member")
                result = raw[:key_comma] + raw[value_end:]
            else:
                raise SignedMessageError("sig member not terminated")
            state = 0
        if punct is not None:
            if punct in b"{[":
                if depth == 0 and punct != b"{":
                    raise SignedMessageError("top level is not an object")
                depth += 1
                expect_key = depth == 1
            elif punct in b"}]":
                depth -= 1
                if depth < 0:
                    raise SignedMessageError("unbalanced brackets")
                closed = depth == 0
            elif depth == 1 and punct == b",":
                prev_comma = match.start(2)
                expect_key = True
        elif depth == 0:
            raise SignedMessageError("top level is not an object")
        elif text is not None and depth == 1 and expect_key:
            expect_key = False
            is_sig = text == b'"sig"'
            if not is_sig and b"\\" in text:  # escaped key, e.g. "\u0073ig"
                try:
                    is_sig = json.loads(text) == "sig"
                except ValueError as exc:
                    raise SignedMessageError("undecodable key") from exc
            if is_sig:
                key_at = match.start(1)
                key_comma = prev_comma
                state = 1
    if not closed or state != 0 or result is None:
        raise SignedMessageError("no complete top-level sig member")
    return result


def _hmac_hex(message: bytes, psk: bytes) -> str:
    return hmac_mod.new(psk, message, hashlib.sha256).hexdigest()


def _signature(unsigned: dict[str, Any], psk: bytes) -> str:
    if not psk:
        return ""
    return _hmac_hex(canonical_bytes(unsigned), psk)


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
    if not psk:
        if not hmac_mod.compare_digest(sig, ""):
            raise FrameDrop(DropReason.AUTH)
    elif not _authentic(sig, raw, obj, psk):
        raise FrameDrop(DropReason.AUTH)
    return Frame(type=frame_type, origin=origin, seq=seq, ts=float(ts), body=body)


def _authentic(sig: str, raw: bytes, obj: dict[str, Any], psk: bytes) -> bool:
    """Accept a signature over either permitted message (ADR 0023).

    The canonical message is tried first: every reference sender emits
    canonical bytes, for which it equals the transmitted-bytes message, so
    the byte scan is paid only by non-canonical senders and forgeries. The
    accepted set is the same in either order.
    """
    legacy = canonical_bytes({k: v for k, v in obj.items() if k != "sig"})
    if hmac_mod.compare_digest(sig, _hmac_hex(legacy, psk)):
        return True
    try:
        message = signed_message(raw)
    except SignedMessageError:
        # Unreachable for input that passed decode's strict parse; an
        # unlocatable member can never authenticate transmitted bytes.
        return False
    if message == legacy:  # canonical frame: the same message, already tried
        return False
    return hmac_mod.compare_digest(sig, _hmac_hex(message, psk))
