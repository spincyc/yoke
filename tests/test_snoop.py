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
from yoke.core.types import INTENT_CACHE_TTL, PEER_FORGET, PEER_TIMEOUT, FrameType
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
    inputs consist of raw datagrams and injected timestamps, with no
    network capability or send callback."""
    parameters = list(inspect.signature(Snooper.__init__).parameters)
    assert parameters == ["self", "psk", "now", "wall_time"]
    snooper = Snooper(PSK)
    for name in ("send", "sendto", "socket", "sock", "transmit", "write"):
        assert not hasattr(snooper, name)
    public = [name for name in vars(snooper) if not name.startswith("_")]
    assert sorted(public) == ["counts", "intents", "nodes"]


def test_ack_observed_before_intent_preserves_fate() -> None:
    snooper = Snooper(PSK)
    one(snooper, ack(applied=False, reason="kind-mismatch", seq=1), T0)
    one(snooper, intent(), T0 + 1)
    assert snooper.intents["tv-den"].acked is True
    assert snooper.intents["tv-den"].applied is False
    assert snooper.intents["tv-den"].reason == "kind-mismatch"


def test_unauthenticated_early_ack_never_changes_intent() -> None:
    snooper = Snooper(PSK)
    one(snooper, ack(psk=WRONG_PSK), T0)
    one(snooper, intent(), T0 + 1)
    assert snooper.intents["tv-den"].acked is False


def test_invalid_body_does_not_enter_topology() -> None:
    for frame_type, body in (
        (
            FrameType.ANN,
            {
                "role": "consumer",
                "kind": "hdmi",
                "state": "idle",
                "name": "x",
                "pairing": {"producer": None, "version": [-1, "cli-x"]},
            },
        ),
        (
            FrameType.INT,
            {
                "op": "pair",
                "consumer": "bad\n",
                "producer": "cam-1",
                "version": [1, "cli-x"],
            },
        ),
        (
            FrameType.ACK,
            {"version": [1, "cli-x"], "applied": "true", "reason": "applied"},
        ),
    ):
        snooper = Snooper(PSK)
        record = one(snooper, encode(frame_type, "cli-x", 1, TS, body, PSK), T0)
        assert record.verdict == "malformed"
        assert not snooper.nodes and not snooper.intents
        assert record.frame is not None  # Bad bodies remain diagnosable.


def test_control_retention_is_bounded_and_expires(monkeypatch: Any) -> None:
    import yoke.snoop as snoop

    monkeypatch.setattr(snoop, "MAX_TRACKED", 3)
    snooper = Snooper(PSK)
    for index in range(5):
        origin = f"node-{index}"
        one(snooper, ann_producer(origin), T0)
        one(snooper, intent(consumer=f"tv-{index}", origin=f"cli-{index}"), T0)
        one(snooper, ack(consumer=f"tv-{index}"), T0)
    for mapping in (
        snooper.nodes,
        snooper.intents,
        snooper._high_seq,
        snooper._high_canon,
        snooper._replay_seen,
        snooper._intent_seen,
        snooper._acks,
    ):
        assert len(mapping) <= 3
    one(snooper, b"junk", T0 + PEER_FORGET + 1)
    assert not snooper.nodes and not snooper._high_seq and not snooper._high_canon
    one(snooper, b"junk", T0 + INTENT_CACHE_TTL + 1)
    assert not snooper.intents and not snooper._acks


def test_text_output_escapes_untrusted_line_breaks() -> None:
    snooper = Snooper(PSK)
    raw = ann_consumer(err="failure\nforged-line\r")
    record = one(snooper, raw, T0)
    text = render_text(record)
    assert "\n" not in text and "\r" not in text
    assert "failure\\nforged-line\\r" in text
    assert json.loads(render_json(record))["body"]["err"] == "failure\nforged-line\r"


def test_topology_does_not_expose_mutable_frame_body() -> None:
    snooper = Snooper(PSK)
    record = one(snooper, ann_producer(), T0)
    assert record.frame is not None
    record.frame.body["media"]["port"] = 9999
    topology = snooper.topology()
    assert topology["producers"]["cam-1"]["media"]["port"] == 5004
    topology["producers"]["cam-1"]["media"]["port"] = 7777
    assert snooper.topology()["producers"]["cam-1"]["media"]["port"] == 5004


def test_node_liveness_uses_last_accepted_announce() -> None:
    snooper = Snooper(PSK)
    one(snooper, ann_producer(seq=2), T0)
    one(snooper, ann_producer(seq=1), T0 + 7)
    producer = snooper.topology()["producers"]["cam-1"]
    assert producer["last_seen"] == T0
    assert producer["live"] is False


def test_expired_early_ack_cannot_attach_to_new_observation() -> None:
    snooper = Snooper(PSK)
    one(snooper, ack(), T0)
    one(snooper, intent(), T0 + INTENT_CACHE_TTL + 1)
    assert snooper.intents["tv-den"].acked is False


def test_announce_preserves_driver_readiness_evidence() -> None:
    body = json.loads(ann_producer())["body"]
    body["driver_status"] = {"detail": "backend ready"}
    raw = encode(FrameType.ANN, "cam-1", 1, TS, body, PSK)
    snooper = Snooper(PSK)
    record = one(snooper, raw, T0)
    assert snooper.topology()["producers"]["cam-1"]["driver_status"] == {
        "detail": "backend ready"
    }
    assert "backend ready" in render_text(record)


def test_topology_labels_open_mode_without_implying_authentication() -> None:
    snooper = Snooper(OPEN)
    one(snooper, ann_producer(psk=OPEN), T0)
    assert snooper.topology()["authentication"] == "open-unauthenticated"
    assert Snooper(PSK).topology()["authentication"] == "site-psk"


def test_silent_network_liveness_ages_without_datagrams() -> None:
    snooper = Snooper(PSK)
    one(snooper, ann_producer(), T0)
    counts = dict(snooper.counts)
    replay = dict(snooper._high_seq)

    assert snooper.topology(now=T0 + PEER_TIMEOUT)["producers"]["cam-1"]["live"]
    topology = snooper.topology(now=T0 + PEER_TIMEOUT + 0.001)
    assert topology["producers"]["cam-1"]["live"] is False
    assert topology["producers"]["cam-1"]["last_seen"] == T0
    assert snooper.topology()["producers"]["cam-1"]["live"] is False
    assert snooper.counts == counts
    assert snooper._high_seq == replay
    for name in ("send", "sendto", "socket", "sock", "transmit", "write"):
        assert not hasattr(snooper, name)


def test_silent_network_expires_retained_caches_without_datagrams() -> None:
    snooper = Snooper(PSK)
    one(snooper, ann_producer(), T0)
    one(snooper, intent(), T0)
    one(snooper, ack(), T0)
    counts = dict(snooper.counts)

    topology = snooper.topology(now=T0 + PEER_FORGET + 1)
    assert topology["producers"] == topology["consumers"] == {}
    assert (
        not snooper._high_seq and not snooper._high_canon and not snooper._replay_seen
    )
    assert topology["intents"]["tv-den"]["acked"] is True
    topology = snooper.topology(now=T0 + INTENT_CACHE_TTL + 1)
    assert topology["intents"] == {}
    assert not snooper._acks and not snooper._intent_seen
    assert snooper.counts == counts


def test_bad_signature_terminal_controls_are_inert_but_json_preserves_data() -> None:
    import unicodedata

    controls = "".join(chr(code) for code in (*range(32), *range(127, 160)))
    attack = "\x1b]52;c;aGVsbG8=\x07\u009b31m\u202e\u2066\u2028\u2029\U000e0001"
    payload = attack + controls + " Café 中🙂"
    snooper = Snooper(PSK)
    record = one(snooper, ann_consumer(psk=WRONG_PSK, err=payload), T0)
    assert record.verdict == "bad-sig"
    line = render_text(record)
    assert not any(
        unicodedata.category(char) in ("Cc", "Cf", "Cs", "Zl", "Zp") for char in line
    )
    assert "\\x1b]52;c;aGVsbG8=\\x07" in line
    assert "\\x9b31m\\u202e\\u2066\\u2028\\u2029\\U000e0001" in line
    assert "Café 中🙂" in line
    assert json.loads(render_json(record))["body"]["err"] == payload
    assert snooper.nodes == {} and snooper._high_seq == {}


def test_corrected_rejected_version_cannot_misattribute_applied_ack() -> None:
    snooper = Snooper(PSK)
    one(snooper, intent(producer="tv-den"), T0)
    one(snooper, ack(applied=False, reason="self-pair", seq=1), T0 + 0.1)
    one(snooper, intent(producer="cam-1", seq=2), T0 + 0.2)
    pending = snooper.topology()["intents"]["tv-den"]
    assert pending["producer"] == "cam-1"
    assert pending["ambiguous"] is True
    assert pending["acked"] is False and pending["applied"] is None
    one(snooper, ack(seq=2), T0 + 0.3)
    ambiguous = snooper.topology()["intents"]["tv-den"]
    assert ambiguous["producer"] == "cam-1"
    assert ambiguous["acked"] is True and ambiguous["ack_applied"] is True
    assert ambiguous["applied"] is None
    one(snooper, intent(version=(2, "cli-x"), seq=3), T0 + 0.4)
    one(snooper, ack(version=(2, "cli-x"), seq=3), T0 + 0.5)
    resolved = snooper.topology()["intents"]["tv-den"]
    assert resolved["applied"] is True and "ambiguous" not in resolved


def test_same_version_conflict_is_ambiguous_even_without_rejection_observed() -> None:
    snooper = Snooper(PSK)
    one(snooper, ack(seq=1), T0)
    one(snooper, intent(producer="cam-1"), T0 + 0.1)
    assert snooper.intents["tv-den"].applied is True
    one(snooper, intent(producer="cam-2", seq=2), T0 + 0.2)
    one(snooper, ack(seq=2), T0 + 0.3)
    assert snooper.intents["tv-den"].ambiguous is True
    assert snooper.intents["tv-den"].applied is None


def test_unauthenticated_correction_and_unknown_extensions_do_not_change_identity() -> (
    None
):
    snooper = Snooper(PSK)
    one(snooper, intent(producer="cam-1"), T0)
    one(snooper, ack(seq=1), T0 + 0.1)
    one(snooper, intent(producer="cam-2", seq=2, psk=WRONG_PSK), T0 + 0.2)
    body = json.loads(intent())["body"]
    body["future-extension"] = {"diagnostic": "ignored"}
    one(snooper, encode(FrameType.INT, "cli-x", 2, TS, body, PSK), T0 + 0.3)
    assert snooper.intents["tv-den"].producer == "cam-1"
    assert snooper.intents["tv-den"].applied is True
    assert snooper.intents["tv-den"].ambiguous is False


def test_future_sequence_matches_agent_and_cannot_hide_legitimate_announce() -> None:
    from yoke.core.frames import decode
    from yoke.core.node import Node
    from yoke.core.types import (
        MAX_SEQUENCE,
        NS_PER_S,
        Kind,
        NodeConfig,
        Persist,
        PersistedState,
        Role,
    )

    node = Node(
        NodeConfig("relay", Role.PRODUCER, Kind.HDMI),
        state=PersistedState(),
        now=T0,
        wall_time=TS,
    )
    save = node.start(T0)[0]
    assert isinstance(save, Persist)
    node.on_persist_result(save.request_id, True, T0)
    snooper = Snooper(PSK, now=T0, wall_time=TS)
    future = ann_producer(seq=MAX_SEQUENCE)
    node.on_frame(decode(future, PSK), future, T0)
    assert node._drops["stale"] == 1
    record = one(snooper, future, T0)
    assert record.verdict == "stale" and record.frame is not None
    assert not snooper._high_seq and not snooper.nodes
    legitimate = ann_producer(seq=int(TS) * NS_PER_S + 1)
    node.on_frame(decode(legitimate, PSK), legitimate, T0 + 0.1)
    assert one(snooper, legitimate, T0 + 0.1).verdict == "ok"
    assert "cam-1" in snooper.nodes
    assert node._drops["stale"] == 1


def test_future_sequence_ceiling_advances_from_injected_wall_origin() -> None:
    from yoke.core.types import NS_PER_S, SEQ_FUTURE_MARGIN_S

    elapsed = 2.5
    ceiling = (int(TS + elapsed) + SEQ_FUTURE_MARGIN_S) * NS_PER_S
    for seq, expected in ((ceiling, "ok"), (ceiling + 1, "stale")):
        snooper = Snooper(PSK, now=T0, wall_time=TS)
        assert one(snooper, ann_producer(seq=seq), T0 + elapsed).verdict == expected
