"""Unit tests for the sans-I/O agent state machine (yoke.core.node.Node).

No sockets, no wall clock, no sleeps: a small harness constructs nodes,
feeds them frames built with the real wire codec (encode -> decode with a
fixed PSK), and advances time by calling ``poll`` at chosen instants.
"""

from __future__ import annotations

import pytest

from yoke.core.frames import decode, encode
from yoke.core.node import Node
from yoke.core.types import (
    ANN_INTERVAL,
    ANN_JITTER,
    INTENT_CACHE_TTL,
    MAX_SEQUENCE,
    PEER_TIMEOUT,
    SEQUENCE_RESERVATION,
    SUPPRESSION_MAX,
    SUPPRESSION_MIN,
    AttachMedia,
    Body,
    DetachMedia,
    DriverStatus,
    DropReason,
    Effect,
    Frame,
    FrameType,
    Kind,
    Log,
    NodeConfig,
    NodeState,
    Pairing,
    Persist,
    PersistedState,
    Role,
    SendFrame,
    Version,
)

PSK = b"unit-test-psk"
T0 = 1_754_000_000.0
SEQ0 = int(T0) * 1_000_000_000  # nanosecond seq floor at T0 (ADR 0007)
HDMI_MEDIA: Body = {"group": "239.10.0.1", "port": 5004}
HDMI_MEDIA_2: Body = {"group": "239.10.0.2", "port": 5004}
USB_MEDIA: Body = {"host": "10.0.0.9", "port": 3240, "busids": ["1-1"]}

ANN_MIN = ANN_INTERVAL * (1 - ANN_JITTER)
ANN_MAX = ANN_INTERVAL * (1 + ANN_JITTER)


def build_frame(
    frame_type: FrameType,
    origin: str,
    seq: int,
    body: Body,
    ts: float = T0,
) -> tuple[Frame, bytes]:
    raw = encode(frame_type, origin, seq, ts, body, PSK)
    return decode(raw, PSK), raw


def producer_ann(
    media: Body | None = None,
    *,
    kind: str = "hdmi",
    state: str = "idle",
    err: str | None = None,
    name: str = "enc",
) -> Body:
    return {
        "role": "producer",
        "kind": kind,
        "name": name,
        "state": state,
        "err": err,
        "pairing": None,
        "media": HDMI_MEDIA if media is None else media,
    }


def consumer_ann(
    version: Version,
    producer: str | None = None,
    *,
    kind: str = "hdmi",
    state: str = "idle",
) -> Body:
    return {
        "role": "consumer",
        "kind": kind,
        "name": "dec",
        "state": state,
        "err": None,
        "pairing": {"producer": producer, "version": list(version)},
        "media": None,
    }


def pair_body(consumer: str, producer: str, version: Version) -> Body:
    return {
        "op": "pair",
        "consumer": consumer,
        "producer": producer,
        "version": list(version),
    }


def unpair_body(consumer: str, version: Version) -> Body:
    return {"op": "unpair", "consumer": consumer, "version": list(version)}


def sends(fx: list[Effect], frame_type: FrameType) -> list[SendFrame]:
    return [e for e in fx if isinstance(e, SendFrame) and e.type is frame_type]


def evts(fx: list[Effect]) -> list[str]:
    return [str(e.body["event"]) for e in sends(fx, FrameType.EVT)]


def evt_detail(fx: list[Effect], slug: str) -> Body:
    for e in sends(fx, FrameType.EVT):
        if e.body["event"] == slug:
            detail: Body = e.body["detail"]
            return detail
    raise AssertionError(f"no evt {slug!r} in {fx!r}")


def attaches(fx: list[Effect]) -> list[AttachMedia]:
    return [e for e in fx if isinstance(e, AttachMedia)]


def ints(fx: list[Effect]) -> list[SendFrame]:
    return sends(fx, FrameType.INT)


class Loop:
    """Drives one Node with fabricated frames and a fake clock."""

    def __init__(
        self,
        *,
        role: Role = Role.CONSUMER,
        kind: Kind = Kind.HDMI,
        node_id: str = "node-1",
        state: PersistedState | None = None,
        media: Body | None = None,
        seed: int = 0,
        now: float = T0,
    ) -> None:
        self.node = Node(
            NodeConfig(node_id=node_id, role=role, kind=kind, name="n"),
            state=state or PersistedState(),
            now=now,
            media=media,
            seed=seed,
        )
        self.now = now
        self._seqs: dict[str, int] = {}
        self.start_fx = self.complete(self.node.start(now))

    def complete(self, effects: list[Effect]) -> list[Effect]:
        """Model successful durable storage, preserving effect-order assertions."""
        result: list[Effect] = []
        for effect in effects:
            result.append(effect)
            if isinstance(effect, Persist):
                result.extend(
                    self.complete(
                        self.node.on_persist_result(effect.request_id, True, self.now)
                    )
                )
        return result

    def feed(
        self,
        frame_type: FrameType,
        origin: str,
        body: Body,
        *,
        seq: int | None = None,
    ) -> list[Effect]:
        if seq is None:
            seq = self._seqs.get(origin, 0) + 1
        self._seqs[origin] = max(self._seqs.get(origin, 0), seq)
        frame, raw = build_frame(frame_type, origin, seq, body)
        return self.complete(self.node.on_frame(frame, raw, self.now))

    def feed_pair(self, frame: Frame, raw: bytes) -> list[Effect]:
        return self.complete(self.node.on_frame(frame, raw, self.now))

    def poll_at(self, t: float) -> list[Effect]:
        assert t >= self.now, "time must not go backwards"
        self.now = t
        return self.complete(self.node.poll(t))


def paired(loop: Loop, producer: str = "prod-1", version: Version = (1, "cli")) -> None:
    """Announce a producer, pair the consumer to it."""
    loop.feed(FrameType.ANN, producer, producer_ann())
    loop.feed(FrameType.INT, "cli", pair_body("node-1", producer, version))


def paired_active(
    loop: Loop, producer: str = "prod-1", version: Version = (1, "cli")
) -> None:
    paired(loop, producer, version)
    loop.node.on_driver_status(DriverStatus(NodeState.ACTIVE), loop.now)


# -- startup and announce cadence -------------------------------------------


def test_start_effect_order_and_shapes() -> None:
    loop = Loop()
    fx = loop.start_fx
    assert [type(e) for e in fx] == [Persist, SendFrame, SendFrame]
    persist = fx[0]
    assert isinstance(persist, Persist)
    assert persist.state.seq == SEQ0 + SEQUENCE_RESERVATION
    assert persist.state.pairing == Pairing(None, (0, ""))
    started, ann = sends(fx, FrameType.EVT)[0], sends(fx, FrameType.ANN)[0]
    assert started.body == {"event": "started", "detail": {}}
    assert started.seq == SEQ0 + 1
    assert ann.seq == SEQ0 + 2
    assert ann.body == {
        "role": "consumer",
        "kind": "hdmi",
        "name": "n",
        "state": "idle",
        "err": None,
        "pairing": {"producer": None, "version": [0, ""]},
        "media": None,
    }


def test_seq_floor_uses_now_over_stale_persisted_seq() -> None:
    loop = Loop(state=PersistedState(seq=10))
    started = sends(loop.start_fx, FrameType.EVT)[0]
    assert started.seq == SEQ0 + 1


def test_seq_floor_uses_persisted_seq_when_higher() -> None:
    high = SEQ0 + 9999
    loop = Loop(state=PersistedState(seq=high))
    started = sends(loop.start_fx, FrameType.EVT)[0]
    assert started.seq == high + 1


def test_persisted_pairing_resumes_pending() -> None:
    state = PersistedState(seq=0, pairing=Pairing("prod-1", (2, "cli")))
    loop = Loop(state=state)
    assert loop.node.node_state is NodeState.PENDING
    assert loop.node.pairing == Pairing("prod-1", (2, "cli"))
    ann = sends(loop.start_fx, FrameType.ANN)[0]
    assert ann.body["state"] == "pending"
    assert ann.body["pairing"] == {"producer": "prod-1", "version": [2, "cli"]}


def test_announce_cadence_within_jitter_bounds() -> None:
    loop = Loop(seed=3)
    prev = T0
    for _ in range(6):
        due = loop.node.next_deadline()
        assert ANN_MIN - 1e-9 <= due - prev <= ANN_MAX + 1e-9
        fx = loop.poll_at(due)
        assert len(sends(fx, FrameType.ANN)) == 1
        prev = due


def test_announce_schedule_deterministic_for_same_seed() -> None:
    a, b = Loop(seed=42), Loop(seed=42)
    for _ in range(5):
        da, db = a.node.next_deadline(), b.node.next_deadline()
        assert da == db
        a.poll_at(da)
        b.poll_at(db)
    c, d = Loop(seed=1), Loop(seed=2)
    assert c.node.next_deadline() != d.node.next_deadline()


# -- consumer intent handling ------------------------------------------------


def test_pair_intent_unknown_producer_effect_order() -> None:
    loop = Loop()
    fx = loop.feed(FrameType.INT, "cli", pair_body("node-1", "prod-1", (1, "cli")))
    assert [type(e) for e in fx] == [
        Persist,
        SendFrame,
        SendFrame,
        SendFrame,
        SendFrame,
    ]
    persist = fx[0]
    assert isinstance(persist, Persist)
    assert persist.state.pairing == Pairing("prod-1", (1, "cli"))
    pending, ack, applied, ann = fx[1], fx[2], fx[3], fx[4]
    assert isinstance(pending, SendFrame) and pending.body["event"] == "media-pending"
    assert isinstance(ack, SendFrame) and ack.type is FrameType.ACK
    assert ack.body == {"version": [1, "cli"], "applied": True, "reason": "applied"}
    assert persist.state.seq >= ack.seq  # reserved durably before releasing the ack
    assert isinstance(applied, SendFrame)
    assert applied.body == {
        "event": "pairing-applied",
        "detail": {"producer": "prod-1", "version": [1, "cli"]},
    }
    assert isinstance(ann, SendFrame) and ann.type is FrameType.ANN
    assert ann.body["pairing"] == {"producer": "prod-1", "version": [1, "cli"]}
    assert ack.seq < applied.seq < ann.seq
    assert loop.node.node_state is NodeState.PENDING


def test_pair_intent_known_producer_attaches() -> None:
    loop = Loop()
    loop.feed(FrameType.ANN, "prod-1", producer_ann())
    fx = loop.feed(FrameType.INT, "cli", pair_body("node-1", "prod-1", (1, "cli")))
    assert isinstance(fx[0], Persist)
    attach = fx[1]
    assert attach == AttachMedia("prod-1", HDMI_MEDIA)
    assert "media-pending" not in evts(fx)
    assert sends(fx, FrameType.ACK)[0].body["applied"] is True
    assert loop.node.node_state is NodeState.PENDING


def test_idempotent_reack_of_applied_intent() -> None:
    loop = Loop()
    body = pair_body("node-1", "prod-1", (1, "cli"))
    loop.feed(FrameType.INT, "cli", body)
    fx = loop.feed(FrameType.INT, "cli", body)  # CLI retry, new seq
    assert len(fx) == 1
    ack = fx[0]
    assert isinstance(ack, SendFrame) and ack.type is FrameType.ACK
    assert ack.body == {"version": [1, "cli"], "applied": True, "reason": "applied"}


def test_lower_version_superseded() -> None:
    loop = Loop()
    loop.feed(FrameType.INT, "cli", pair_body("node-1", "prod-1", (2, "cli")))
    fx = loop.feed(FrameType.INT, "cli", pair_body("node-1", "prod-2", (1, "cli")))
    assert len(fx) == 1
    ack = fx[0]
    assert isinstance(ack, SendFrame)
    assert ack.body == {"version": [1, "cli"], "applied": False, "reason": "superseded"}
    assert loop.node.pairing.producer == "prod-1"


def test_equal_version_different_intent_superseded() -> None:
    loop = Loop()
    loop.feed(FrameType.INT, "cli", pair_body("node-1", "prod-1", (1, "cli")))
    fx = loop.feed(FrameType.INT, "cli", pair_body("node-1", "prod-2", (1, "cli")))
    assert len(fx) == 1
    ack = fx[0]
    assert isinstance(ack, SendFrame)
    assert ack.body["applied"] is False and ack.body["reason"] == "superseded"
    assert loop.node.pairing.producer == "prod-1"


def test_version_tiebreak_by_origin() -> None:
    loop = Loop()
    loop.feed(FrameType.INT, "cli-a", pair_body("node-1", "prod-1", (1, "cli-a")))
    fx = loop.feed(FrameType.INT, "cli-b", pair_body("node-1", "prod-2", (1, "cli-b")))
    assert sends(fx, FrameType.ACK)[0].body["applied"] is True
    assert loop.node.pairing == Pairing("prod-2", (1, "cli-b"))
    fx = loop.feed(FrameType.INT, "cli-a", pair_body("node-1", "prod-1", (1, "cli-a")))
    ack = sends(fx, FrameType.ACK)[0]
    assert ack.body["applied"] is False and ack.body["reason"] == "superseded"
    assert loop.node.pairing.producer == "prod-2"


def test_unpair() -> None:
    loop = Loop()
    paired_active(loop)
    fx = loop.feed(FrameType.INT, "cli", unpair_body("node-1", (2, "cli")))
    assert [type(e) for e in fx] == [
        Persist,
        DetachMedia,
        SendFrame,
        SendFrame,
        SendFrame,
    ]
    ack = sends(fx, FrameType.ACK)[0]
    assert ack.body == {"version": [2, "cli"], "applied": True, "reason": "applied"}
    assert evt_detail(fx, "pairing-applied") == {
        "producer": None,
        "version": [2, "cli"],
    }
    ann = sends(fx, FrameType.ANN)[0]
    assert ann.body["pairing"] == {"producer": None, "version": [2, "cli"]}
    assert loop.node.node_state is NodeState.PENDING
    assert loop.node.pairing == Pairing(None, (2, "cli"))


def test_reject_self_pair() -> None:
    loop = Loop()
    fx = loop.feed(FrameType.INT, "cli", pair_body("node-1", "node-1", (1, "cli")))
    assert len(fx) == 1
    ack = fx[0]
    assert isinstance(ack, SendFrame)
    assert ack.body == {"version": [1, "cli"], "applied": False, "reason": "self-pair"}
    assert loop.node.pairing == Pairing(None, (0, ""))
    assert loop.node.node_state is NodeState.IDLE


def test_reject_bad_producer_id() -> None:
    loop = Loop()
    fx = loop.feed(FrameType.INT, "cli", pair_body("node-1", "Bad_ID!", (1, "cli")))
    assert len(fx) == 1
    ack = fx[0]
    assert isinstance(ack, SendFrame)
    assert ack.body["applied"] is False
    assert ack.body["reason"] == "unknown-producer"
    assert loop.node.pairing == Pairing(None, (0, ""))
    # a pair intent missing "producer" entirely is also unknown-producer
    fx = loop.feed(
        FrameType.INT,
        "cli",
        {"op": "pair", "consumer": "node-1", "version": [2, "cli"]},
    )
    ack2 = sends(fx, FrameType.ACK)[0]
    assert ack2.body["reason"] == "unknown-producer"


def test_reject_kind_mismatch() -> None:
    loop = Loop(kind=Kind.HDMI)
    loop.feed(FrameType.ANN, "prod-u", producer_ann(USB_MEDIA, kind="usb"))
    fx = loop.feed(FrameType.INT, "cli", pair_body("node-1", "prod-u", (1, "cli")))
    assert len(fx) == 1
    ack = fx[0]
    assert isinstance(ack, SendFrame)
    assert ack.body["applied"] is False
    assert ack.body["reason"] == "kind-mismatch"
    assert loop.node.pairing == Pairing(None, (0, ""))


def test_malformed_intent_counts_and_drops() -> None:
    loop = Loop()
    fx = loop.feed(FrameType.INT, "cli", {"op": "explode", "consumer": "node-1"})
    assert not sends(fx, FrameType.ACK)
    assert all(isinstance(e, Log) for e in fx)
    fx = loop.poll_at(T0 + 0.1)
    assert evt_detail(fx, "rx-drop")["malformed"] == 1


# -- driver lifecycle --------------------------------------------------------


def test_pending_to_active_on_producer_announce() -> None:
    loop = Loop()
    loop.feed(FrameType.INT, "cli", pair_body("node-1", "prod-1", (1, "cli")))
    assert loop.node.node_state is NodeState.PENDING
    fx = loop.feed(FrameType.ANN, "prod-1", producer_ann())
    assert attaches(fx) == [AttachMedia("prod-1", HDMI_MEDIA)]
    fx = loop.node.on_driver_status(DriverStatus(NodeState.ACTIVE), loop.now)
    assert evts(fx) == ["media-up"]
    assert len(sends(fx, FrameType.ANN)) == 1
    assert loop.node.node_state is NodeState.ACTIVE
    # media-up only on transition
    fx = loop.node.on_driver_status(DriverStatus(NodeState.ACTIVE), loop.now)
    assert fx == []


def test_media_change_reattaches_and_same_media_does_not() -> None:
    loop = Loop()
    paired_active(loop)
    fx = loop.feed(FrameType.ANN, "prod-1", producer_ann(HDMI_MEDIA_2))
    assert attaches(fx) == [AttachMedia("prod-1", HDMI_MEDIA_2)]
    fx = loop.feed(FrameType.ANN, "prod-1", producer_ann(HDMI_MEDIA_2))
    assert attaches(fx) == []


def test_driver_error_backoff_retries_and_recovery() -> None:
    loop = Loop()
    paired(loop)
    t = loop.now
    for delay in (1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0):
        fx = loop.node.on_driver_status(DriverStatus(NodeState.ERROR, "boom"), t)
        assert evt_detail(fx, "driver-error") == {"detail": "boom"}
        assert loop.node.node_state is NodeState.ERROR
        # keep the producer announcing so it does not time out during the wait
        step = t
        while step + 2.0 < t + delay - 0.01:
            step += 2.0
            fx = loop.poll_at(step)
            assert attaches(fx) == []
            loop.feed(FrameType.ANN, "prod-1", producer_ann())
        fx = loop.poll_at(t + delay - 0.01)
        assert attaches(fx) == []
        fx = loop.poll_at(t + delay)
        assert attaches(fx) == [AttachMedia("prod-1", HDMI_MEDIA)]
        assert "driver-retry" in evts(fx)
        t = t + delay
    # recovery resets the backoff to 1 s
    fx = loop.node.on_driver_status(DriverStatus(NodeState.ACTIVE), t)
    assert "media-up" in evts(fx)
    loop.node.on_driver_status(DriverStatus(NodeState.ERROR, "boom"), t)
    fx = loop.poll_at(t + 1.0)
    assert attaches(fx) == [AttachMedia("prod-1", HDMI_MEDIA)]


def test_paired_producer_loss_cancels_driver_retry() -> None:
    loop = Loop()
    paired(loop)
    t = loop.now
    loop.node.on_driver_status(DriverStatus(NodeState.ERROR, "boom"), t)
    # producer times out before the retry backoff elapses fully
    fx = loop.poll_at(t + PEER_TIMEOUT + 0.1)
    assert "peer-lost" in evts(fx)
    assert "media-pending" in evts(fx)
    assert attaches(fx) == []
    assert loop.node.node_state is NodeState.PENDING
    # the producer's return re-attaches instead
    fx = loop.feed(FrameType.ANN, "prod-1", producer_ann())
    assert attaches(fx) == [AttachMedia("prod-1", HDMI_MEDIA)]


def test_error_ann_carries_err_detail() -> None:
    loop = Loop()
    paired(loop)
    fx = loop.node.on_driver_status(
        DriverStatus(NodeState.ERROR, "no signal"), loop.now
    )
    ann = sends(fx, FrameType.ANN)[0]
    assert ann.body["state"] == "error"
    assert ann.body["err"] == "no signal"


# -- peer lifecycle ----------------------------------------------------------


def test_peer_timeout_media_pending_keeps_pairing() -> None:
    loop = Loop()
    paired_active(loop)
    fx = loop.poll_at(T0 + PEER_TIMEOUT + 0.1)
    assert evt_detail(fx, "peer-lost") == {"peer": "prod-1", "reason": "timeout"}
    assert "media-pending" in evts(fx)
    assert not any(isinstance(e, DetachMedia) for e in fx)
    assert loop.node.node_state is NodeState.PENDING
    assert loop.node.pairing.producer == "prod-1"
    assert loop.node.peers["prod-1"].departed is False  # timeout, not bye
    # fires once
    fx = loop.poll_at(T0 + PEER_TIMEOUT + 1.0)
    assert "peer-lost" not in evts(fx)


def test_producer_return_reattaches() -> None:
    loop = Loop()
    paired_active(loop)
    loop.poll_at(T0 + PEER_TIMEOUT + 0.1)
    fx = loop.feed(FrameType.ANN, "prod-1", producer_ann())
    assert evt_detail(fx, "peer-returned") == {"peer": "prod-1"}
    assert attaches(fx) == [AttachMedia("prod-1", HDMI_MEDIA)]
    fx = loop.node.on_driver_status(DriverStatus(NodeState.ACTIVE), loop.now)
    assert "media-up" in evts(fx)
    assert loop.node.node_state is NodeState.ACTIVE


def test_bye_marks_departed_and_goes_pending() -> None:
    loop = Loop()
    paired_active(loop)
    fx = loop.feed(FrameType.BYE, "prod-1", {})
    assert evt_detail(fx, "peer-lost") == {"peer": "prod-1", "reason": "bye"}
    assert "media-pending" in evts(fx)
    assert loop.node.node_state is NodeState.PENDING
    assert loop.node.peers["prod-1"].departed is True
    # no second peer-lost at the timeout
    fx = loop.poll_at(T0 + PEER_TIMEOUT + 1.0)
    assert "peer-lost" not in evts(fx)


def test_bye_from_unknown_peer_is_ignored() -> None:
    loop = Loop()
    assert loop.feed(FrameType.BYE, "ghost-1", {}) == []


def test_peers_property_is_a_copy_with_peer_details() -> None:
    loop = Loop()
    loop.feed(FrameType.ANN, "prod-1", producer_ann(name="enc"))
    peers = loop.node.peers
    peer = peers["prod-1"]
    assert peer.node_id == "prod-1"
    assert peer.role is Role.PRODUCER
    assert peer.kind is Kind.HDMI
    assert peer.name == "enc"
    assert peer.media == HDMI_MEDIA
    assert peer.pairing is None
    assert peer.last_seen == T0
    del peers["prod-1"]
    assert "prod-1" in loop.node.peers


# -- replay defense and drop accounting --------------------------------------


def test_replay_stale_dup_counting_and_rx_drop_rate_limit() -> None:
    loop = Loop()
    body = {"event": "noop", "detail": {}}
    frame5, raw5 = build_frame(FrameType.EVT, "peer-9", 5, body)
    assert loop.feed_pair(frame5, raw5) == []
    # lower seq -> stale
    frame4, raw4 = build_frame(FrameType.EVT, "peer-9", 4, body)
    assert loop.feed_pair(frame4, raw4) == []
    # equal seq, identical bytes -> dup (counted, but never reported)
    assert loop.feed_pair(frame5, raw5) == []
    # equal seq, different bytes -> stale
    frame5b, raw5b = build_frame(
        FrameType.EVT, "peer-9", 5, {"event": "x", "detail": {}}
    )
    assert loop.feed_pair(frame5b, raw5b) == []
    loop.node.on_drop(DropReason.AUTH, loop.now)
    fx = loop.poll_at(T0 + 0.1)
    detail = evt_detail(fx, "rx-drop")
    assert detail == {
        "malformed": 0,
        "version": 0,
        "auth": 1,
        "stale": 2,
        "oversize": 0,
    }
    assert "dup" not in detail
    # rate limit: changed counters do not re-emit within ANN_INTERVAL
    loop.node.on_drop(DropReason.OVERSIZE, loop.now)
    fx = loop.poll_at(T0 + 0.1 + ANN_INTERVAL - 0.2)
    assert "rx-drop" not in evts(fx)
    fx = loop.poll_at(T0 + 0.1 + ANN_INTERVAL)
    assert evt_detail(fx, "rx-drop")["oversize"] == 1
    # unchanged counters never re-emit
    fx = loop.poll_at(T0 + 0.1 + 3 * ANN_INTERVAL)
    assert "rx-drop" not in evts(fx)


def test_own_frames_ignored_silently() -> None:
    loop = Loop()
    body = consumer_ann((0, ""))
    frame, raw = build_frame(FrameType.ANN, "node-1", 7, body)
    assert loop.feed_pair(frame, raw) == []
    assert loop.feed_pair(frame, raw) == []  # not even a dup
    fx = loop.poll_at(T0 + 0.1)
    assert "rx-drop" not in evts(fx)
    assert loop.node.peers == {}


# -- anti-entropy ------------------------------------------------------------


def test_anti_entropy_reissues_cached_body_under_own_seq() -> None:
    loop = Loop(role=Role.PRODUCER, node_id="watch-1", media=HDMI_MEDIA)
    body = pair_body("con-9", "prod-1", (3, "cli"))
    assert loop.feed(FrameType.INT, "cli", body) == []
    loop.feed(FrameType.ANN, "con-9", consumer_ann((1, "cli"), "prod-0"))
    due = loop.node.next_deadline()
    assert T0 + SUPPRESSION_MIN <= due <= T0 + SUPPRESSION_MAX
    fx = loop.poll_at(due)
    assert all(isinstance(e, SendFrame) for e in fx)  # never SendRaw (ADR 0009)
    (reissue,) = ints(fx)
    # the cached BODY is re-issued: version (incl. original author) preserved
    assert reissue.body == body
    assert reissue.body["version"] == [3, "cli"]
    # ... but under the relayer's own seq counter, not the author's seq 1
    assert reissue.seq > SEQ0
    ann_after = sends(loop.poll_at(loop.node.next_deadline()), FrameType.ANN)[0]
    assert ann_after.seq > reissue.seq  # same counter as the relayer's own frames
    # fires at most once per scheduling
    fx = loop.poll_at(loop.now + ANN_INTERVAL)
    assert ints(fx) == []


def test_anti_entropy_suppressed_by_newer_intent() -> None:
    loop = Loop(role=Role.PRODUCER, node_id="watch-1", media=HDMI_MEDIA)
    loop.feed(FrameType.INT, "cli", pair_body("con-9", "prod-1", (3, "cli")))
    loop.feed(FrameType.ANN, "con-9", consumer_ann((1, "cli"), "prod-0"))
    due = loop.node.next_deadline()
    assert due <= T0 + SUPPRESSION_MAX
    loop.feed(FrameType.INT, "cli", pair_body("con-9", "prod-2", (4, "cli")))
    fx = loop.poll_at(T0 + SUPPRESSION_MAX + 0.1)
    assert ints(fx) == []


def test_anti_entropy_suppressed_by_peer_reissue() -> None:
    loop = Loop(role=Role.PRODUCER, node_id="watch-1", media=HDMI_MEDIA)
    body = pair_body("con-9", "prod-1", (3, "cli"))
    loop.feed(FrameType.INT, "cli", body)
    loop.feed(FrameType.ANN, "con-9", consumer_ann((1, "cli"), "prod-0"))
    assert loop.node.next_deadline() <= T0 + SUPPRESSION_MAX
    # another agent won the suppression race and re-issued the same intent
    # body under its own origin and seq: heard via the ordinary int path
    assert loop.feed(FrameType.INT, "relay-2", body) == []
    fx = loop.poll_at(T0 + SUPPRESSION_MAX + 0.1)
    assert ints(fx) == []


def test_anti_entropy_cancelled_by_current_ann() -> None:
    loop = Loop(role=Role.PRODUCER, node_id="watch-1", media=HDMI_MEDIA)
    loop.feed(FrameType.INT, "cli", pair_body("con-9", "prod-1", (3, "cli")))
    loop.feed(FrameType.ANN, "con-9", consumer_ann((1, "cli"), "prod-0"))
    loop.feed(FrameType.ANN, "con-9", consumer_ann((3, "cli"), "prod-1"))
    fx = loop.poll_at(T0 + SUPPRESSION_MAX + 0.1)
    assert ints(fx) == []


def test_intent_cache_ttl_expiry() -> None:
    loop = Loop(role=Role.PRODUCER, node_id="watch-1", media=HDMI_MEDIA)
    loop.feed(FrameType.INT, "cli", pair_body("con-9", "prod-1", (3, "cli")))
    late = T0 + INTENT_CACHE_TTL + 5.0
    loop.poll_at(late)
    loop.feed(FrameType.ANN, "con-9", consumer_ann((1, "cli"), "prod-0"))
    assert loop.node.next_deadline() > late + SUPPRESSION_MAX
    fx = loop.poll_at(late + SUPPRESSION_MAX + 0.1)
    assert ints(fx) == []


def test_consumer_caches_and_serves_intents_for_other_consumers() -> None:
    loop = Loop()  # consumer role
    body = pair_body("con-9", "prod-1", (3, "cli"))
    fx = loop.feed(FrameType.INT, "cli", body)
    assert sends(fx, FrameType.ACK) == []  # not addressed to us: no ack
    assert loop.node.pairing == Pairing(None, (0, ""))
    loop.feed(FrameType.ANN, "con-9", consumer_ann((1, "cli"), "prod-0"))
    due = loop.node.next_deadline()
    assert due <= T0 + SUPPRESSION_MAX
    fx = loop.poll_at(due)
    (reissue,) = ints(fx)
    assert reissue.body == body


def test_livelock_regression_reissue_survives_authors_later_traffic() -> None:
    """ADR 0009: verbatim resends livelock once the author sent higher seqs.

    Bystander B caches cli-x's intent (seq 1) for consumer A, then hears
    cli-x seq 2. A verbatim copy of seq 1 would now be stale-dropped by
    anyone who heard seq 2 — including A after partial reception. The
    re-issue must therefore be a fresh int under B's own origin and seq.
    """
    loop = Loop(role=Role.PRODUCER, node_id="watch-1", media=HDMI_MEDIA)
    body = pair_body("con-9", "prod-1", (3, "cli-x"))
    loop.feed(FrameType.INT, "cli-x", body)  # cli-x seq 1: cached for con-9
    # any later frame from the author bumps its replay high-water mark
    assert loop.feed(FrameType.EVT, "cli-x", {"event": "noop", "detail": {}}) == []
    loop.feed(FrameType.ANN, "con-9", consumer_ann((1, "cli-x"), "prod-0"))
    due = loop.node.next_deadline()
    assert due <= T0 + SUPPRESSION_MAX
    fx = loop.poll_at(due)
    (reissue,) = ints(fx)
    assert reissue.body == body
    assert reissue.body["version"] == [3, "cli-x"]  # authorship preserved
    # fresh seq from B's own counter, far above anything cli-x ever sent,
    # so con-9's replay defense accepts the relay
    assert reissue.seq > SEQ0
    assert reissue.seq > 2


def test_rejection_ack_evicts_cache_and_blocks_recache() -> None:
    loop = Loop(role=Role.PRODUCER, node_id="watch-1", media=HDMI_MEDIA)
    doomed = pair_body("con-9", "con-9", (3, "cli"))  # self-pair: A rejects it
    loop.feed(FrameType.INT, "cli", doomed)
    fx = loop.feed(
        FrameType.ACK,
        "con-9",
        {"version": [3, "cli"], "applied": False, "reason": "self-pair"},
    )
    assert fx == []
    # A still announces an older pairing: nothing may be scheduled or sent
    loop.feed(FrameType.ANN, "con-9", consumer_ann((1, "cli"), None))
    assert loop.node.next_deadline() > T0 + SUPPRESSION_MAX
    fx = loop.poll_at(T0 + SUPPRESSION_MAX + 0.1)
    assert ints(fx) == []
    # a re-delivered copy of the rejected intent must not re-enter the cache
    loop.feed(FrameType.INT, "cli", doomed)  # fresh seq, same body
    loop.feed(FrameType.ANN, "con-9", consumer_ann((1, "cli"), None))
    fx = loop.poll_at(loop.now + SUPPRESSION_MAX + 0.1)
    assert ints(fx) == []
    fx = loop.poll_at(loop.now + SUPPRESSION_MAX + 0.1)  # any later schedule too
    assert ints(fx) == []


def test_newer_intent_after_rejection_is_cached_and_reoffered() -> None:
    loop = Loop(role=Role.PRODUCER, node_id="watch-1", media=HDMI_MEDIA)
    loop.feed(FrameType.INT, "cli", pair_body("con-9", "con-9", (3, "cli")))
    loop.feed(
        FrameType.ACK,
        "con-9",
        {"version": [3, "cli"], "applied": False, "reason": "self-pair"},
    )
    fixed = pair_body("con-9", "prod-1", (4, "cli"))  # higher version: valid again
    loop.feed(FrameType.INT, "cli", fixed)
    loop.feed(FrameType.ANN, "con-9", consumer_ann((1, "cli"), None))
    due = loop.node.next_deadline()
    assert due <= loop.now + SUPPRESSION_MAX
    fx = loop.poll_at(due)
    (reissue,) = ints(fx)
    assert reissue.body == fixed
    assert reissue.body["version"] == [4, "cli"]


def test_consumer_records_own_rejection_and_never_reoffers_it() -> None:
    loop = Loop()  # consumer node-1
    fx = loop.feed(FrameType.INT, "cli", pair_body("node-1", "node-1", (1, "cli")))
    ack = sends(fx, FrameType.ACK)[0]
    assert ack.body["applied"] is False and ack.body["reason"] == "self-pair"
    # across several announce periods the consumer must never re-offer the
    # intent it rejected for itself
    for _ in range(5):
        fx = loop.poll_at(loop.node.next_deadline())
        assert ints(fx) == []


# -- producer behavior -------------------------------------------------------


def test_producer_announce_shape() -> None:
    loop = Loop(role=Role.PRODUCER, node_id="prod-1", media=HDMI_MEDIA)
    ann = sends(loop.start_fx, FrameType.ANN)[0]
    assert ann.body == {
        "role": "producer",
        "kind": "hdmi",
        "name": "n",
        "state": "idle",
        "err": None,
        "pairing": None,
        "media": HDMI_MEDIA,
    }


def test_producer_without_media_announces_null_media() -> None:
    loop = Loop(role=Role.PRODUCER, node_id="prod-1", media=None)
    ann = sends(loop.start_fx, FrameType.ANN)[0]
    assert ann.body["media"] is None
    fx = loop.node.set_media(HDMI_MEDIA_2, loop.now)
    ann2 = sends(fx, FrameType.ANN)[0]
    assert ann2.body["media"] == HDMI_MEDIA_2


def test_producer_never_applies_intents() -> None:
    loop = Loop(role=Role.PRODUCER, node_id="prod-1", media=HDMI_MEDIA)
    # even an intent whose consumer field names this producer's own id
    fx = loop.feed(FrameType.INT, "cli", pair_body("prod-1", "prod-2", (1, "cli")))
    assert fx == []
    assert loop.node.pairing == Pairing(None, (0, ""))
    assert loop.node.node_state is NodeState.IDLE


def test_producer_driver_status_events_no_retry() -> None:
    loop = Loop(role=Role.PRODUCER, node_id="prod-1", media=HDMI_MEDIA)
    fx = loop.node.on_driver_status(DriverStatus(NodeState.ACTIVE), loop.now)
    assert "media-up" in evts(fx)
    fx = loop.node.on_driver_status(DriverStatus(NodeState.ERROR, "busy"), loop.now)
    assert evt_detail(fx, "driver-error") == {"detail": "busy"}
    assert not attaches(fx)
    # no 1 s retry scheduled: next deadline is the (jittered) announce
    assert loop.node.next_deadline() - loop.now >= ANN_MIN - 1e-9
    fx = loop.node.on_driver_status(DriverStatus(NodeState.IDLE), loop.now)
    assert "media-down" in evts(fx)
    assert loop.node.node_state is NodeState.IDLE


# -- shutdown ----------------------------------------------------------------


def test_shutdown_sends_stopping_then_bye() -> None:
    loop = Loop()
    fx = loop.node.shutdown(loop.now)
    assert [type(e) for e in fx] == [DetachMedia, SendFrame, SendFrame]
    stopping, bye = fx[1:]
    assert isinstance(stopping, SendFrame) and isinstance(bye, SendFrame)
    assert stopping.type is FrameType.EVT
    assert stopping.body == {"event": "stopping", "detail": {}}
    assert bye.type is FrameType.BYE
    assert bye.body == {}
    assert stopping.seq < bye.seq


# -- review regressions ------------------------------------------------------


def test_pending_pair_to_later_wrong_kind_producer_does_not_attach() -> None:
    # Pair (pending) to an unknown producer, which then announces as the
    # WRONG kind. The consumer must NOT attach its media (which would
    # churn the driver forever); it stays pending. (Review finding: the
    # kind-mismatch guard only ran at intent-apply time.)
    loop = Loop(kind=Kind.HDMI)
    loop.feed(FrameType.INT, "cli", pair_body("node-1", "prodx", (1, "cli")))
    assert loop.node.node_state is NodeState.PENDING
    fx = loop.feed(FrameType.ANN, "prodx", producer_ann(USB_MEDIA, kind="usb"))
    assert attaches(fx) == []
    assert loop.node.node_state is NodeState.PENDING


def test_paired_producer_role_flip_detaches_and_goes_pending() -> None:
    loop = Loop(kind=Kind.HDMI)
    paired_active(loop, "prod-1", (1, "cli"))
    assert loop.node.node_state is NodeState.ACTIVE
    # prod-1 re-announces as a consumer (operator reused the node id).
    fx = loop.feed(FrameType.ANN, "prod-1", consumer_ann((0, "")))
    assert any(isinstance(e, DetachMedia) for e in fx)
    assert loop.node.node_state is NodeState.PENDING


def test_far_future_seq_is_rejected_and_does_not_poison_replay() -> None:
    loop = Loop()
    # A spoofed frame claiming prod-1's origin with an absurd seq.
    poison = int(T0 + 10_000) * 1_000_000_000  # >1h ahead, in ns
    fx = loop.feed(FrameType.ANN, "prod-1", producer_ann(), seq=poison)
    assert fx == []
    assert loop.node._drops["stale"] == 1
    # The real prod-1 (normal seq) is still heard afterwards.
    loop.feed(FrameType.ANN, "prod-1", producer_ann(), seq=SEQ0 + 5)
    assert "prod-1" in loop.node.peers


def test_stale_driver_active_after_unpair_is_ignored() -> None:
    loop = Loop()
    paired_active(loop, "prod-1", (1, "cli"))
    loop.feed(FrameType.INT, "cli", unpair_body("node-1", (2, "cli")))
    assert loop.node.node_state is NodeState.PENDING  # cleanup not confirmed yet
    # A late ACTIVE from the attach that completed after the unpair.
    fx = loop.node.on_driver_status(DriverStatus(NodeState.ACTIVE), loop.now)
    assert fx == []
    assert loop.node.node_state is NodeState.PENDING
    assert "media-up" not in evts(fx)


def test_corrected_retry_reusing_rejected_version_is_cached() -> None:
    # Bystander view: an intent is rejected (self-pair) at version (1,cli),
    # then a corrected intent reuses the same version number with a valid
    # producer. The corrected one must still cache/anti-entropy.
    bystander = Loop(node_id="watch-1", role=Role.PRODUCER, kind=Kind.HDMI)
    # cache the doomed self-pair intent (producer == consumer)
    bystander.feed(FrameType.INT, "cli", pair_body("con-9", "con-9", (1, "cli")))
    assert bystander.node._intent_cache.get("con-9") is not None
    # observe con-9 reject it
    ack = {"version": [1, "cli"], "applied": False, "reason": "self-pair"}
    bystander.feed(FrameType.ACK, "con-9", ack)
    assert bystander.node._intent_cache.get("con-9") is None
    # corrected retry: same version number, valid producer -> caches
    bystander.feed(FrameType.INT, "cli", pair_body("con-9", "prod-1", (1, "cli")))
    cached = bystander.node._intent_cache.get("con-9")
    assert cached is not None
    assert cached.body["producer"] == "prod-1"


def test_prune_forgets_long_departed_peers() -> None:
    from yoke.core.types import PEER_FORGET

    loop = Loop()
    loop.feed(FrameType.ANN, "ghost", producer_ann())
    loop.feed(FrameType.BYE, "ghost", {})
    assert "ghost" in loop.node.peers
    loop.poll_at(loop.now + PEER_FORGET + 1.0)
    assert "ghost" not in loop.node.peers
    assert "ghost" not in loop.node._replay


def test_failed_pair_save_never_commits_or_reacks_duplicate() -> None:
    loop = Loop()
    frame, raw = build_frame(
        FrameType.INT, "cli", 1, pair_body("node-1", "prod-1", (1, "cli"))
    )
    effects = loop.node.on_frame(frame, raw, loop.now)
    assert len(effects) == 1 and isinstance(effects[0], Persist)
    save = effects[0]
    assert loop.node.pairing == Pairing(None, (0, ""))
    assert loop.node.on_persist_result(save.request_id + 1, True, loop.now) == []
    assert loop.node.on_frame(frame, raw, loop.now) == []
    failed = loop.node.on_persist_result(save.request_id, False, loop.now)
    assert all(isinstance(effect, Log) for effect in failed)
    assert loop.node.pairing == Pairing(None, (0, ""))
    assert loop.node.on_frame(frame, raw, loop.now) == []
    # CLI retries use a fresh envelope sequence but the same intent version.
    retry, retry_raw = build_frame(FrameType.INT, "cli", 2, frame.body)
    retried = loop.node.on_frame(retry, retry_raw, loop.now)
    assert len(retried) == 1 and isinstance(retried[0], Persist)
    completed = loop.node.on_persist_result(retried[0].request_id, True, loop.now)
    assert sends(completed, FrameType.ACK)[0].body["applied"] is True
    assert loop.node.pairing == Pairing("prod-1", (1, "cli"))
    assert loop.node.on_persist_result(retried[0].request_id, True, loop.now) == []


def test_failed_unpair_keeps_committed_pairing_and_emits_no_detach() -> None:
    loop = Loop()
    paired_active(loop)
    before = loop.node.pairing
    frame, raw = build_frame(FrameType.INT, "cli", 2, unpair_body("node-1", (2, "cli")))
    effects = loop.node.on_frame(frame, raw, loop.now)
    assert len(effects) == 1 and isinstance(effects[0], Persist)
    assert loop.node.pairing == before
    failed = loop.node.on_persist_result(effects[0].request_id, False, loop.now)
    assert not any(isinstance(effect, DetachMedia | SendFrame) for effect in failed)
    assert loop.node.pairing == before


def test_start_save_failure_emits_no_frames_or_driver_commands() -> None:
    node = Node(
        NodeConfig("node-1", Role.CONSUMER, Kind.USB), state=PersistedState(), now=T0
    )
    effects = node.start(T0)
    assert len(effects) == 1 and isinstance(effects[0], Persist)
    failed = node.on_persist_result(effects[0].request_id, False, T0)
    assert all(isinstance(effect, Log) for effect in failed)
    assert node.poll(T0 + 10) == []
    # Local shutdown must still quiesce even without an outbound reservation.
    assert node.shutdown(T0 + 10) == [DetachMedia()]


def test_same_second_restart_skips_durable_reservation_with_backwards_wall_clock() -> (
    None
):
    loop = Loop()
    saved = next(
        effect.state for effect in loop.start_fx if isinstance(effect, Persist)
    )
    prior_sequences = [
        effect.seq for effect in loop.start_fx if isinstance(effect, SendFrame)
    ]
    restarted = Node(
        NodeConfig("node-1", Role.CONSUMER, Kind.HDMI),
        state=saved,
        now=0.0,
        wall_time=T0 - 60,
    )
    save = restarted.start(0.0)[0]
    assert isinstance(save, Persist)
    frames = restarted.on_persist_result(save.request_id, True, 0.0)
    sequences = [effect.seq for effect in frames if isinstance(effect, SendFrame)]
    assert min(sequences) > saved.seq >= max(prior_sequences)
    assert max(sequences) <= save.state.seq


def test_sequence_refill_failure_withholds_batch_until_durable_retry() -> None:
    loop = Loop()
    loop.node._seq = loop.node._reserved_until
    t = loop.node.next_deadline()
    effects = loop.node.poll(t)
    assert len(effects) == 1 and isinstance(effects[0], Persist)
    failed = loop.node.on_persist_result(effects[0].request_id, False, t)
    assert not any(isinstance(effect, SendFrame) for effect in failed)
    assert loop.node.poll(t + 0.5) == []
    retry = loop.node.poll(t + 1.0)
    assert len(retry) == 1 and isinstance(retry[0], Persist)
    released = loop.node.on_persist_result(retry[0].request_id, True, t + 1.0)
    assert sends(released, FrameType.ANN)
    assert all(
        effect.seq <= retry[0].state.seq
        for effect in released
        if isinstance(effect, SendFrame)
    )


def test_shutdown_detaches_even_when_sequence_refill_save_is_pending() -> None:
    loop = Loop()
    paired(loop)
    loop.node._seq = loop.node._reserved_until
    assert isinstance(loop.node.poll(loop.node.next_deadline())[0], Persist)
    assert loop.node.shutdown(loop.now) == [DetachMedia()]


def test_repair_to_unknown_producer_releases_previous_attachment() -> None:
    loop = Loop()
    paired_active(loop)
    effects = loop.feed(
        FrameType.INT, "cli", pair_body("node-1", "unknown", (2, "cli"))
    )
    assert any(isinstance(effect, DetachMedia) for effect in effects)
    assert not attaches(effects)
    assert loop.node.pairing == Pairing("unknown", (2, "cli"))
    assert loop.node.on_driver_status(DriverStatus(NodeState.ACTIVE), loop.now) == []
    loop.node.on_driver_status(DriverStatus(NodeState.IDLE), loop.now)
    assert loop.node.node_state is NodeState.PENDING


def test_backend_readiness_reports_bounded_evidence_without_payload_claim() -> None:
    loop = Loop()
    paired(loop)
    effects = loop.node.on_driver_status(
        DriverStatus(NodeState.ACTIVE, "joined group"), loop.now
    )
    assert evt_detail(effects, "media-up") == {"detail": "joined group"}
    assert sends(effects, FrameType.ANN)[0].body["driver_status"] == {
        "detail": "joined group"
    }


@pytest.mark.parametrize(
    "version",
    [[1, ""], [0, "cli"], [MAX_SEQUENCE + 1, "cli"], [True, "cli"], [1, "bad\n"]],
)
def test_invalid_version_cannot_update_pairing_or_enter_cache(version: object) -> None:
    loop = Loop()
    effects = loop.feed(
        FrameType.INT,
        "cli",
        {"op": "pair", "consumer": "node-1", "producer": "prod-1", "version": version},
    )
    assert not sends(effects, FrameType.ACK)
    assert loop.node.pairing == Pairing(None, (0, ""))
    assert not loop.node._intent_cache


def test_monotonic_timer_inputs_are_independent_of_sequence_wall_floor() -> None:
    node = Node(
        NodeConfig("node-1", Role.PRODUCER, Kind.USB),
        state=PersistedState(),
        now=0,
        wall_time=T0,
    )
    save = node.start(0)[0]
    assert isinstance(save, Persist)
    node.on_persist_result(save.request_id, True, 0)
    assert 1.0 <= node.next_deadline() <= 3.0
    frame, raw = build_frame(FrameType.ANN, "peer", SEQ0 + 100, producer_ann())
    node.on_frame(frame, raw, 0.25)
    assert "peer" in node.peers


def test_pairing_survives_longer_than_intent_cache_and_peer_timeout() -> None:
    loop = Loop()
    paired_active(loop)
    committed = loop.node.pairing
    loop.poll_at(T0 + INTENT_CACHE_TTL + PEER_TIMEOUT + 1)
    assert loop.node.pairing == committed
    assert loop.node.node_state is NodeState.PENDING


def test_peer_maps_are_bounded_between_timer_ticks() -> None:
    from yoke.core.types import MAX_TRACKED

    loop = Loop()
    for index in range(MAX_TRACKED + 20):
        loop.feed(FrameType.ANN, f"peer-{index}", producer_ann())
    assert len(loop.node._peers) == MAX_TRACKED
    assert len(loop.node._replay) == MAX_TRACKED


def test_optional_driver_evidence_cannot_overflow_existing_announce() -> None:
    loop = Loop(role=Role.PRODUCER, media={"pad": "x" * 850})
    effects = loop.node.on_driver_status(
        DriverStatus(NodeState.ACTIVE, "証拠" * 100), loop.now
    )
    for effect in effects:
        if isinstance(effect, SendFrame):
            encode(effect.type, "node-1", effect.seq, T0, effect.body, PSK)


def test_unpair_failure_stays_visible_and_retries_detach_until_idle() -> None:
    loop = Loop()
    paired_active(loop)
    loop.feed(FrameType.INT, "cli", unpair_body("node-1", (2, "cli")))
    effects = loop.node.on_driver_status(
        DriverStatus(NodeState.ERROR, "release failed"), T0
    )
    assert loop.node.node_state is NodeState.ERROR
    assert "driver-error" in evts(effects)
    loop.node.on_driver_status(
        DriverStatus(NodeState.ERROR, "release failed"), T0 + 0.9
    )
    retry = loop.poll_at(T0 + 1)
    assert any(isinstance(effect, DetachMedia) for effect in retry)
    assert not attaches(retry)
    assert loop.node.pairing == Pairing(None, (2, "cli"))
    loop.node.on_driver_status(DriverStatus(NodeState.IDLE), T0 + 1)
    assert loop.node.node_state is NodeState.IDLE
    assert loop.node._retry_at is None


def test_unknown_replacement_failure_retries_old_teardown_without_new_peer() -> None:
    loop = Loop()
    paired_active(loop)
    loop.feed(FrameType.INT, "cli", pair_body("node-1", "unknown", (2, "cli")))
    loop.node.on_driver_status(DriverStatus(NodeState.ERROR, "old route stuck"), T0)
    retry = loop.poll_at(T0 + 1)
    assert any(isinstance(effect, DetachMedia) for effect in retry)
    assert not attaches(retry)
    loop.node.on_driver_status(DriverStatus(NodeState.IDLE), T0 + 1)
    assert loop.node.pairing == Pairing("unknown", (2, "cli"))
    assert loop.node.node_state is NodeState.PENDING
    assert loop.node._retry_at is None


def test_restart_after_durable_unpair_reconciles_possible_surviving_imports() -> None:
    loop = Loop(state=PersistedState(seq=17, pairing=Pairing(None, (2, "cli"))))
    assert any(isinstance(effect, DetachMedia) for effect in loop.start_fx)
    assert loop.node.node_state is NodeState.PENDING
    loop.node.on_driver_status(
        DriverStatus(NodeState.PENDING, "owned ports remain"), T0
    )
    assert any(isinstance(effect, DetachMedia) for effect in loop.poll_at(T0 + 1))


def test_restart_unknown_replacement_cleans_previous_owned_imports() -> None:
    loop = Loop(state=PersistedState(seq=17, pairing=Pairing("unknown", (2, "cli"))))
    assert any(isinstance(effect, DetachMedia) for effect in loop.start_fx)
    loop.node.on_driver_status(DriverStatus(NodeState.ERROR, "old import remains"), T0)
    retry = loop.poll_at(T0 + 1)
    assert any(isinstance(effect, DetachMedia) for effect in retry)
    assert not attaches(retry)


def test_repeated_error_polls_cannot_starve_attach_retry() -> None:
    loop = Loop()
    paired(loop)
    loop.node.on_driver_status(DriverStatus(NodeState.ERROR, "same failure"), T0)
    deadline = loop.node._retry_at
    attempts = 0
    for index in range(1, 31):
        loop.now = T0 + index * 0.9
        loop.feed(FrameType.ANN, "prod-1", producer_ann())
        loop.node.on_driver_status(
            DriverStatus(NodeState.ERROR, "same failure"), loop.now
        )
        if index == 1:
            assert loop.node._retry_at == deadline
        attempts += len(attaches(loop.poll_at(loop.now)))
    assert attempts >= 4


def test_pending_backend_drift_reconciles_with_live_configured_producer() -> None:
    loop = Loop()
    paired_active(loop)
    loop.node.on_driver_status(DriverStatus(NodeState.PENDING, "channel changed"), T0)
    deadline = loop.node._retry_at
    loop.node.on_driver_status(
        DriverStatus(NodeState.PENDING, "channel changed"), T0 + 0.9
    )
    assert loop.node._retry_at == deadline
    assert attaches(loop.poll_at(T0 + 1)) == [AttachMedia("prod-1", HDMI_MEDIA)]


@pytest.mark.parametrize("reason", [[], {}, None, True, 1])
def test_malformed_ack_reason_cannot_crash_or_evict_intent(reason: object) -> None:
    loop = Loop(role=Role.PRODUCER)
    loop.feed(FrameType.INT, "cli", pair_body("con-9", "prod-1", (1, "cli")))
    loop.feed(
        FrameType.ACK,
        "con-9",
        {"version": [1, "cli"], "applied": False, "reason": reason},
    )
    assert "con-9" in loop.node._intent_cache
    assert loop.node._drops["malformed"] == 1


def test_large_producer_error_announcements_fit_without_truncating_media() -> None:
    media = {"pad": "x" * 850}
    loop = Loop(role=Role.PRODUCER, media=media)
    batches = [loop.start_fx]
    for status in (
        DriverStatus(NodeState.ERROR, "x" * 128),
        DriverStatus(NodeState.PENDING, "waiting"),
        DriverStatus(NodeState.ACTIVE, "ready"),
    ):
        batches.append(loop.node.on_driver_status(status, loop.now))
        batches.append(loop.poll_at(loop.node.next_deadline()))
    for batch in batches:
        for effect in batch:
            if isinstance(effect, SendFrame):
                encode(effect.type, "node-1", effect.seq, T0, effect.body, PSK)
                if effect.type is FrameType.ANN:
                    assert effect.body["media"] == media


def test_unencodable_local_media_is_rejected_before_start_or_update() -> None:
    with pytest.raises(ValueError):
        Loop(role=Role.PRODUCER, media={"pad": "x" * 1200})
    loop = Loop(role=Role.PRODUCER, media=HDMI_MEDIA)
    with pytest.raises(ValueError):
        loop.node.set_media({"pad": "x" * 1200}, loop.now)
    assert loop.node._media == HDMI_MEDIA


def test_changed_producer_description_invalidates_active_evidence_until_success() -> (
    None
):
    loop = Loop()
    paired(loop)
    loop.node.on_driver_status(
        DriverStatus(NodeState.ACTIVE, "old group ready"), loop.now
    )
    effects = loop.feed(FrameType.ANN, "prod-1", producer_ann(HDMI_MEDIA_2))
    assert attaches(effects) == [AttachMedia("prod-1", HDMI_MEDIA_2)]
    assert loop.node.node_state is NodeState.PENDING
    assert loop.node._driver_detail == ""
    assert loop.node._err is None
    announcements = sends(effects, FrameType.ANN)
    announcements.extend(sends(loop.poll_at(loop.node.next_deadline()), FrameType.ANN))
    assert announcements
    assert all(frame.body["state"] == "pending" for frame in announcements)
    assert all("driver_status" not in frame.body for frame in announcements)
    ready = loop.node.on_driver_status(
        DriverStatus(NodeState.ACTIVE, "new group ready"), loop.now
    )
    assert loop.node.node_state is NodeState.ACTIVE
    assert evt_detail(ready, "media-up") == {"detail": "new group ready"}


def test_invalid_producer_payload_is_not_retained_for_relay() -> None:
    loop = Loop(role=Role.PRODUCER)
    loop.feed(
        FrameType.INT,
        "cli",
        {"op": "pair", "consumer": "con-1", "producer": [], "version": [1, "cli"]},
    )
    assert not loop.node._intent_cache
