"""Exhaustive tests for ``yoke.core.frames`` (YWP/1 wire format).

Normative reference: doc/design/PROTOCOL.md (Transport, Encoding, Frame
envelope, Authentication, Replay defense). The suite is fully
deterministic: fixed timestamps, seeded RNG, no network, no wall clock.
"""

from __future__ import annotations

import contextlib
import json
import random
import re
from typing import Any

import pytest

from yoke.core.frames import EncodeError, FrameDrop, canonical_bytes, decode, encode
from yoke.core.types import (
    MAX_FRAME_BYTES,
    MAX_JSON_DEPTH,
    DropReason,
    Frame,
    FrameType,
)

PSK = b"site-secret-0123456789abcdef"
OTHER_PSK = b"a-different-site-secret"
OPEN = b""
TS = 1_755_000_000.5
ORIGIN = "node-1"
SEQ = 7

ENVELOPE_KEYS = ["body", "id", "seq", "sig", "t", "ts", "v"]

# One representative body per frame type, following the PROTOCOL.md schemas.
BODIES: dict[FrameType, dict[str, Any]] = {
    FrameType.ANN: {
        "role": "producer",
        "kind": "hdmi",
        "name": "rack encoder café 山",
        "state": "active",
        "err": None,
        "pairing": None,
        "media": {"group": "239.10.0.1", "port": 5004},
    },
    FrameType.INT: {
        "op": "pair",
        "consumer": "desk-a",
        "producer": "rack-1",
        "version": [3, "cli-7"],
    },
    FrameType.ACK: {"version": [3, "cli-7"], "applied": True, "reason": "applied"},
    FrameType.EVT: {
        "event": "rx-drop",
        "detail": {"malformed": 1, "version": 0, "auth": 2, "stale": 0, "oversize": 0},
    },
    FrameType.BYE: {},
}


def _compact(obj: Any) -> bytes:
    """Compact JSON preserving insertion order (deliberately non-canonical)."""
    return json.dumps(
        obj, sort_keys=False, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _open_frame(**overrides: Any) -> dict[str, Any]:
    """A valid open-mode frame object to mutate in malformed-input tests."""
    base: dict[str, Any] = {
        "v": 1,
        "t": "ann",
        "id": ORIGIN,
        "seq": SEQ,
        "ts": TS,
        "body": {},
        "sig": "",
    }
    base.update(overrides)
    return base


# --- Round trip -------------------------------------------------------------


@pytest.mark.parametrize("psk", [PSK, OPEN], ids=["psk", "open"])
@pytest.mark.parametrize(
    "frame_type", list(FrameType), ids=[t.value for t in FrameType]
)
def test_round_trip_every_frame_type(frame_type: FrameType, psk: bytes) -> None:
    body = BODIES[frame_type]
    raw = encode(frame_type, ORIGIN, SEQ, TS, body, psk)
    assert len(raw) <= MAX_FRAME_BYTES
    frame = decode(raw, psk)
    assert frame == Frame(type=frame_type, origin=ORIGIN, seq=SEQ, ts=TS, body=body)
    sig = json.loads(raw.decode("utf-8"))["sig"]
    if psk:
        assert re.fullmatch(r"[0-9a-f]{64}", sig)
    else:
        assert sig == ""


def test_consumer_announce_round_trip_keeps_unknown_body_fields() -> None:
    body = {
        "role": "consumer",
        "kind": "usb",
        "name": "desk",
        "state": "idle",
        "err": None,
        "pairing": {"producer": None, "version": [0, ""]},
        "media": None,
        "future-field": {"x": 1},  # unknown body fields are ignored, not dropped
    }
    frame = decode(encode(FrameType.ANN, "desk-a", 1, TS, body, PSK), PSK)
    assert frame.body == body
    assert frame.origin == "desk-a"


@pytest.mark.parametrize("origin", ["a", "a" * 32, "0-node", "x9-"])
def test_valid_origin_ids_round_trip(origin: str) -> None:
    frame = decode(encode(FrameType.BYE, origin, 1, TS, {}, PSK), PSK)
    assert frame.origin == origin


def test_integer_ts_on_wire_decodes_to_float() -> None:
    raw = _compact(_open_frame(ts=1_755_000_000))
    frame = decode(raw, OPEN)
    assert isinstance(frame.ts, float)
    assert frame.ts == 1_755_000_000.0


# --- Canonicality -----------------------------------------------------------


def test_encoded_bytes_are_canonical_json() -> None:
    for frame_type, body in BODIES.items():
        raw = encode(frame_type, ORIGIN, SEQ, TS, body, PSK)
        obj = json.loads(raw.decode("utf-8"))
        canonical = json.dumps(
            obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        assert raw == canonical
        assert list(obj) == ENVELOPE_KEYS  # document key order is sorted


def test_encode_is_byte_stable_across_repeated_calls() -> None:
    for psk in (PSK, OPEN):
        encodings = {
            encode(FrameType.ANN, ORIGIN, SEQ, TS, BODIES[FrameType.ANN], psk)
            for _ in range(3)
        }
        assert len(encodings) == 1


def test_unicode_name_survives_without_ascii_escapes() -> None:
    raw = encode(FrameType.ANN, ORIGIN, SEQ, TS, BODIES[FrameType.ANN], PSK)
    assert "rack encoder café 山".encode() in raw
    assert b"\\u" not in raw
    assert decode(raw, PSK).body["name"] == "rack encoder café 山"


def test_canonical_bytes_sorts_keys_at_every_level() -> None:
    assert (
        canonical_bytes({"b": {"z": 1, "a": 2}, "a": 0}) == b'{"a":0,"b":{"a":2,"z":1}}'
    )


# --- Signature --------------------------------------------------------------


def test_bit_flip_anywhere_in_raw_never_decodes() -> None:
    raw = encode(FrameType.INT, ORIGIN, SEQ, TS, BODIES[FrameType.INT], PSK)
    for index in range(len(raw)):
        mutated = bytearray(raw)
        mutated[index] ^= 0x01
        with pytest.raises(FrameDrop) as excinfo:
            decode(bytes(mutated), PSK)
        assert excinfo.value.reason in {
            DropReason.AUTH,
            DropReason.MALFORMED,
            DropReason.VERSION,
        }


def test_content_tamper_that_keeps_json_valid_is_auth() -> None:
    body = {
        "role": "producer",
        "kind": "hdmi",
        "name": "tamperzone",
        "state": "idle",
        "err": None,
        "pairing": None,
        "media": None,
    }
    raw = encode(FrameType.ANN, ORIGIN, 123456, TS, body, PSK)
    # Every byte of a string value.
    start = raw.index(b"tamperzone")
    for offset in range(len(b"tamperzone")):
        mutated = bytearray(raw)
        original = mutated[start + offset]
        mutated[start + offset] = ord("q") if original != ord("q") else ord("x")
        with pytest.raises(FrameDrop) as excinfo:
            decode(bytes(mutated), PSK)
        assert excinfo.value.reason is DropReason.AUTH
    # Non-leading digits of seq (result stays a valid canonical integer).
    seq_at = raw.index(b'"seq":123456') + len(b'"seq":')
    for offset in range(1, 6):
        mutated = bytearray(raw)
        original = mutated[seq_at + offset]
        mutated[seq_at + offset] = ord("9") if original != ord("9") else ord("8")
        with pytest.raises(FrameDrop) as excinfo:
            decode(bytes(mutated), PSK)
        assert excinfo.value.reason is DropReason.AUTH


def test_tampering_any_sig_hex_digit_is_auth() -> None:
    obj = json.loads(encode(FrameType.BYE, ORIGIN, SEQ, TS, {}, PSK).decode("utf-8"))
    sig = obj["sig"]
    for index in range(64):
        replacement = "0" if sig[index] != "0" else "f"
        bad = sig[:index] + replacement + sig[index + 1 :]
        with pytest.raises(FrameDrop) as excinfo:
            decode(canonical_bytes({**obj, "sig": bad}), PSK)
        assert excinfo.value.reason is DropReason.AUTH


def test_wrong_psk_is_auth() -> None:
    raw = encode(FrameType.ACK, ORIGIN, SEQ, TS, BODIES[FrameType.ACK], PSK)
    with pytest.raises(FrameDrop) as excinfo:
        decode(raw, OTHER_PSK)
    assert excinfo.value.reason is DropReason.AUTH


def test_open_mode_frame_rejected_by_keyed_decoder() -> None:
    raw = encode(FrameType.ANN, ORIGIN, SEQ, TS, BODIES[FrameType.ANN], OPEN)
    with pytest.raises(FrameDrop) as excinfo:
        decode(raw, PSK)
    assert excinfo.value.reason is DropReason.AUTH


def test_keyed_frame_rejected_by_open_decoder() -> None:
    raw = encode(FrameType.ANN, ORIGIN, SEQ, TS, BODIES[FrameType.ANN], PSK)
    with pytest.raises(FrameDrop) as excinfo:
        decode(raw, OPEN)
    assert excinfo.value.reason is DropReason.AUTH


def test_keyed_frame_with_emptied_sig_is_auth() -> None:
    obj = json.loads(
        encode(FrameType.EVT, ORIGIN, SEQ, TS, BODIES[FrameType.EVT], PSK).decode(
            "utf-8"
        )
    )
    obj["sig"] = ""
    with pytest.raises(FrameDrop) as excinfo:
        decode(canonical_bytes(obj), PSK)
    assert excinfo.value.reason is DropReason.AUTH


def test_signature_binds_content_not_byte_order() -> None:
    raw = encode(FrameType.ACK, ORIGIN, SEQ, TS, BODIES[FrameType.ACK], PSK)
    obj = json.loads(raw.decode("utf-8"))
    reordered = {key: obj[key] for key in ["sig", "body", "ts", "seq", "id", "t", "v"]}
    raw_reordered = json.dumps(
        reordered, sort_keys=False, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    assert raw_reordered != raw
    assert decode(raw_reordered, PSK) == decode(raw, PSK)
    # Non-compact whitespace is likewise irrelevant to the signature.
    raw_spaced = json.dumps(reordered, sort_keys=False, ensure_ascii=False).encode(
        "utf-8"
    )
    assert decode(raw_spaced, PSK) == decode(raw, PSK)


# --- Envelope validation ----------------------------------------------------


@pytest.mark.parametrize("missing", ENVELOPE_KEYS)
def test_missing_envelope_key_is_malformed(missing: str) -> None:
    obj = _open_frame()
    del obj[missing]
    with pytest.raises(FrameDrop) as excinfo:
        decode(_compact(obj), OPEN)
    assert excinfo.value.reason is DropReason.MALFORMED
    assert excinfo.value.detail == "envelope keys"


def test_extra_envelope_key_is_malformed() -> None:
    obj = _open_frame()
    obj["hop"] = 1
    with pytest.raises(FrameDrop) as excinfo:
        decode(_compact(obj), OPEN)
    assert excinfo.value.reason is DropReason.MALFORMED
    assert excinfo.value.detail == "envelope keys"


def test_renamed_envelope_key_is_malformed() -> None:
    obj = _open_frame()
    del obj["sig"]
    obj["signature"] = ""
    with pytest.raises(FrameDrop) as excinfo:
        decode(_compact(obj), OPEN)
    assert excinfo.value.reason is DropReason.MALFORMED


@pytest.mark.parametrize("doc", ["[]", "1", "3.5", '"frame"', "null", "true"])
def test_non_object_datagram_is_malformed(doc: str) -> None:
    with pytest.raises(FrameDrop) as excinfo:
        decode(doc.encode("utf-8"), OPEN)
    assert excinfo.value.reason is DropReason.MALFORMED
    assert excinfo.value.detail == "not an object"


@pytest.mark.parametrize("raw", [b"", b"{", b'{"v":1', b"\xff\xfe", b"\x80\x80"])
def test_unparseable_datagram_is_malformed(raw: bytes) -> None:
    with pytest.raises(FrameDrop) as excinfo:
        decode(raw, OPEN)
    assert excinfo.value.reason is DropReason.MALFORMED
    assert excinfo.value.detail == "not canonical json"


@pytest.mark.parametrize(
    ("field", "value", "detail"),
    [
        ("id", "UPPER", "origin id"),
        ("id", "-lead", "origin id"),
        ("id", "a" * 33, "origin id"),
        ("id", "", "origin id"),
        ("id", "a_b", "origin id"),
        ("id", 7, "origin id"),
        ("id", None, "origin id"),
        ("seq", 0, "seq"),
        ("seq", -3, "seq"),
        ("seq", True, "seq"),  # bool trap: True is an int subclass
        ("seq", "1", "seq"),
        ("seq", 1.0, "seq"),
        ("ts", "1755000000", "ts"),
        ("ts", True, "ts"),
        ("ts", False, "ts"),
        ("ts", None, "ts"),
        ("body", [], "body"),
        ("body", "x", "body"),
        ("body", None, "body"),
        ("body", 3, "body"),
        ("sig", 7, "sig"),
        ("sig", None, "sig"),
        ("sig", True, "sig"),
        ("t", "nop", "frame type"),
        ("t", "ANN", "frame type"),
        ("t", "", "frame type"),
        ("t", 3, "frame type"),
        ("t", None, "frame type"),
    ],
    ids=lambda value: repr(value),
)
def test_malformed_envelope_field(field: str, value: Any, detail: str) -> None:
    obj = _open_frame(**{field: value})
    with pytest.raises(FrameDrop) as excinfo:
        decode(_compact(obj), OPEN)
    assert excinfo.value.reason is DropReason.MALFORMED
    assert excinfo.value.detail == detail


@pytest.mark.parametrize("version", [2, 0, -1, "1", 1.5, None], ids=repr)
def test_wrong_protocol_version_is_dropped(version: Any) -> None:
    obj = _open_frame(v=version)
    with pytest.raises(FrameDrop) as excinfo:
        decode(_compact(obj), OPEN)
    assert excinfo.value.reason is DropReason.VERSION


def test_version_check_precedes_type_and_field_checks() -> None:
    obj = _open_frame(v=2, t="nop", seq=0)
    with pytest.raises(FrameDrop) as excinfo:
        decode(_compact(obj), OPEN)
    assert excinfo.value.reason is DropReason.VERSION


def test_envelope_key_check_precedes_version_check() -> None:
    obj = _open_frame(v=2)
    obj["hop"] = 1
    with pytest.raises(FrameDrop) as excinfo:
        decode(_compact(obj), OPEN)
    assert excinfo.value.reason is DropReason.MALFORMED


def test_envelope_validation_precedes_auth() -> None:
    obj = json.loads(encode(FrameType.BYE, ORIGIN, SEQ, TS, {}, PSK).decode("utf-8"))
    obj["seq"] = 0  # invalidates both the field and the signature
    with pytest.raises(FrameDrop) as excinfo:
        decode(canonical_bytes(obj), PSK)
    assert excinfo.value.reason is DropReason.MALFORMED


# --- Oversize ---------------------------------------------------------------


def test_oversize_datagram_is_dropped_before_parsing() -> None:
    raw = b"\xff" * (MAX_FRAME_BYTES + 1)  # not even valid UTF-8: size wins
    with pytest.raises(FrameDrop) as excinfo:
        decode(raw, OPEN)
    assert excinfo.value.reason is DropReason.OVERSIZE
    assert excinfo.value.detail == str(MAX_FRAME_BYTES + 1)


def test_oversize_valid_json_frame_is_dropped() -> None:
    raw = _compact(_open_frame(body={"pad": "a" * (MAX_FRAME_BYTES + 100)}))
    assert len(raw) > MAX_FRAME_BYTES
    with pytest.raises(FrameDrop) as excinfo:
        decode(raw, OPEN)
    assert excinfo.value.reason is DropReason.OVERSIZE


def _padded_frame(pad_len: int, psk: bytes) -> bytes:
    body = {"event": "started", "detail": {"pad": "a" * pad_len}}
    return encode(FrameType.EVT, ORIGIN, SEQ, TS, body, psk)


def test_exactly_max_frame_bytes_encodes_and_decodes() -> None:
    pad = MAX_FRAME_BYTES - len(_padded_frame(0, PSK))
    raw = _padded_frame(pad, PSK)
    assert len(raw) == MAX_FRAME_BYTES
    assert decode(raw, PSK).body["detail"]["pad"] == "a" * pad
    with pytest.raises(EncodeError):
        _padded_frame(pad + 1, PSK)


# --- EncodeError ------------------------------------------------------------


@pytest.mark.parametrize("origin", ["UPPER", "-lead", "a" * 33, "", "a_b", "café"])
def test_encode_rejects_bad_origin(origin: str) -> None:
    with pytest.raises(EncodeError):
        encode(FrameType.BYE, origin, 1, TS, {}, PSK)


@pytest.mark.parametrize("seq", [0, -1, -1000])
def test_encode_rejects_seq_below_one(seq: int) -> None:
    with pytest.raises(EncodeError):
        encode(FrameType.BYE, ORIGIN, seq, TS, {}, PSK)


def test_encode_rejects_oversize_body() -> None:
    with pytest.raises(EncodeError):
        encode(FrameType.EVT, ORIGIN, SEQ, TS, {"pad": "x" * 2000}, PSK)


# --- Fuzz: decode never raises anything but FrameDrop -----------------------


def test_decode_raises_only_framedrop_under_seeded_fuzz() -> None:
    rng = random.Random(1234)
    valid = [encode(t, ORIGIN, SEQ, TS, BODIES[t], PSK) for t in FrameType]
    corpus: list[bytes] = []
    # Random byte strings.
    for _ in range(120):
        corpus.append(bytes(rng.randrange(256) for _ in range(rng.randrange(80))))
    # Truncated valid frames.
    for _ in range(120):
        frame_raw = valid[rng.randrange(len(valid))]
        corpus.append(frame_raw[: rng.randrange(len(frame_raw))])
    # Valid JSON scalars and arrays.
    for _ in range(60):
        choice = rng.randrange(5)
        value: Any
        if choice == 0:
            value = rng.randrange(-(10**9), 10**9)
        elif choice == 1:
            value = rng.random()
        elif choice == 2:
            value = "".join(
                chr(rng.randrange(32, 127)) for _ in range(rng.randrange(20))
            )
        elif choice == 3:
            value = [rng.randrange(100) for _ in range(rng.randrange(6))]
        else:
            value = [None, True, False][rng.randrange(3)]
        corpus.append(json.dumps(value).encode("utf-8"))
    # Single random byte mutations of valid frames.
    for _ in range(60):
        mutated = bytearray(valid[rng.randrange(len(valid))])
        mutated[rng.randrange(len(mutated))] ^= rng.randrange(1, 256)
        corpus.append(bytes(mutated))
    assert len(corpus) == 360
    for raw in corpus:
        for psk in (PSK, OPEN):
            with contextlib.suppress(FrameDrop):
                decode(raw, psk)  # success or FrameDrop; anything else fails


# --- Drop counter slugs -----------------------------------------------------


def test_drop_reason_slugs_match_protocol_counters() -> None:
    assert {reason.name: reason.value for reason in DropReason} == {
        "MALFORMED": "malformed",
        "VERSION": "version",
        "AUTH": "auth",
        "STALE": "stale",
        "DUP": "dup",
        "OVERSIZE": "oversize",
    }


def test_framedrop_carries_reason_and_detail() -> None:
    drop = FrameDrop(DropReason.OVERSIZE, "1300")
    assert drop.reason is DropReason.OVERSIZE
    assert drop.detail == "1300"
    assert str(drop) == "oversize: 1300"
    assert str(FrameDrop(DropReason.AUTH)) == "auth"


# --- Regressions from the first review pass ---------------------------------


@pytest.mark.parametrize("version", [True, False, 1.0], ids=["true", "false", "1.0"])
def test_version_bool_and_float_traps_are_dropped(version: Any) -> None:
    """`v` must be the int 1: Python's True == 1 and 1.0 == 1 must not leak."""
    obj = _open_frame(v=version)
    with pytest.raises(FrameDrop) as excinfo:
        decode(_compact(obj), OPEN)
    assert excinfo.value.reason is DropReason.VERSION


def test_encode_rejects_bool_seq() -> None:
    with pytest.raises(EncodeError, match="seq"):
        encode(FrameType.ANN, ORIGIN, True, TS, {}, PSK)


def test_encode_rejects_floats_outside_ts() -> None:
    with pytest.raises(EncodeError, match="body"):
        encode(FrameType.EVT, ORIGIN, SEQ, TS, {"detail": {"rate": 1.5}}, PSK)
    with pytest.raises(EncodeError, match=r"body\[0\]|body"):
        encode(FrameType.EVT, ORIGIN, SEQ, TS, {"xs": [2.5]}, PSK)


def test_decode_tolerates_floats_in_body() -> None:
    obj = _open_frame(body={"rate": 1.5})
    frame = decode(_compact(obj), OPEN)
    assert frame.body == {"rate": 1.5}


@pytest.mark.parametrize(
    "sig",
    ["é", "ÿ" * 64, "not-hex-but-ascii", "abc", "A" * 64, "0" * 63, "0" * 65],
    ids=["nonascii1", "nonascii64", "ascii-nonhex", "short", "uppercase", "63", "65"],
)
def test_bad_sig_format_drops_as_auth_never_raises(sig: str) -> None:
    """A non-hex sig — including non-ASCII, which would crash
    hmac.compare_digest with TypeError — must drop as auth, not escape.
    One crafted packet must never take down a receive loop."""
    obj = _open_frame(sig=sig)
    with pytest.raises(FrameDrop) as excinfo:
        decode(_compact(obj), OPEN)
    assert excinfo.value.reason is DropReason.AUTH
    with pytest.raises(FrameDrop) as excinfo_keyed:
        decode(_compact(obj), PSK)
    assert excinfo_keyed.value.reason is DropReason.AUTH


@pytest.mark.parametrize(
    "raw",
    [
        b'{"v":1,"v":1,"t":"bye","id":"n","seq":1,"ts":0,"body":{},"sig":""}',
        b'{"v":1,"t":"bye","id":"n","seq":1,"ts":NaN,"body":{},"sig":""}',
        b'{"v":1,"t":"bye","id":"n","seq":1,"ts":0,"body":{"x":Infinity},"sig":""}',
        b'{"v":1,"t":"bye","id":"n","seq":1,"ts":0,"body":{"x":"\\ud800"},"sig":""}',
        b'{"v":1,"t":[],"id":"n","seq":1,"ts":0,"body":{},"sig":""}',
    ],
)
def test_adversarial_json_is_framedrop_only(raw: bytes) -> None:
    for psk in (PSK, OPEN):
        with pytest.raises(FrameDrop):
            decode(raw, psk)


def test_nested_json_work_is_bounded_before_authentication() -> None:
    raw = (
        b'{"v":1,"t":"bye","id":"n","seq":1,"ts":0,"body":{"x":'
        + b"[" * 40
        + b"0"
        + b"]" * 40
        + b'},"sig":""}'
    )
    assert len(raw) < MAX_FRAME_BYTES
    with pytest.raises(FrameDrop) as exc:
        decode(raw, OPEN)
    assert exc.value.reason is DropReason.MALFORMED


@pytest.mark.parametrize("seq", [1.5, "1", (1 << 63), True])
def test_encode_rejects_noninteger_or_out_of_range_sequence(seq: Any) -> None:
    with pytest.raises(EncodeError):
        encode(FrameType.BYE, ORIGIN, seq, TS, {}, PSK)


@pytest.mark.parametrize("ts", [float("nan"), float("inf"), -float("inf"), 10**1000])
def test_encode_rejects_nonfinite_or_unrepresentable_timestamp(ts: Any) -> None:
    with pytest.raises(EncodeError):
        encode(FrameType.BYE, ORIGIN, 1, ts, {}, PSK)


def test_trailing_newline_cannot_extend_origin_id() -> None:
    with pytest.raises(EncodeError):
        encode(FrameType.BYE, "node\n", 1, TS, {}, PSK)
    with pytest.raises(FrameDrop):
        decode(_compact(_open_frame(id="node\n")), OPEN)


def test_json_depth_limit_round_trips_at_exact_wire_boundary() -> None:
    value: Any = 0
    for _ in range(MAX_JSON_DEPTH - 2):
        value = [value]
    body = {"nested": value}
    raw = encode(FrameType.EVT, ORIGIN, 1, TS, body, PSK)
    assert decode(raw, PSK).body == body
    with pytest.raises(EncodeError):
        encode(FrameType.EVT, ORIGIN, 2, TS, {"nested": [value]}, PSK)
