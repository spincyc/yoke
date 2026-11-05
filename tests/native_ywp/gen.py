"""Seeded generators of YWP/1 datagrams for the native differential tests.

Frames are produced in many valid serializations (canonical, ASCII-escaped,
pretty, unsorted, odd number spellings and escapes), signed over their own
transmitted bytes or over the legacy canonical message, and then optionally
damaged by byte-level and JSON-level mutations. Both decoders must agree on
every result, so the generator needs no oracle of its own.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import random
import struct
from typing import Any

from yoke.core.frames import canonical_bytes, signed_message

PSK = bytes(range(32))
OTHER_PSK = bytes(range(32, 64))
TYPES = ("ann", "int", "ack", "evt", "bye")
ENVELOPE = ("v", "t", "id", "seq", "ts", "body", "sig")
_CHARS = 'abcxyz-019 "\\/\b\f\n\r\t\x00\x01\x1f\x7f' + "".join(
    map(chr, (0xE9, 0x101, 0x2028, 0x2029, 0x5C71, 0xFEFF, 0xFFFF, 0x1F600, 0x10FFFF))
)
_SPECIAL_FLOATS = (
    0.0,
    -0.0,
    0.1,
    1e16,
    1e15,
    1e-5,
    1e-4,
    5e-324,
    2.2250738585072014e-308,
    1.7976931348623157e308,
    1793000000.1,
    1793000000.0999999,
    123456789.0,
)


def random_float(rng: random.Random) -> float:
    choice = rng.random()
    if choice < 0.3:
        return rng.choice(_SPECIAL_FLOATS) * rng.choice((1, -1))
    if choice < 0.6:
        return rng.uniform(-1e10, 1e10)
    while True:
        value: float = struct.unpack("<d", rng.getrandbits(64).to_bytes(8, "little"))[0]
        if value == value and abs(value) != float("inf"):
            return value


def random_text(rng: random.Random, limit: int = 12) -> str:
    return "".join(rng.choice(_CHARS) for _ in range(rng.randrange(limit)))


def random_value(rng: random.Random, depth: int = 0) -> Any:
    roll = rng.random()
    if depth < 4 and roll < 0.15:
        return {
            random_text(rng, 6): random_value(rng, depth + 1)
            for _ in range(rng.randrange(4))
        }
    if depth < 4 and roll < 0.25:
        return [random_value(rng, depth + 1) for _ in range(rng.randrange(4))]
    if roll < 0.45:
        return random_text(rng)
    if roll < 0.65:
        return rng.choice(
            (
                0,
                1,
                -1,
                2**63 - 1,
                2**63,
                2**64,
                -(2**70),
                10**40,
                rng.randrange(-1000, 1000),
            )
        )
    if roll < 0.85:
        return random_float(rng)
    return rng.choice((True, False, None))


def _body(rng: random.Random, frame_type: str) -> dict[str, Any]:
    ids = ("prod-1", "con-1", "cli-7", "a", "x" * 32, "Bad", "", "-x")
    version: Any = [rng.choice((0, 1, 3, 2**63 - 1, 2**63, -1)), rng.choice(ids)]
    if rng.random() < 0.1:
        version = rng.choice((None, [1], [True, "a"], "v", [1.0, "a"]))
    if frame_type == "ann":
        body: dict[str, Any] = {
            "role": rng.choice(("producer", "consumer", "x", 1)),
            "kind": rng.choice(("hdmi", "usb", "")),
            "name": random_text(rng, 70),
            "state": rng.choice(("idle", "pending", "active", "error", None)),
            "err": rng.choice((None, "boom", 3)),
            "pairing": rng.choice(
                (
                    None,
                    {
                        "producer": rng.choice((None, "prod-1", "Bad")),
                        "version": version,
                    },
                )
            ),
            "media": rng.choice(
                (None, {"host": "10.0.0.1", "port": 3240, "busids": ["1-1"]}, [1])
            ),
        }
    elif frame_type == "int":
        body = {
            "op": rng.choice(("pair", "unpair", "x")),
            "consumer": rng.choice(ids),
            "version": version,
        }
        if rng.random() < 0.7:
            body["producer"] = rng.choice((*ids, 1, None))
    elif frame_type == "ack":
        body = {
            "version": version,
            "applied": rng.choice((True, False, 0)),
            "reason": rng.choice(("applied", "self-pair", None)),
        }
    elif frame_type == "evt":
        body = {
            "event": rng.choice(("started", "rx-drop")),
            "detail": random_value(rng, 1),
        }
    else:
        body = {}
    for _ in range(rng.randrange(3)):
        body[random_text(rng, 5)] = random_value(rng, 1)
    return body


def _dump_str(rng: random.Random, text: str, style: str) -> str:
    if style == "canonical":
        return json.dumps(text, ensure_ascii=False)
    out = ['"']
    for ch in text:
        code = ord(ch)
        if ch in '"\\' or code < 0x20 or (style == "ascii" and code > 0x7E):
            out.append(json.dumps(ch, ensure_ascii=True)[1:-1])
        elif style == "escapes" and rng.random() < 0.3:
            if code > 0xFFFF:
                hi, lo = divmod(code - 0x10000, 0x400)
                out.append(f"\\u{0xD800 + hi:04X}\\u{0xDC00 + lo:04x}")
            elif ch == "/":
                out.append("\\/")
            else:
                out.append(f"\\u{code:04X}" if rng.random() < 0.5 else f"\\u{code:04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _dump_float(rng: random.Random, value: float, style: str) -> str:
    if style == "canonical":
        return repr(value)
    spelling = rng.choice(("repr", ".17g", "e", "E+"))
    if spelling == ".17g":
        text = format(value, ".17g")
    elif spelling == "e":
        text = format(value, ".16e")
    elif spelling == "E+":
        text = format(value, ".12E")
    else:
        text = repr(value)
    if "e" not in text.lower() and "." not in text:
        text += ".0"
    return text


def dump(rng: random.Random, value: Any, style: str, sort: bool = True) -> str:
    """Serialize with a style: canonical, ascii, escapes, pretty, unsorted."""
    sep = ", " if style == "pretty" else ","
    colon = " : " if style == "pretty" else ":"
    if isinstance(value, dict):
        items = list(value.items())
        if sort:
            items.sort()
        else:
            rng.shuffle(items)
        inner = sep.join(
            _dump_str(rng, key, style) + colon + dump(rng, item, style, sort)
            for key, item in items
        )
        return ("{\r\n " if style == "pretty" else "{") + inner + "}"
    if isinstance(value, list):
        return "[" + sep.join(dump(rng, item, style, sort) for item in value) + "]"
    if isinstance(value, str):
        return _dump_str(rng, value, style)
    if isinstance(value, bool) or value is None:
        return json.dumps(value)
    if isinstance(value, float):
        return _dump_float(rng, value, style)
    return str(value)


def frame(rng: random.Random, psk: bytes = PSK) -> bytes:
    """One signed datagram in a random valid serialization."""
    frame_type = rng.choice(TYPES)
    version: Any = 1
    if rng.random() < 0.05:
        version = rng.choice((2, 0, -1, 1.0, True, "1", None, 10**30))
    obj: dict[str, Any] = {
        "v": version,
        "t": frame_type,
        "id": rng.choice(("rack-1", "con-1", "a" * 32)),
        "seq": rng.choice((1, 1793000000000000001, 2**63 - 1)),
        "ts": rng.choice((1793000000.25, random_float(rng), 1793000000)),
        "body": _body(rng, frame_type),
        "sig": "0" * 64 if psk else "",
    }
    style = rng.choice(
        ("canonical", "canonical", "ascii", "escapes", "pretty", "unsorted")
    )
    text = dump(rng, obj, style, sort=style != "unsorted")
    raw = text.encode("utf-8")
    if not psk:
        return raw
    path = rng.random()
    try:
        if path < 0.7:
            message = signed_message(raw)
        else:
            unsigned = json.loads(raw)
            del unsigned["sig"]
            message = canonical_bytes(unsigned)
    except ValueError:
        return raw
    sig = hmac.new(psk, message, hashlib.sha256).hexdigest()
    return raw.replace(b'"' + b"0" * 64 + b'"', b'"' + sig.encode() + b'"', 1)


_SNIPPETS = (
    b"NaN",
    b"Infinity",
    b"-Infinity",
    b"1e400",
    b"-1e400",
    b"1E-400",
    b"-0",
    b"01",
    b"1.",
    b".5",
    b"1e",
    b"\\ud800",
    b"\\udc00",
    b"\\ud800\\udc00",
    b"\\u00",
    b"\\x",
    b"\xff",
    b"\xc0\xaf",
    b"\xed\xa0\x80",
    b"\xef\xbb\xbf",
    b"\x00",
    b"\x1f",
    b"true",
    b"null",
    b",",
    b":",
    b"{",
    b"}",
    b"[",
    b"]",
    b'"',
    b"\\",
    b'"sig":""',
    b'"v":1',
    b"[" * 40,
    b"]" * 40,
    b'"\\u0073ig"',
    b" ",
    b"\t",
    b"\r\n",
    b"\x0b",
)


def mutate(rng: random.Random, raw: bytes) -> bytes:
    data = bytearray(raw)
    for _ in range(rng.randrange(1, 4)):
        op = rng.random()
        at = rng.randrange(len(data) + 1)
        if op < 0.25 and data:
            data[min(at, len(data) - 1)] ^= 1 << rng.randrange(8)
        elif op < 0.45:
            data[at:at] = rng.choice(_SNIPPETS)
        elif op < 0.6 and data:
            del data[at : at + rng.randrange(1, 6)]
        elif op < 0.7:
            data = data[:at]
        elif op < 0.8 and data:
            data[min(at, len(data) - 1)] = rng.randrange(256)
        elif op < 0.9:
            data += bytes(rng.randrange(256) for _ in range(rng.randrange(4)))
        else:
            data[at:at] = b" " * rng.randrange(1, 1300)
    return bytes(data)


def corpus(seed: int, count: int) -> list[tuple[bytes, bytes]]:
    """(psk, datagram) cases: valid frames, mutated frames, wrong keys."""
    rng = random.Random(seed)
    cases: list[tuple[bytes, bytes]] = []
    for _ in range(count):
        psk = rng.choice((PSK, PSK, PSK, b""))
        raw = frame(rng, psk)
        roll = rng.random()
        if roll < 0.45:
            raw = mutate(rng, raw)
        elif roll < 0.5:
            psk = OTHER_PSK
        cases.append((psk, raw))
    return cases
