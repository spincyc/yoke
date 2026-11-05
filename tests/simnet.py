"""Deterministic multi-node simulation of the yoke control plane.

``SimNet`` hosts N sans-I/O ``Node`` instances — each with its own
``SimDriver`` and persisted-state slot — on a virtual multicast bus with
a virtual clock. Frames travel through the real wire codec
(``frames.encode``/``frames.decode`` with a shared PSK); delivery
applies scriptable per-link fault policies (drop, duplicate, delay
jitter for reordering, partitions) driven by a single seeded
``random.Random``, so every run is reproducible from one top-level
seed. Nothing here reads wall time or opens a socket.

Partition semantics: while a partition is active, only ids listed in
some group can communicate, and only within their own group. List every
node (and pseudo-origin such as ``cli-sim``) that should stay reachable.

Clocks (ADR 0023): the virtual clock is true time and every node's
monotonic clock. Each node also has a wall-clock skew, which can be set at
boot and stepped at any moment (time sync, bad RTC); the node receives
``now + skew`` as its wall reading with every input. Each sender has a
transport address so producer data-plane admission (ADR 0024) can be
simulated, including injected spoofed or replayed datagrams.

Implementations (ADR 0027): nodes come from ``NODE_FACTORY``, the Python
reference by default. tests/native_ywp substitutes the native C node, which
decodes its own datagrams (``on_datagram``) and encodes its own frames
(``encode_frame``); everything else is shared.
"""

from __future__ import annotations

import heapq
import math
import random
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from yoke.core.frames import FrameDrop, decode, encode
from yoke.core.node import Node
from yoke.core.types import (
    Admission,
    AttachMedia,
    Body,
    DetachMedia,
    DriverStatus,
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
    SendRaw,
    Version,
    version_key,
)
from yoke.drivers.base import DriverError
from yoke.drivers.sim import SimDriver

PSK = b"simnet-psk"
T0 = 1_754_000_000.0


def python_node(config: NodeConfig, *, psk: bytes, **kwargs: Any) -> Node:
    """The reference node; the site key stays with the shell (SimNet)."""
    return Node(config, **kwargs)


#: Builds every node a SimNet creates (read when the SimNet is constructed).
NODE_FACTORY: Callable[..., Node] = python_node


@dataclass(frozen=True)
class FaultPolicy:
    """Per-link delivery behavior. ``jitter`` is a uniform extra delay in
    seconds on top of ``latency``; independent draws reorder frames."""

    drop: float = 0.0
    dup: float = 0.0
    jitter: float = 0.0
    latency: float = 0.001


@dataclass(frozen=True)
class LogEntry:
    """One datagram as broadcast on the bus. ``sender`` is the physical
    transmitter; ``origin`` is the frame's authenticated author (they
    differ only for injected raws and verbatim ``SendRaw`` effects —
    since ADR 0009 anti-entropy re-issues carry the relayer's own
    origin, with authorship preserved in the body's version). Undecodable
    datagrams (forged, wrong key) keep ``origin``/``ftype`` as None."""

    time: float
    sender: str
    origin: str | None
    ftype: FrameType | None
    seq: int | None
    body: Body | None
    raw: bytes


@dataclass
class SimNode:
    node_id: str
    config: NodeConfig
    node: Node
    driver: SimDriver
    stored: PersistedState
    seed: int
    offline: bool = False
    delivered: list[tuple[float, bytes]] = field(default_factory=list)
    wall_skew: float = 0.0
    address: str = ""
    admission: bool = False
    admitted: tuple[tuple[str, str], ...] | None = None
    logs: list[Log] = field(default_factory=list)


_QueueItem = tuple[float, int, str, bytes, str]  # (time, tie, dst, raw, src)


class SimNet:
    """Virtual clock + virtual multicast bus + N agents."""

    def __init__(self, *, seed: int = 0, psk: bytes = PSK, now: float = T0) -> None:
        self.now = now
        self.psk = psk
        self._seed = seed
        self._rng = random.Random(seed)
        self.nodes: dict[str, SimNode] = {}
        self._order: list[str] = []
        self._queue: list[_QueueItem] = []
        self._tie = 0
        self._default = FaultPolicy()
        self._links: dict[tuple[str | None, str | None], FaultPolicy] = {}
        self._groups: list[frozenset[str]] | None = None
        self.log: list[LogEntry] = []
        self.trace: list[str] = []
        self._cli_seq: dict[str, int] = {}
        self._observed: dict[str, int] = {}
        self._media_n = 0
        self._addresses: dict[str, str] = {"cli-sim": "10.0.0.250"}
        self._factory = NODE_FACTORY

    # -- topology ------------------------------------------------------------

    def add_node(
        self,
        node_id: str,
        role: Role,
        kind: Kind,
        *,
        seed: int | None = None,
        media: Body | None = None,
        wall_skew: float = 0.0,
        admission: bool = False,
        address: str | None = None,
        state: PersistedState | None = None,
    ) -> SimNode:
        if seed is None:
            seed = self._seed * 7919 + len(self._order) + 1
        if role is Role.PRODUCER and media is None:
            media = self._default_media(kind)
        driver = SimDriver(media=media if role is Role.PRODUCER else None)
        config = NodeConfig(node_id=node_id, role=role, kind=kind, name=node_id)
        stored = state or PersistedState()
        node = self._factory(
            config,
            psk=self.psk,
            state=PersistedState(seq=stored.seq, pairing=stored.pairing),
            now=self.now,
            media=driver.describe(),
            seed=seed,
            wall_time=self.now + wall_skew,
            admission=admission,
        )
        sn = SimNode(
            node_id,
            config,
            node,
            driver,
            PersistedState(seq=stored.seq, pairing=stored.pairing),
            seed,
            wall_skew=wall_skew,
            address=address or f"10.0.1.{len(self._order) + 1}",
            admission=admission,
        )
        self.nodes[node_id] = sn
        self._order.append(node_id)
        self._trace(f"boot {node_id} role={role.value} kind={kind.value}")
        self._process(sn, node.start(self.now), self.now)
        return sn

    def _default_media(self, kind: Kind) -> Body:
        self._media_n += 1
        if kind is Kind.HDMI:
            return {"group": f"239.77.0.{self._media_n}", "port": 5004}
        return {"host": f"10.0.0.{self._media_n}", "port": 3240, "busids": ["1-1"]}

    # -- fault scripting -----------------------------------------------------

    def set_faults(
        self,
        policy: FaultPolicy,
        *,
        src: str | None = None,
        dst: str | None = None,
    ) -> None:
        if src is None and dst is None:
            self._default = policy
        else:
            self._links[(src, dst)] = policy
        self._trace(f"faults src={src} dst={dst} {policy}")

    def clear_faults(self) -> None:
        self._default = FaultPolicy()
        self._links.clear()
        self._trace("faults cleared")

    def partition(self, groups: Sequence[Iterable[str]]) -> None:
        self._groups = [frozenset(g) for g in groups]
        self._trace(f"partition {[sorted(g) for g in self._groups]}")

    def heal(self) -> None:
        self._groups = None
        self._trace("heal")

    def set_offline(self, node_id: str, offline: bool = True) -> None:
        self.nodes[node_id].offline = offline
        self._trace(f"{'offline' if offline else 'online'} {node_id}")

    def reboot(
        self,
        node_id: str,
        *,
        lose_state: bool = False,
        at: float | None = None,
        wall_skew: float | None = None,
    ) -> SimNode:
        if at is not None:
            self.run_until(at)
        sn = self.nodes[node_id]
        if wall_skew is not None:
            sn.wall_skew = wall_skew
        self._queue = [item for item in self._queue if item[2] != node_id]
        heapq.heapify(self._queue)
        if lose_state:
            state = PersistedState()
        else:
            state = PersistedState(seq=sn.stored.seq, pairing=sn.stored.pairing)
        sn.driver = SimDriver(media=sn.driver.media)
        sn.node = self._factory(
            sn.config,
            psk=self.psk,
            state=state,
            now=self.now,
            media=sn.driver.describe(),
            seed=sn.seed,
            wall_time=self.wall_of(node_id),
            admission=sn.admission,
        )
        sn.admitted = None
        sn.offline = False
        self._trace(f"reboot {node_id} lose_state={lose_state} skew={sn.wall_skew}")
        self._process(sn, sn.node.start(self.now), self.now)
        return sn

    # -- inputs --------------------------------------------------------------

    def send_intent(
        self,
        op: str,
        consumer: str,
        producer: str | None = None,
        version: Version | None = None,
        origin: str = "cli-sim",
    ) -> tuple[Version, bytes]:
        """Multicast a signed intent from an ephemeral CLI-style origin.

        ``version=None`` builds the version the real CLI would: highest
        pairing-version counter observed on the bus for that consumer,
        plus one. ``seq`` is time-based from the virtual clock (never
        wall time), kept strictly increasing per origin.
        """
        if version is None:
            version = (self._observed.get(consumer, 0) + 1, origin)
        body: Body = {
            "op": op,
            "consumer": consumer,
            "version": [version[0], version[1]],
        }
        if producer is not None:
            body["producer"] = producer
        seq = max(self._cli_seq.get(origin, 0) + 1, int(self.now * 1_000_000_000))
        self._cli_seq[origin] = seq
        raw = encode(FrameType.INT, origin, seq, self.now, body, self.psk)
        self._trace(f"intent {origin} {op} {consumer}<-{producer} v={version}")
        self._broadcast(raw, origin)
        return version, raw

    def inject_raw(
        self, raw: bytes, sender: str = "attacker", *, address: str | None = None
    ) -> None:
        """Put an arbitrary datagram on the bus (forgeries, replays).

        ``address`` is the transport source the receivers see; an attacker
        may claim any address.
        """
        if address is not None:
            self._addresses[sender] = address
        self._trace(f"inject from {sender} ({len(raw)}B)")
        self._broadcast(raw, sender)

    # -- clocks, driver health and the simulated data plane ------------------

    def wall_of(self, node_id: str) -> float:
        """The wall clock ``node_id`` reads now (true time plus its skew)."""
        return self.now + self.nodes[node_id].wall_skew

    def step_clock(self, node_id: str, delta: float) -> None:
        """Step one node's wall clock (NTP correction, bad RTC, operator)."""
        self.nodes[node_id].wall_skew += delta
        self._trace(f"clock {node_id} step {delta:+.0f}s")

    def driver_status(self, node_id: str, status: DriverStatus) -> None:
        """Report backend health from outside a command (e.g. a replug)."""
        sn = self.nodes[node_id]
        sn.driver.set_status(status)
        self._process(sn, sn.node.on_driver_status(status, self.now), self.now)

    def address_of(self, sender: str) -> str | None:
        if sender in self.nodes:
            return self.nodes[sender].address
        return self._addresses.get(sender)

    def data_plane_admits(self, producer: str, address: str) -> bool:
        """Model the producer's admission firewall for new TCP sessions.

        The real nftables effect is ⚠ bench-unverified; this models the
        rendered rules: loopback and allowlisted IPv4 sources only.
        """
        sn = self.nodes[producer]
        if not sn.admission:
            return True  # legacy: data plane open to the LAN
        if address.startswith("127."):
            return True
        return any(addr == address for _, addr in sn.admitted or ())

    # -- clock ---------------------------------------------------------------

    def run_until(self, t: float) -> None:
        while True:
            tq = self._queue[0][0] if self._queue else math.inf
            tn = math.inf
            due: str | None = None
            for node_id in self._order:
                sn = self.nodes[node_id]
                if sn.offline:
                    continue
                deadline = sn.node.next_deadline()
                if deadline < tn:
                    tn, due = deadline, node_id
            nxt = min(tq, tn)
            if nxt > t:
                break
            self.now = max(self.now, nxt)
            if tq <= tn:
                _t, _tie, dst, raw, src = heapq.heappop(self._queue)
                self._deliver(dst, raw, self.now, src)
            else:
                assert due is not None
                sn = self.nodes[due]
                self._process(
                    sn, sn.node.poll(self.now, wall=self.wall_of(due)), self.now
                )
        self.now = max(self.now, t)

    def run(self, seconds: float) -> None:
        self.run_until(self.now + seconds)

    # -- inspection ----------------------------------------------------------

    def pairing_of(self, node_id: str) -> Pairing:
        return self.nodes[node_id].node.pairing

    def state_of(self, node_id: str) -> NodeState:
        return self.nodes[node_id].node.node_state

    def events(self, node_id: str) -> list[Body]:
        """All evt frame bodies this node put on the bus, in order."""
        return [
            entry.body
            for entry in self.log
            if entry.origin == node_id
            and entry.ftype is FrameType.EVT
            and entry.body is not None
        ]

    def event_slugs(self, node_id: str) -> list[str]:
        return [str(body["event"]) for body in self.events(node_id)]

    def event_times(self, node_id: str, slug: str) -> list[float]:
        return [
            entry.time
            for entry in self.log
            if entry.origin == node_id
            and entry.ftype is FrameType.EVT
            and entry.body is not None
            and entry.body.get("event") == slug
        ]

    def acks(self, node_id: str) -> list[Body]:
        return [
            entry.body
            for entry in self.log
            if entry.origin == node_id
            and entry.ftype is FrameType.ACK
            and entry.body is not None
        ]

    def intent_resends(self) -> list[LogEntry]:
        """Anti-entropy re-issues (ADR 0009): int frames sent by a relay
        under its own envelope origin, with the authoring origin
        preserved in body ``version[1]`` — a relay is any int whose
        envelope origin differs from its version's origin."""
        out: list[LogEntry] = []
        for entry in self.log:
            if entry.ftype is not FrameType.INT or entry.body is None:
                continue
            version = entry.body.get("version")
            if (
                isinstance(version, list)
                and len(version) == 2
                and entry.origin is not None
                and entry.origin != version[1]
            ):
                out.append(entry)
        return out

    def drops_of(self, node_id: str) -> dict[str, int]:
        return dict(self.nodes[node_id].node._drops)

    def cached_version(self, node_id: str, consumer: str) -> Version | None:
        entry = self.nodes[node_id].node._intent_cache.get(consumer)
        return None if entry is None else entry.version

    def rejected_version(self, node_id: str, consumer: str) -> Version | None:
        """The version this node has pinned as consumer-rejected
        (ADR 0009 eviction), if any. The node stores (version, producer);
        this returns just the version for assertions."""
        pin = self.nodes[node_id].node._rejected.get(consumer)
        return pin[0] if pin is not None else None

    def observed_winner(self, consumer: str) -> Version:
        """Highest intent version for ``consumer`` that any surviving
        node received and that can still win: max over every node's
        intent cache plus the consumer's own applied version (an applied
        intent was, by definition, received). Versions the consumer
        rejected are evicted from caches on the rejection ack (ADR
        0009), so they are excluded here by construction."""
        best = self.nodes[consumer].node.pairing.version
        for sn in self.nodes.values():
            if sn.offline:
                continue
            cached = self.cached_version(sn.node_id, consumer)
            if cached is not None and version_key(cached) > version_key(best):
                best = cached
        return best

    def agree(self, consumer_id: str) -> Pairing:
        """Assert every online node's view of this consumer's pairing
        matches the consumer's own; return the agreed pairing."""
        own = self.nodes[consumer_id].node.pairing
        for node_id, sn in self.nodes.items():
            if node_id == consumer_id or sn.offline:
                continue
            peer = sn.node.peers.get(consumer_id)
            if peer is None:
                raise AssertionError(f"{node_id} has no record of {consumer_id}")
            if peer.pairing != own:
                raise AssertionError(
                    f"{node_id} sees {consumer_id} as {peer.pairing}, "
                    f"consumer itself has {own}"
                )
        return own

    def quiescent(self, *, drain: int = 500) -> bool:
        """True when no non-periodic work is pending: the delivery queue
        drains without cascading, and no node has a driver retry, an
        anti-entropy resend, or unreported drop counters outstanding.
        (Periodic announces and passive peer-timeout checks remain.)"""
        steps = 0
        while self._queue:
            if steps >= drain:
                return False
            t, _tie, dst, raw, src = heapq.heappop(self._queue)
            self.now = max(self.now, t)
            self._deliver(dst, raw, self.now, src)
            steps += 1
        for sn in self.nodes.values():
            if sn.offline:
                continue
            node = sn.node
            if node._retry_at is not None:
                return False
            if node._resend_at:
                return False
            if node._drops != node._drops_reported:
                return False
        return True

    def trace_tail(self, n: int = 60) -> str:
        return "\n".join(self.trace[-n:])

    # -- internals -----------------------------------------------------------

    def _trace(self, message: str) -> None:
        self.trace.append(f"{self.now:12.3f} {message}")

    def _policy(self, src: str, dst: str) -> FaultPolicy:
        for key in ((src, dst), (src, None), (None, dst)):
            policy = self._links.get(key)
            if policy is not None:
                return policy
        return self._default

    def _blocked(self, src: str, dst: str) -> bool:
        if self._groups is None:
            return False
        src_group = next((g for g in self._groups if src in g), None)
        dst_group = next((g for g in self._groups if dst in g), None)
        return src_group is None or dst_group is None or src_group is not dst_group

    def _observe(self, frame: Frame) -> None:
        if frame.type is FrameType.INT:
            version = frame.body.get("version")
            consumer = frame.body.get("consumer")
            if (
                isinstance(consumer, str)
                and isinstance(version, list)
                and len(version) == 2
                and isinstance(version[0], int)
            ):
                seen = self._observed.get(consumer, 0)
                self._observed[consumer] = max(seen, version[0])
        elif frame.type is FrameType.ANN:
            pairing = frame.body.get("pairing")
            if isinstance(pairing, dict):
                version = pairing.get("version")
                if (
                    isinstance(version, list)
                    and len(version) == 2
                    and isinstance(version[0], int)
                ):
                    seen = self._observed.get(frame.origin, 0)
                    self._observed[frame.origin] = max(seen, version[0])

    def _broadcast(self, raw: bytes, sender: str) -> None:
        origin: str | None = None
        ftype: FrameType | None = None
        seq: int | None = None
        body: Body | None = None
        try:
            frame = decode(raw, self.psk)
        except FrameDrop:
            pass
        else:
            origin, ftype, seq, body = frame.origin, frame.type, frame.seq, frame.body
            self._observe(frame)
        self.log.append(LogEntry(self.now, sender, origin, ftype, seq, body, raw))
        self.trace.append(self._fmt_send(sender, origin, ftype, seq, body))
        for dst in self._order:
            if dst == sender or self._blocked(sender, dst):
                continue
            policy = self._policy(sender, dst)
            if policy.drop and self._rng.random() < policy.drop:
                self._trace(f"  x dropped {sender}->{dst}")
                continue
            self._schedule(dst, raw, sender, policy)
            if policy.dup and self._rng.random() < policy.dup:
                self._schedule(dst, raw, sender, policy)

    def _schedule(self, dst: str, raw: bytes, src: str, policy: FaultPolicy) -> None:
        delay = policy.latency
        if policy.jitter:
            delay += self._rng.uniform(0.0, policy.jitter)
        self._tie += 1
        heapq.heappush(self._queue, (self.now + delay, self._tie, dst, raw, src))

    def _deliver(self, dst: str, raw: bytes, now: float, src: str = "") -> None:
        sn = self.nodes[dst]
        if sn.offline:
            return
        on_datagram = getattr(sn.node, "on_datagram", None)
        if on_datagram is not None:  # a native node decodes for itself
            try:
                decode(raw, self.psk)
            except FrameDrop as drop:
                self._trace(f"  ! {dst} rx-drop {drop.reason.value}")
            else:
                sn.delivered.append((now, raw))
            effects = on_datagram(
                raw, now, wall=self.wall_of(dst), source=self.address_of(src)
            )
            self._process(sn, effects, now)
            return
        try:
            frame = decode(raw, self.psk)
        except FrameDrop as drop:
            self._trace(f"  ! {dst} rx-drop {drop.reason.value}")
            self._process(sn, sn.node.on_drop(drop.reason, now), now)
            return
        sn.delivered.append((now, raw))
        effects = sn.node.on_frame(
            frame, raw, now, wall=self.wall_of(dst), source=self.address_of(src)
        )
        self._process(sn, effects, now)

    def _process(self, sn: SimNode, effects: list[Effect], now: float) -> None:
        status: DriverStatus | None = None
        for effect in effects:
            if isinstance(effect, SendFrame):
                encode_frame = getattr(sn.node, "encode_frame", None)
                if encode_frame is not None:  # native encoder, checked
                    raw = encode_frame(effect, now + sn.wall_skew)
                else:
                    raw = encode(
                        effect.type,
                        sn.node_id,
                        effect.seq,
                        now + sn.wall_skew,
                        effect.body,
                        self.psk,
                    )
                self._broadcast(raw, sn.node_id)
            elif isinstance(effect, SendRaw):
                self._broadcast(effect.raw, sn.node_id)
            elif isinstance(effect, Persist):
                sn.stored = PersistedState(
                    seq=effect.state.seq, pairing=effect.state.pairing
                )
                self._process(
                    sn, sn.node.on_persist_result(effect.request_id, True, now), now
                )
            elif isinstance(effect, AttachMedia):
                status = self._drive(sn, effect, now)
            elif isinstance(effect, DetachMedia):
                status = self._drive(sn, None, now)
            elif isinstance(effect, Log):
                sn.logs.append(effect)
                self._trace(f"{sn.node_id} log: {effect.message}")
            elif isinstance(effect, Admission):
                sn.admitted = effect.peers
                self._trace(f"{sn.node_id} admission {list(effect.peers)}")
        if status is not None:
            self._process(sn, sn.node.on_driver_status(status, now), now)

    def _drive(
        self, sn: SimNode, attach: AttachMedia | None, now: float
    ) -> DriverStatus:
        """Run one driver command, then feed the resulting DriverStatus
        back into the node (scripted failures surface as ERROR)."""
        try:
            if attach is not None:
                sn.driver.attach(attach.producer, attach.media)
            else:
                sn.driver.detach()
        except DriverError:
            pass
        status = sn.driver.poll()
        verb = "attach" if attach is not None else "detach"
        self._trace(f"  {sn.node_id} driver {verb} -> {status.state.value}")
        return status

    def _fmt_send(
        self,
        sender: str,
        origin: str | None,
        ftype: FrameType | None,
        seq: int | None,
        body: Body | None,
    ) -> str:
        if ftype is None or body is None:
            return f"{self.now:12.3f} {sender} !undecodable"
        if ftype is FrameType.EVT:
            desc = str(body.get("event"))
        elif ftype is FrameType.INT:
            desc = (
                f"{body.get('op')} {body.get('consumer')}"
                f"<-{body.get('producer')} v={body.get('version')}"
            )
        elif ftype is FrameType.ACK:
            desc = (
                f"applied={body.get('applied')} {body.get('reason')} "
                f"v={body.get('version')}"
            )
        elif ftype is FrameType.ANN:
            desc = f"{body.get('state')} pairing={body.get('pairing')}"
        else:
            desc = ""
        via = "" if sender == origin else f" via {sender}"
        return f"{self.now:12.3f} {origin} {ftype.value} seq={seq} {desc}{via}"


def replay_into_fresh(net: SimNet, consumer_id: str) -> Pairing:
    """Replay a consumer's full delivered-datagram log, in delivery
    order and at delivery timestamps, into a fresh Node with the same
    identity; return the pairing it converges to (idempotence check)."""
    sn = net.nodes[consumer_id]
    node = Node(
        sn.config,
        state=PersistedState(),
        now=T0,
        media=None,
        seed=sn.seed + 1,
    )

    def persist(effects: list[Effect], now: float) -> None:
        for effect in effects:
            if isinstance(effect, Persist):
                persist(node.on_persist_result(effect.request_id, True, now), now)

    persist(node.start(T0), T0)
    for t, raw in sn.delivered:
        try:
            frame = decode(raw, net.psk)
        except FrameDrop as drop:
            node.on_drop(drop.reason, t)
            continue
        persist(node.on_frame(frame, raw, t), t)
    return node.pairing
