"""Lockstep differential: a native node against the reference Node.

A seeded script drives one Python ``Node`` and one native node with the same
inputs: frames from synthetic peers (valid, malformed bodies, replays, stale
and far-future sequences, forgeries), timer polls, wall-clock steps, driver
statuses, media changes, storage completions that sometimes fail, and
shutdown. After every input the effects must be equal (logs excepted) and
so must the observable state. Table capacities are made small on both sides
(the reference's MAX_TRACKED is patched to the native limits) so FIFO
eviction runs constantly. HOST ONLY.
"""

from __future__ import annotations

import hashlib
import hmac
import random
from collections import Counter
from typing import Any

import pytest

import yoke.core.node as node_module
from tests.native_ywp.cnode import CNode, Library
from tests.native_ywp.simnet_native import build_library
from yoke.core.frames import FrameDrop, canonical_bytes, decode
from yoke.core.node import Node
from yoke.core.types import (
    NODE_ID_RE,
    Admission,
    AttachMedia,
    Body,
    DetachMedia,
    DriverStatus,
    Effect,
    FrameType,
    Kind,
    Log,
    NodeConfig,
    NodeState,
    Persist,
    PersistedState,
    Role,
    SendFrame,
)

PSK = b"lockstep-site-key-0123456789abcdef"
T0 = 1_793_000_000.0
CAPACITY = 4
CONSUMERS = [f"c{i}" for i in range(12)]
PEERS = [f"p{i}" for i in range(6)] + CONSUMERS + ["prod-a", "prod-b", "prod-h", "self"]
REJECTIONS = ("self-pair", "kind-mismatch", "unknown-producer")


def producer_key(value: object) -> object:
    """A rejected intent's producer as both implementations classify it."""
    if value is None or (isinstance(value, str) and NODE_ID_RE.fullmatch(value)):
        return value
    return "<other>"


@pytest.fixture(autouse=True)
def small_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(node_module, "MAX_TRACKED", CAPACITY)


@pytest.fixture(scope="module")
def library(tmp_path_factory: pytest.TempPathFactory) -> Library:
    return build_library(tmp_path_factory.mktemp("ywp-lockstep"))


def normalize(effects: list[Effect]) -> list[tuple[Any, ...]]:
    out: list[tuple[Any, ...]] = []
    for effect in effects:
        if isinstance(effect, Log):
            continue
        if isinstance(effect, SendFrame):
            out.append(
                ("send", effect.type.value, effect.seq, canonical_bytes(effect.body))
            )
        elif isinstance(effect, Persist):
            out.append(
                ("persist", effect.state.seq, effect.state.pairing, effect.request_id)
            )
        elif isinstance(effect, AttachMedia):
            out.append(("attach", effect.producer, canonical_bytes(effect.media)))
        elif isinstance(effect, DetachMedia):
            out.append(("detach",))
        elif isinstance(effect, Admission):
            out.append(("admission", effect.peers))
        else:
            raise AssertionError(effect)
    return out


def sign(
    frame_type: FrameType, origin: str, seq: int, ts: float, body: Body, psk: bytes
) -> bytes:
    """A canonical signed frame, also with bodies the reference encoder
    refuses to emit (non-integer extension members) but must accept."""
    unsigned = {
        "v": 1,
        "t": frame_type.value,
        "id": origin,
        "seq": seq,
        "ts": ts,
        "body": body,
    }
    sig = hmac.new(psk, canonical_bytes(unsigned), hashlib.sha256).hexdigest()
    return canonical_bytes({**unsigned, "sig": sig})


class World:
    """Synthetic peers whose frames both nodes receive."""

    def __init__(self, rng: random.Random, me: str) -> None:
        self.rng = rng
        self.me = me
        self.seq: dict[str, int] = {}
        self.sent: list[bytes] = []
        self.versions = 0
        self.last_intent: dict[str, Any] = {}  # consumer -> version, for acks

    def _seq(self, origin: str, now: float) -> int:
        seq = max(self.seq.get(origin, 0) + 1, int(now * 1e9))
        self.seq[origin] = seq
        return seq

    def _version(self) -> list[Any]:
        rng = self.rng
        if rng.random() < 0.7:
            self.versions += 1
        return [
            self.versions if rng.random() < 0.9 else rng.randrange(0, 4),
            rng.choice(("cli-x", "cli-y", "a", "self")),
        ]

    def body(self, frame_type: FrameType, origin: str) -> Body:
        rng = self.rng
        if frame_type is FrameType.ANN:
            role = rng.choice(("producer", "consumer"))
            kind = rng.choice(("usb", "usb", "hdmi"))
            if origin == "prod-h":
                role, kind = "producer", "hdmi"
            elif origin.startswith("prod"):
                role, kind = "producer", "usb" if rng.random() < 0.9 else "hdmi"
            elif origin in CONSUMERS:
                role = "consumer"
            body: Body = {
                "role": role,
                "kind": kind,
                "name": rng.choice(("", "box", "é" * 64, "x" * 65)),
                "state": rng.choice(("idle", "pending", "active", "active", "error")),
                "err": rng.choice((None, "boom")),
                "media": None,
                "pairing": None,
            }
            if role == "producer":
                body["media"] = rng.choice(
                    (
                        None,
                        {"host": "10.0.0.1", "port": 3240, "busids": ["1-1"]},
                        {"host": "10.0.0.2", "port": 3240, "busids": ["1-1", "1-2"]},
                        [1],
                    )
                )
            else:
                producer = rng.choice((None, "prod-a", "prod-b", self.me, "Bad"))
                version = [rng.randrange(1, self.versions + 2), "cli-x"]
                if not producer and rng.random() < 0.3:
                    version = [0, ""]
                body["pairing"] = {"producer": producer, "version": version}
            if rng.random() < 0.05:
                del body[rng.choice(list(body))]
            return body
        if frame_type is FrameType.INT:
            consumer = rng.choice([self.me] * 4 + CONSUMERS + ["Bad"])
            body = {
                "op": rng.choice(("pair", "pair", "pair", "unpair", "nope")),
                "consumer": consumer,
                "version": self._version(),
            }
            self.last_intent[consumer] = body["version"]
            if rng.random() < 0.9:
                # Biased toward rejections: self-pair, kind-mismatch (an HDMI
                # producer or a consumer), unknown-producer (non-ids).
                body["producer"] = rng.choice(
                    (
                        consumer,
                        consumer,
                        "prod-h",
                        "c3",
                        "prod-a",
                        "prod-b",
                        "p1",
                        "Bad",
                        7,
                        1.0,
                        None,
                        ["x"],
                    )
                )
            if rng.random() < 0.1:
                body["extension"] = rng.choice((0.5, "pad", {"a": 1}))
            return body
        if frame_type is FrameType.ACK:
            return {
                "version": self._version(),
                "applied": rng.choice((False, False, True)),
                "reason": rng.choice(
                    (
                        "self-pair",
                        "kind-mismatch",
                        "unknown-producer",
                        "superseded",
                        None,
                    )
                ),
            }
        if frame_type is FrameType.EVT:
            return {"event": "started", "detail": {}}
        return {}

    def datagram(self, now: float) -> bytes:
        rng = self.rng
        roll = rng.random()
        if self.sent and roll < 0.08:
            return rng.choice(self.sent)  # replay: dup or stale
        origin = rng.choice(PEERS + ["prod-a", "prod-b", "prod-h"] * 3)
        frame_type = rng.choice(
            (FrameType.ANN,) * 5
            + (FrameType.INT,) * 3
            + (FrameType.ACK, FrameType.ACK, FrameType.BYE, FrameType.EVT)
        )
        body = self.body(frame_type, origin)
        rejected = [c for c in self.last_intent if c in CONSUMERS]
        if frame_type is FrameType.ACK and rejected and rng.random() < 0.7:
            # The consumer's own rejection ack for its latest intent: bystanders
            # evict and pin exactly that version (ADR 0009).
            origin = rng.choice(rejected)
            body = {
                "version": self.last_intent[origin],
                "applied": False,
                "reason": rng.choice(REJECTIONS),
            }
        seq = self._seq(origin, now)
        if roll < 0.12:
            seq += 7200 * 10**9  # far future
        psk = OTHER if roll > 0.98 else PSK
        raw = sign(frame_type, origin, seq, now, body, psk)
        if roll > 0.97:
            raw = raw[: rng.randrange(len(raw))]
        self.sent.append(raw)
        return raw


OTHER = b"another-site-key-0123456789abcdef"


class Pair:
    """The reference and native nodes, driven in lockstep."""

    def __init__(self, lib: Library, seed: int, role: Role, admission: bool) -> None:
        rng = random.Random(seed)
        self.rng = rng
        self.seed = seed
        config = NodeConfig(node_id="self", role=role, kind=Kind.USB, name="box")
        media = {"host": "10.0.0.9", "port": 3240, "busids": ["1-1"]}
        kwargs: dict[str, Any] = {
            "state": PersistedState(),
            "now": 100.0,
            "media": media if role is Role.PRODUCER else None,
            "seed": rng.randrange(2**32),
            "wall_time": T0,
            "admission": admission,
        }
        limits = lib.default_limits()
        for name in ("peers", "origins", "intents", "rejections"):
            setattr(limits, name, CAPACITY)
        self.py = Node(config, **kwargs)
        self.c = CNode(lib, config, psk=PSK, limits=limits, **kwargs)
        self.now = 100.0
        self.skew = T0 - 100.0
        self.steps = 0
        self.seen: Counter[str] = Counter()
        self.peak: Counter[str] = Counter()  # highest table occupancy seen

    def compare_state(self) -> None:
        py, c = self.py, self.c
        assert c.pairing == py.pairing
        assert c.node_state is py.node_state
        assert c._drops == py._drops and c._dups == py._dups
        assert c._drops_reported == py._drops_reported
        assert c._retry_at == py._retry_at and c._backoff == py._backoff
        for node_id, peer in py.peers.items():
            other = c.peers[node_id]
            assert (
                other.role,
                other.kind,
                other.state,
                other.pairing,
                other.last_seen,
                other.departed,
            ) == (
                peer.role,
                peer.kind,
                peer.state,
                peer.pairing,
                peer.last_seen,
                peer.departed,
            ), node_id
        assert list(c.peers) == list(py.peers)
        cached = {
            k: (v.version, v.body, v.cached_at) for k, v in py._intent_cache.items()
        }
        assert {
            k: (v.version, v.body, v.cached_at) for k, v in c._intent_cache.items()
        } == cached
        assert {k: (v[0], producer_key(v[1])) for k, v in c._rejected.items()} == {
            k: (v[0], producer_key(v[1])) for k, v in py._rejected.items()
        }
        live_resends = {k: v for k, v in py._resend_at.items() if k in py._intent_cache}
        assert c._resend_at == live_resends
        if live_resends == py._resend_at:  # the reference keeps evicted no-op timers
            assert c.next_deadline() == py.next_deadline()

    def call(self, name: str, *args: Any, **kwargs: Any) -> None:
        """The same input to both nodes."""
        self.steps += 1
        expected = getattr(self.py, name)(*args, **kwargs)
        self.settle(expected, getattr(self.c, name)(*args, **kwargs), name)

    def datagram(self, raw: bytes, wall: float, source: str | None) -> None:
        """The reference shell decodes; the native node decodes for itself."""
        self.steps += 1
        try:
            frame = decode(raw, PSK)
        except FrameDrop as drop:
            expected = self.py.on_drop(drop.reason, self.now)
        else:
            expected = self.py.on_frame(frame, raw, self.now, wall=wall, source=source)
        got = self.c.on_datagram(raw, self.now, wall=wall, source=source)
        self.settle(expected, got, "frame")

    def settle(self, expected: list[Effect], got: list[Effect], label: str) -> None:
        if label == "poll":  # the native poll may yield and ask to be polled again
            while self.c.next_deadline() <= self.now and self.c.status().started:
                more = self.c.poll(self.now, wall=self.now + self.skew)
                if not more:
                    break
                got = got + more
        normalized = normalize(expected)
        assert normalize(got) == normalized, (self.seed, label, self.steps)
        self.seen.update(
            item[0] if item[0] != "send" else item[1] for item in normalized
        )
        status = self.c.status()
        if status.held:
            self.seen["held"] += 1
        for table in ("peers", "origins", "intents", "rejections"):
            self.peak[table] = max(self.peak[table], getattr(status, table))
        persist = [e for e in expected if isinstance(e, Persist)]
        if persist:
            # A failed startup save aborts the reference runner; others retry.
            ok = label == "start" or self.rng.random() < 0.9
            self.seen["persist-ok" if ok else "persist-failed"] += 1
            self.settle(
                self.py.on_persist_result(persist[-1].request_id, ok, self.now),
                self.c.on_persist_result(persist[-1].request_id, ok, self.now),
                "persist",
            )
        self.compare_state()


def run_script(
    lib: Library,
    seed: int,
    role: Role,
    steps: int,
    admission: bool,
    churn: bool = False,
) -> Pair:
    pair = Pair(lib, seed, role, admission)
    rng = pair.rng
    world = World(random.Random(seed + 1), "self")
    pair.call("start", pair.now)
    for _ in range(steps):
        roll = rng.random()
        gap = rng.random()
        if gap < 0.005:
            pair.now += 400.0 * rng.random()  # long silence: peers lost, pruned
        elif gap < 0.03:
            pair.now += 10.0 * rng.random()
        else:
            pair.now += rng.choice((0.0, 0.001, 0.05, 0.3, 1.0)) * rng.random()
        if rng.random() < 0.01:
            pair.skew += rng.choice((-7200.0, 7200.0, 30.0))
        wall = pair.now + pair.skew
        if churn and roll < 0.7:  # a flapping driver: two frames per input
            state = (NodeState.ACTIVE, NodeState.ERROR)[pair.steps % 2]
            pair.call("on_driver_status", DriverStatus(state, "flap"), pair.now)
        elif roll < 0.55:
            source = rng.choice((None, "10.0.0.5", "10.0.0.6", "bad"))
            pair.datagram(world.datagram(wall), wall, source)
        elif roll < 0.8:
            pair.call("poll", pair.now, wall=wall)
        elif roll < 0.95:
            state = rng.choice(list(NodeState))
            detail = rng.choice(("", "ok", "x" * 300, 'é\x01"\\' * 40, "attached"))
            pair.call("on_driver_status", DriverStatus(state, detail), pair.now)
        elif role is Role.PRODUCER and roll < 0.97:
            media = rng.choice(
                (
                    {"host": "10.0.0.9", "port": 1, "busids": ["2-1"]},
                    {"pad": "x" * 2000},
                )
            )
            try:
                expected = pair.py.set_media(media, pair.now)
            except ValueError:
                with pytest.raises(ValueError):
                    pair.c.set_media(media, pair.now)
            else:
                pair.settle(expected, pair.c.set_media(media, pair.now), "set_media")
    pair.call("shutdown", pair.now)
    return pair


@pytest.mark.parametrize("role", [Role.CONSUMER, Role.PRODUCER])
def test_lockstep(
    library: Library, monkeypatch: pytest.MonkeyPatch, role: Role
) -> None:
    seen: Counter[str] = Counter()
    peak: Counter[str] = Counter()
    for seed in range(10):
        admission = role is Role.PRODUCER and seed % 2 == 0
        pair = run_script(
            library, 2 * seed + (role is Role.PRODUCER), role, 2500, admission
        )
        seen += pair.seen
        peak |= pair.peak
    # Every table fills, so FIFO eviction is compared, not just insertion.
    assert all(peak[table] == CAPACITY for table in peak), peak
    # Agreement proves little unless the scripts reach the interesting paths.
    wanted = {"ann": 2000, "evt": 2000, "int": 20, "bye": 10, "persist-ok": 10}
    if role is Role.CONSUMER:
        wanted |= {"ack": 100, "attach": 20, "detach": 100, "persist-failed": 5}
    else:
        wanted |= {"admission": 10}
    assert all(seen[key] >= least for key, least in wanted.items()), seen


def test_lockstep_crosses_sequence_reservations(
    library: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Long enough that frames repeatedly pass the 4096-number reservation, so
    held batches (and failed reservation saves) are exercised."""
    pair = run_script(library, 77, Role.PRODUCER, 9000, admission=True, churn=True)
    assert pair.c.status().last_seq - int(T0) * 10**9 > 3 * 4096
    assert pair.seen["held"] > 0, pair.seen


def _started(
    lib: Library, role: Role, capacity: int, **kwargs: Any
) -> tuple[Node, CNode]:
    config = NodeConfig(node_id="self", role=role, kind=Kind.USB, name="box")
    limits = lib.default_limits()
    for name in ("peers", "origins", "intents", "rejections"):
        setattr(limits, name, capacity)
    options: dict[str, Any] = {
        "state": PersistedState(),
        "now": 100.0,
        "seed": 1,
        "wall_time": T0,
        **kwargs,
    }
    nodes = (
        Node(config, **options),
        CNode(lib, config, psk=PSK, limits=limits, **options),
    )
    for node in nodes:
        (persist,) = [e for e in node.start(100.0) if isinstance(e, Persist)]
        node.on_persist_result(persist.request_id, True, 100.0)
    return nodes


def test_tables_are_capped_when_the_input_ends(
    library: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review repro: an intent inserted and then evicted within one input
    (a self-pair rejection) must not push out an older row first."""
    monkeypatch.setattr(node_module, "MAX_TRACKED", 2)
    py, c = _started(library, Role.CONSUMER, 2)
    seq = int(T0 * 1e9)
    for consumer, producer, n in (
        ("con-a", "prod-a", 1),
        ("con-b", "prod-a", 1),
        ("self", "self", 5),
    ):
        seq += 1
        raw = sign(
            FrameType.INT,
            "cli-x",
            seq,
            T0,
            {
                "op": "pair",
                "consumer": consumer,
                "producer": producer,
                "version": [n, "cli-x"],
            },
            PSK,
        )
        expected = normalize(py.on_frame(decode(raw, PSK), raw, 100.0, wall=T0))
        assert normalize(c.on_datagram(raw, 100.0, wall=T0)) == expected
    assert list(c._intent_cache) == list(py._intent_cache) == ["con-a", "con-b"]
    assert c._rejected["self"][0] == py._rejected["self"][0] == (5, "cli-x")


def test_admitted_consumer_rows_are_never_evicted(library: Library) -> None:
    """ADR 0029: a flood of other authentic origins cannot evict an admitted
    consumer's replay row, so its captured frames stay stale from any address
    and its admission stands."""
    _, producer = _started(
        library,
        Role.PRODUCER,
        4,
        admission=True,
        media={"host": "10.0.0.9", "port": 3240, "busids": ["1-1"]},
    )
    seq = int(T0 * 1e9)
    claim = {
        "role": "consumer",
        "kind": "usb",
        "name": "",
        "state": "pending",
        "err": None,
        "media": None,
        "pairing": {"producer": "self", "version": [1, "cli-x"]},
    }
    captured = []
    for _ in range(2):
        seq += 1
        captured.append(sign(FrameType.ANN, "con-a", seq, T0, claim, PSK))
        effects = producer.on_datagram(captured[-1], 100.0, wall=T0, source="10.0.1.10")
    assert Admission((("con-a", "10.0.1.10"),)) in effects
    for index in range(12):  # other origins: more than the origin table holds
        seq += 1
        raw = sign(
            FrameType.INT,
            f"cli-{index}",
            seq,
            T0,
            {"op": "unpair", "consumer": f"x{index}", "version": [1, "cli-x"]},
            PSK,
        )
        assert not [
            e
            for e in producer.on_datagram(raw, 101.0, wall=T0, source="10.6.6.6")
            if isinstance(e, Admission)
        ]
    stale = producer._drops["stale"]
    for raw in captured:  # replays from an attacker's address bind nothing
        producer.on_datagram(raw, 102.0, wall=T0, source="10.6.6.6")
        producer.on_datagram(raw, 102.0, wall=T0, source="10.6.6.6")
    assert producer._drops["stale"] + producer._dups >= stale + 3
    assert producer.status().admitted == 1
