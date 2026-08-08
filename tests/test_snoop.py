"""Tests for ``yoke.snoop`` — the passive conformance oracle.

Fully deterministic: frames are built with ``yoke.core.frames.encode``
and fed straight into ``Snooper.observe`` with explicit timestamps. No
sockets, no sleeps, no network, no wall clock.
"""

from __future__ import annotations

import inspect
import json
from typing import Any

from yoke.core.frames import canonical_bytes, encode
from yoke.core.types import FrameType
from yoke.snoop import Snooper, render_json, render_text

PSK = b"snoop-site-secret-0123"
WRONG_PSK = b"not-the-site-secret"
OPEN = b""
TS = 1_755_000_000.0
T0 = 1000.0


def ann_producer(
    origin: str = "cam-1",
    seq: int = 1,
    *,
    psk: bytes = PSK,
    kind: str = "hdmi",
    state: str = "active",
    name: str = "Rack cam",
) -> bytes:
    body: dict[str, Any] = {
        "role": "producer",
        "kind": kind,
        "name": name,
        "state": state,
        "err": None,
        "pairing": None,
        "media": {"group": "239.10.0.1", "port": 5004},
    }
    return encode(FrameType.ANN, origin, seq, TS, body, psk)


def ann_consumer(
    origin: str = "tv-den",
    seq: int = 1,
    *,
    psk: bytes = PSK,
    producer: str | None = None,
    version: tuple[int, str] = (0, ""),
    kind: str = "hdmi",
    state: str = "idle",
    err: str | None = None,
    name: str = "Den TV",
) -> bytes:
    body: dict[str, Any] = {
        "role": "consumer",
        "kind": kind,
        "name": name,
        "state": state,
        "err": err,
        "pairing": {"producer": producer, "version": [version[0], version[1]]},
        "media": None,
    }
    return encode(FrameType.ANN, origin, seq, TS, body, psk)


def intent(
    consumer: str = "tv-den",
    producer: str = "cam-1",
    version: tuple[int, str] = (1, "cli-x"),
    *,
    op: str = "pair",
    origin: str = "cli-x",
    seq: int = 1,
    psk: bytes = PSK,
) -> bytes:
    body: dict[str, Any] = {
        "op": op,
        "consumer": consumer,
        "version": [version[0], version[1]],
    }
    if op == "pair":
        body["producer"] = producer
    return encode(FrameType.INT, origin, seq, TS, body, psk)


def ack(
    consumer: str = "tv-den",
    version: tuple[int, str] = (1, "cli-x"),
    *,
    applied: bool = True,
    reason: str = "applied",
    seq: int = 2,
    psk: bytes = PSK,
) -> bytes:
    body: dict[str, Any] = {
        "version": [version[0], version[1]],
        "applied": applied,
        "reason": reason,
    }
    return encode(FrameType.ACK, consumer, seq, TS, body, psk)


def one(snooper: Snooper, raw: bytes, now: float) -> Any:
    records = snooper.observe(raw, now)
    assert len(records) == 1
    return records[0]


# -- verdicts ----------------------------------------------------------------


def test_ok_announce_record_and_cache() -> None:
    snooper = Snooper(PSK)
    raw = ann_producer()
    record = one(snooper, raw, T0)
    assert record.verdict == "ok"
    assert record.origin == "cam-1"
    assert record.frame_type == "ann"
    assert record.frame is not None
    assert record.raw_len == len(raw)
    assert record.rel_time == 0.0
    assert snooper.nodes["cam-1"].role == "producer"
    assert snooper.nodes["cam-1"].last_seen == T0


def test_bad_sig_wrong_key_still_extracts_content() -> None:
    snooper = Snooper(PSK)
    record = one(snooper, ann_producer(psk=WRONG_PSK), T0)
    assert record.verdict == "bad-sig"
    assert record.origin == "cam-1"
    assert record.frame is not None
    assert record.frame.body["role"] == "producer"
    assert "BAD-SIG" in render_text(record)
    assert snooper.nodes == {}  # unauthenticated frames never enter the cache


def test_bad_sig_open_frame_on_keyed_network() -> None:
    snooper = Snooper(PSK)
    record = one(snooper, ann_consumer(psk=OPEN), T0)
    assert record.verdict == "bad-sig"
    assert record.origin == "tv-den"
    assert record.frame is not None


def test_open_snooper_accepts_open_frames() -> None:
    snooper = Snooper(OPEN)
    record = one(snooper, ann_producer(psk=OPEN), T0)
    assert record.verdict == "ok"


def test_bad_sig_does_not_advance_replay_state() -> None:
    snooper = Snooper(PSK)
    one(snooper, ann_producer(seq=10, psk=WRONG_PSK), T0)
    record = one(snooper, ann_producer(seq=1), T0 + 1)
    assert record.verdict == "ok"


def test_stale_reported_and_cache_unchanged() -> None:
    snooper = Snooper(PSK)
    one(snooper, ann_producer(seq=5, state="active"), T0)
    record = one(snooper, ann_producer(seq=3, state="idle"), T0 + 1)
    assert record.verdict == "stale"
    assert snooper.nodes["cam-1"].state == "active"
    assert snooper.nodes["cam-1"].last_seen == T0


def test_dup_identical_datagram() -> None:
    snooper = Snooper(PSK)
    raw = ann_producer(seq=7)
    assert one(snooper, raw, T0).verdict == "ok"
    assert one(snooper, raw, T0 + 0.01).verdict == "dup"
    # Same seq, different content: stale, not dup.
    other = ann_producer(seq=7, state="idle")
    assert one(snooper, other, T0 + 0.02).verdict == "stale"


def test_malformed_datagram() -> None:
    snooper = Snooper(PSK)
    record = one(snooper, b"{not json", T0)
    assert record.verdict == "malformed"
    assert record.origin == ""
    assert record.frame_type == ""
    assert record.frame is None
    assert record.raw_len == len(b"{not json")


def test_wrong_protocol_version() -> None:
    obj = {
        "v": 2,
        "t": "ann",
        "id": "cam-1",
        "seq": 1,
        "ts": TS,
        "body": {},
        "sig": "",
    }
    record = one(Snooper(PSK), canonical_bytes(obj), T0)
    assert record.verdict == "version"


def test_oversize_datagram() -> None:
    record = one(Snooper(PSK), b"x" * 1300, T0)
    assert record.verdict == "oversize"
    assert record.raw_len == 1300


def test_counts_accumulate() -> None:
    snooper = Snooper(PSK)
    one(snooper, ann_producer(), T0)
    one(snooper, b"junk", T0 + 1)
    one(snooper, ann_producer(psk=WRONG_PSK, seq=2), T0 + 2)
    assert snooper.counts["ok"] == 1
    assert snooper.counts["malformed"] == 1
    assert snooper.counts["bad-sig"] == 1


# -- topology reconstruction -------------------------------------------------


def test_topology_from_announce_intent_ack_sequence() -> None:
    snooper = Snooper(PSK)
    one(snooper, ann_producer(), T0)
    one(snooper, ann_consumer(), T0 + 0.1)
    one(snooper, intent(version=(1, "cli-x")), T0 + 0.2)
    one(snooper, ack(version=(1, "cli-x")), T0 + 0.3)
    one(
        snooper,
        ann_consumer(seq=3, producer="cam-1", version=(1, "cli-x"), state="active"),
        T0 + 0.4,
    )
    topo = snooper.topology()
    assert topo["producers"]["cam-1"]["media"] == {"group": "239.10.0.1", "port": 5004}
    consumer = topo["consumers"]["tv-den"]
    assert consumer["state"] == "active"
    assert consumer["pairing"] == {"producer": "cam-1", "version": [1, "cli-x"]}
    assert topo["intents"]["tv-den"] == {
        "op": "pair",
        "producer": "cam-1",
        "version": [1, "cli-x"],
        "acked": True,
        "applied": True,
        "reason": "applied",
    }
    assert topo["counts"]["ok"] == 5


def test_higher_version_intent_replaces_lower() -> None:
    snooper = Snooper(PSK)
    one(snooper, intent(version=(2, "cli-x"), seq=1), T0)
    one(snooper, intent(version=(1, "cli-y"), origin="cli-y", seq=1), T0 + 0.1)
    assert snooper.intents["tv-den"].version == (2, "cli-x")
    one(snooper, intent(version=(3, "cli-y"), origin="cli-y", seq=2), T0 + 0.2)
    assert snooper.intents["tv-den"].version == (3, "cli-y")
    assert snooper.intents["tv-den"].acked is False


def test_ack_for_stale_version_is_not_matched() -> None:
    snooper = Snooper(PSK)
    one(snooper, intent(version=(2, "cli-x")), T0)
    one(snooper, ack(version=(1, "cli-x"), seq=1), T0 + 0.1)
    assert snooper.intents["tv-den"].acked is False


def test_bye_removes_node_from_topology() -> None:
    snooper = Snooper(PSK)
    one(snooper, ann_producer(), T0)
    assert "cam-1" in snooper.nodes
    record = one(snooper, encode(FrameType.BYE, "cam-1", 2, TS, {}, PSK), T0 + 1)
    assert record.verdict == "ok"
    assert "cam-1" not in snooper.nodes
    assert snooper.topology()["producers"] == {}


# -- rendering ---------------------------------------------------------------


def test_render_text_lines() -> None:
    snooper = Snooper(PSK)
    ann_line = render_text(one(snooper, ann_consumer(), T0))
    assert "\n" not in ann_line
    assert "tv-den" in ann_line
    assert "ann" in ann_line
    assert "state=idle" in ann_line
    assert "unpaired" in ann_line
    int_line = render_text(one(snooper, intent(), T0 + 1))
    assert "tv-den<-cam-1" in int_line
    assert "[1,cli-x]" in int_line
    ack_line = render_text(one(snooper, ack(), T0 + 2))
    assert "applied=true" in ack_line
    assert "reason=applied" in ack_line
    evt = encode(
        FrameType.EVT, "tv-den", 3, TS, {"event": "media-up", "detail": {"n": 1}}, PSK
    )
    evt_line = render_text(one(snooper, evt, T0 + 3))
    assert "media-up" in evt_line


def test_render_text_relative_time() -> None:
    snooper = Snooper(PSK)
    one(snooper, ann_producer(seq=1), T0)
    record = one(snooper, ann_producer(seq=2), T0 + 5.25)
    assert record.rel_time == 5.25
    assert "+   5.250s" in render_text(record)


def test_render_json_is_single_line_sorted_keys() -> None:
    snooper = Snooper(PSK)
    for raw, now in [
        (ann_producer(), T0),
        (b"garbage", T0 + 1),
        (ann_consumer(psk=WRONG_PSK), T0 + 2),
    ]:
        for record in snooper.observe(raw, now):
            line = render_json(record)
            assert "\n" not in line
            obj = json.loads(line)
            assert line == json.dumps(
                obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
            assert obj["verdict"] == record.verdict
            assert obj["raw_len"] == record.raw_len


# -- never sends -------------------------------------------------------------


def test_snooper_has_no_send_path() -> None:
    """The snooper takes no socket and exposes nothing that transmits:
    the only inputs are (raw, now) via observe — a type-level guarantee
    that it cannot send even in principle."""
    parameters = list(inspect.signature(Snooper.__init__).parameters)
    assert parameters == ["self", "psk"]
    snooper = Snooper(PSK)
    for name in ("send", "sendto", "socket", "sock", "transmit", "write"):
        assert not hasattr(snooper, name)
    public = [name for name in vars(snooper) if not name.startswith("_")]
    assert sorted(public) == ["counts", "intents", "nodes"]
