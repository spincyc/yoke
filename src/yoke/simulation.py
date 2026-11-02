"""Deterministic, socket-free control-plane simulation and failure scenarios.

This replaces the I/O shell, never the protocol: real ``Node`` instances,
wire authentication, persistence feedback and ``SimDriver`` handle every
transition. All time and randomness belong to the simulation instance.
Run the packaged suite with ``python -m yoke.simulation --json``.
"""

from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import math
import random
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Literal

from yoke.core.frames import FrameDrop, decode, encode
from yoke.core.node import Node
from yoke.core.types import (
    NS_PER_S,
    PEER_TIMEOUT,
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
)
from yoke.drivers.base import DriverError
from yoke.drivers.sim import SimDriver
from yoke.snoop import Snooper

SIMULATION_PSK = b"yoke-offline-simulation"
WALL_TIME = 1_754_000_000.0


def _nonnegative(value: float, name: str) -> None:
    if isinstance(value, bool) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and nonnegative")


@dataclass(frozen=True)
class FaultPolicy:
    """Independent per-link probabilities and delay in virtual seconds.

    Reordering adds a delay to selected copies; independent jitter also
    naturally reorders traffic. A partition is checked at send and delivery,
    so queued packets cannot cross a subsequently installed partition.
    """

    drop: float = 0.0
    dup: float = 0.0
    latency: float = 0.001
    jitter: float = 0.0
    reorder: float = 0.0

    def __post_init__(self) -> None:
        for name in ("drop", "dup", "reorder"):
            value = getattr(self, name)
            _nonnegative(value, name)
            if value > 1:
                raise ValueError(f"{name} must be at most one")
        _nonnegative(self.latency, "latency")
        _nonnegative(self.jitter, "jitter")


@dataclass
class Counters:
    """Work counters, separate from nondeterministic benchmark timings."""

    transmissions: int = 0
    deliveries: int = 0
    dropped: int = 0
    duplicated: int = 0
    reordered: int = 0
    polls: int = 0
    saves: int = 0
    storage_failures: int = 0
    driver_failures: int = 0
    crashes: int = 0


@dataclass(frozen=True)
class Transmission:
    time: float
    sender: str
    raw: bytes
    frame: Frame | None

    def to_dict(self) -> Body:
        return {
            "time": round(self.time, 9),
            "sender": self.sender,
            "origin": self.frame.origin if self.frame else None,
            "type": self.frame.type.value if self.frame else None,
            "seq": self.frame.seq if self.frame else None,
            "body": self.frame.body if self.frame else None,
            "bytes": len(self.raw),
        }


@dataclass
class Endpoint:
    config: NodeConfig
    node: Node
    driver: SimDriver
    stored: PersistedState
    seed: int
    online: bool = True
    save_failures: int = 0
    crash_on_save: Literal["before", "after"] | None = None
    timer_revision: int = 0
    timer_deadline: float | None = None


class SimulationError(AssertionError):
    """An invariant or scenario expectation failed; CLI returns nonzero."""


_Packet = tuple[float, int, str, str, bytes, int]
_Timer = tuple[float, int, str]


class Simulation:
    """Virtual multicast bus, durable-state slots and deterministic clock.

    Link precedence is exact pair, source, destination, then default.
    Partition groups must be disjoint; unlisted ids are isolated. Include
    intent origins in their group. Observers tap every transmission without
    consuming randomness, scheduling work or becoming protocol peers.
    """

    def __init__(
        self,
        *,
        seed: int = 0,
        psk: bytes = SIMULATION_PSK,
        wall_time: float = WALL_TIME,
        record: bool = True,
    ) -> None:
        _nonnegative(wall_time, "wall_time")
        self.now = 0.0
        self.wall_time = wall_time
        self.seed = seed
        self.psk = psk
        self.nodes: dict[str, Endpoint] = {}
        self.observers: dict[str, Snooper] = {}
        self.transcript: list[Transmission] = []
        self.counters = Counters()
        self._record = record
        self._rng = random.Random(seed)
        self._packets: list[_Packet] = []
        self._timers: list[_Timer] = []
        self._tie = 0
        self._default = FaultPolicy()
        self._links: dict[tuple[str | None, str | None], FaultPolicy] = {}
        self._groups: dict[str, int] | None = None
        self._intent_seq: dict[str, int] = {}
        self._sent_seq: dict[str, int] = {}
        self._delivery_order: dict[tuple[str, str], int] = {}
        self._digest = hashlib.sha256()

    def add_node(
        self,
        node_id: str,
        role: Role,
        kind: Kind = Kind.HDMI,
        *,
        media: Body | None = None,
        state: PersistedState | None = None,
    ) -> Endpoint:
        if node_id in self.nodes:
            raise ValueError(f"duplicate node id: {node_id}")
        config = NodeConfig(node_id=node_id, role=role, kind=kind, name=node_id)
        index = len(self.nodes) + 1
        if role is Role.PRODUCER and media is None:
            media = (
                {"group": f"239.77.{index // 254}.{index % 254 + 1}", "port": 5004}
                if kind is Kind.HDMI
                else {"host": "192.0.2.1", "port": 3240, "busids": ["1-1"]}
            )
        driver = SimDriver(media=media if role is Role.PRODUCER else None)
        stored = _copy_state(state or PersistedState())
        node_seed = self.seed * 7919 + index
        node = Node(
            config,
            state=_copy_state(stored),
            now=self.now,
            wall_time=self.wall_time + self.now,
            media=driver.describe(),
            seed=node_seed,
        )
        endpoint = Endpoint(config, node, driver, stored, node_seed)
        self.nodes[node_id] = endpoint
        self._process(endpoint, node.start(self.now))
        return endpoint

    def add_observer(self, name: str = "snooper") -> Snooper:
        if name in self.observers:
            raise ValueError(f"duplicate observer: {name}")
        observer = Snooper(self.psk, now=self.now, wall_time=self.wall_time + self.now)
        self.observers[name] = observer
        return observer

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
            self._links[src, dst] = policy

    def clear_faults(self) -> None:
        self._default = FaultPolicy()
        self._links.clear()

    def partition(self, groups: Sequence[Iterable[str]]) -> None:
        memberships: dict[str, int] = {}
        for index, group in enumerate(groups):
            for node_id in group:
                if node_id in memberships:
                    raise ValueError(f"partition id belongs to two groups: {node_id}")
                memberships[node_id] = index
        self._groups = memberships

    def heal(self) -> None:
        self._groups = None

    def set_online(self, node_id: str, online: bool) -> None:
        endpoint = self.nodes[node_id]
        endpoint.online = online
        self._arm(endpoint)

    def fail_saves(self, node_id: str, count: int = 1) -> None:
        if type(count) is not int or count < 0:
            raise ValueError("save failure count must be a nonnegative integer")
        self.nodes[node_id].save_failures = count

    def crash_during_next_save(
        self, node_id: str, *, after_write: bool = False
    ) -> None:
        """Crash at the persistence barrier, before write or before feedback."""
        self.nodes[node_id].crash_on_save = "after" if after_write else "before"

    def restart(self, node_id: str, *, wall_time: float | None = None) -> Endpoint:
        """Restart from the last successful save, including sequence reservation.

        ``wall_time`` can move backwards to exercise same-second and clock
        rollback recovery. In-flight bus packets remain in flight.
        """
        endpoint = self.nodes[node_id]
        restart_wall = self.wall_time + self.now if wall_time is None else wall_time
        _nonnegative(restart_wall, "wall_time")
        endpoint.driver = SimDriver(media=endpoint.driver.describe())
        endpoint.node = Node(
            endpoint.config,
            state=_copy_state(endpoint.stored),
            now=self.now,
            wall_time=restart_wall,
            media=endpoint.driver.describe(),
            seed=endpoint.seed,
        )
        endpoint.online = True
        self._process(endpoint, endpoint.node.start(self.now))
        return endpoint

    def intent(
        self,
        consumer: str,
        producer: str | None,
        *,
        version: Version,
        origin: str = "cli-sim",
    ) -> bytes:
        """Send an explicit-version intent, returning bytes for replay tests."""
        body: Body = {
            "op": "pair" if producer is not None else "unpair",
            "consumer": consumer,
            "version": list(version),
        }
        if producer is not None:
            body["producer"] = producer
        seq = max(
            self._intent_seq.get(origin, 0) + 1,
            int(self.wall_time + self.now) * NS_PER_S,
        )
        self._intent_seq[origin] = seq
        raw = encode(
            FrameType.INT, origin, seq, self.wall_time + self.now, body, self.psk
        )
        self.inject(raw, sender=origin)
        return raw

    def inject(self, raw: bytes, *, sender: str = "injected") -> None:
        """Broadcast arbitrary bytes, including wrong-key frames and replays."""
        self._broadcast(raw, sender)

    def run(self, seconds: float, *, max_steps: int = 1_000_000) -> None:
        _nonnegative(seconds, "duration")
        self.run_until(self.now + seconds, max_steps=max_steps)

    def run_until(self, deadline: float, *, max_steps: int = 1_000_000) -> None:
        _nonnegative(deadline, "deadline")
        if deadline < self.now:
            raise ValueError("simulation clock cannot move backwards")
        if type(max_steps) is not int or max_steps < 1:
            raise ValueError("max_steps must be a positive integer")
        steps = 0
        while True:
            self._discard_old_timers()
            packet_at = self._packets[0][0] if self._packets else math.inf
            timer_at = self._timers[0][0] if self._timers else math.inf
            next_at = min(packet_at, timer_at)
            if next_at > deadline:
                break
            if steps >= max_steps:
                raise SimulationError(f"event budget exhausted at {self.now:g}s")
            steps += 1
            self.now = max(self.now, next_at)
            if packet_at <= timer_at:
                _, _, dst, src, raw, serial = heapq.heappop(self._packets)
                self._deliver(dst, src, raw, serial)
            else:
                _, _, node_id = heapq.heappop(self._timers)
                endpoint = self.nodes[node_id]
                endpoint.timer_deadline = None
                self.counters.polls += 1
                self._process(endpoint, endpoint.node.poll(self.now))
        self.now = deadline

    @property
    def digest(self) -> str:
        return self._digest.hexdigest()

    def snapshot(self) -> Body:
        return {
            "time": round(self.now, 9),
            "nodes": {
                node_id: {
                    "online": endpoint.online,
                    "state": endpoint.node.node_state.value,
                    "pairing": {
                        "producer": endpoint.node.pairing.producer,
                        "version": list(endpoint.node.pairing.version),
                    },
                    "stored": endpoint.stored.to_json(),
                    "attached_to": endpoint.driver.attached_to,
                }
                for node_id, endpoint in sorted(self.nodes.items())
            },
            "counters": asdict(self.counters),
            "digest": self.digest,
        }

    def assert_agreement(self, consumer: str, expected: Pairing) -> None:
        endpoint = self.nodes[consumer]
        _require(endpoint.node.pairing == expected, f"{consumer}: pairing differs")
        _require(endpoint.stored.pairing == expected, f"{consumer}: save differs")
        for node_id, other in self.nodes.items():
            if not other.online or node_id == consumer:
                continue
            peer = other.node.peers.get(consumer)
            _require(
                peer is not None and peer.pairing == expected,
                f"{node_id}: has not converged on {consumer}",
            )

    def events(self, node_id: str) -> list[str]:
        return [
            str(entry.frame.body.get("event"))
            for entry in self.transcript
            if entry.sender == node_id
            and entry.frame is not None
            and entry.frame.type is FrameType.EVT
        ]

    def _arm(self, endpoint: Endpoint) -> None:
        deadline = (
            max(self.now, endpoint.node.next_deadline()) if endpoint.online else None
        )
        if deadline == endpoint.timer_deadline:
            return
        endpoint.timer_revision += 1
        endpoint.timer_deadline = deadline
        if deadline is not None:
            heapq.heappush(
                self._timers,
                (
                    deadline,
                    endpoint.timer_revision,
                    endpoint.config.node_id,
                ),
            )

    def _discard_old_timers(self) -> None:
        while self._timers:
            _, revision, node_id = self._timers[0]
            endpoint = self.nodes[node_id]
            if endpoint.online and endpoint.timer_revision == revision:
                break
            heapq.heappop(self._timers)

    def _blocked(self, src: str, dst: str) -> bool:
        return self._groups is not None and (
            src not in self._groups
            or dst not in self._groups
            or self._groups[src] != self._groups[dst]
        )

    def _policy(self, src: str, dst: str) -> FaultPolicy:
        for key in ((src, dst), (src, None), (None, dst)):
            if key in self._links:
                return self._links[key]
        return self._default

    def _broadcast(self, raw: bytes, sender: str) -> None:
        try:
            frame = decode(raw, self.psk)
        except FrameDrop:
            frame = None
        self.counters.transmissions += 1
        serial = self.counters.transmissions
        self._digest.update(f"{self.now.hex()}:{sender}:".encode())
        self._digest.update(len(raw).to_bytes(4, "big"))
        self._digest.update(raw)
        if self._record:
            self.transcript.append(Transmission(self.now, sender, raw, frame))
        for observer in self.observers.values():
            observer.observe(raw, self.now)
        for dst in self.nodes:
            if sender == dst:
                continue
            policy = self._policy(sender, dst)
            if self._blocked(sender, dst) or (
                policy.drop and self._rng.random() < policy.drop
            ):
                self.counters.dropped += 1
                continue
            self._schedule(dst, sender, raw, serial, policy)
            if policy.dup and self._rng.random() < policy.dup:
                self.counters.duplicated += 1
                self._schedule(dst, sender, raw, serial, policy)

    def _schedule(
        self, dst: str, src: str, raw: bytes, serial: int, policy: FaultPolicy
    ) -> None:
        delay = policy.latency
        if policy.jitter:
            delay += self._rng.uniform(0, policy.jitter)
        if policy.reorder and self._rng.random() < policy.reorder:
            delay += max(policy.latency, 0.001) * 2 + policy.jitter
        self._tie += 1
        heapq.heappush(
            self._packets, (self.now + delay, self._tie, dst, src, raw, serial)
        )

    def _deliver(self, dst: str, src: str, raw: bytes, serial: int) -> None:
        endpoint = self.nodes[dst]
        if not endpoint.online or self._blocked(src, dst):
            self.counters.dropped += 1
            return
        self.counters.deliveries += 1
        link = src, dst
        previous = self._delivery_order.get(link, 0)
        if serial < previous:
            self.counters.reordered += 1
        self._delivery_order[link] = max(previous, serial)
        try:
            frame = decode(raw, self.psk)
        except FrameDrop as drop:
            effects = endpoint.node.on_drop(drop.reason, self.now)
        else:
            effects = endpoint.node.on_frame(frame, raw, self.now)
        self._process(endpoint, effects)

    def _process(self, endpoint: Endpoint, effects: list[Effect]) -> None:
        pending = deque(effects)
        node_id = endpoint.config.node_id
        driver_status: DriverStatus | None = None
        while pending and endpoint.online:
            effect = pending.popleft()
            followup: list[Effect] = []
            if isinstance(effect, SendFrame):
                _require(
                    self._sent_seq.get(node_id, 0) < effect.seq <= endpoint.stored.seq,
                    f"{node_id}: outbound sequence reused or not durably reserved",
                )
                self._sent_seq[node_id] = effect.seq
                if effect.type is FrameType.ACK and effect.body.get("applied") is True:
                    _require(
                        effect.body.get("version")
                        == list(endpoint.stored.pairing.version),
                        f"{node_id}: applied ACK precedes durable pairing",
                    )
                self._broadcast(
                    encode(
                        effect.type,
                        node_id,
                        effect.seq,
                        self.wall_time + self.now,
                        effect.body,
                        self.psk,
                    ),
                    node_id,
                )
            elif isinstance(effect, SendRaw):
                self._broadcast(effect.raw, node_id)
            elif isinstance(effect, Persist):
                crash = endpoint.crash_on_save
                endpoint.crash_on_save = None
                if crash == "before":
                    endpoint.online = False
                    self.counters.crashes += 1
                    break
                success = endpoint.save_failures == 0
                if success:
                    endpoint.stored = _copy_state(effect.state)
                    self.counters.saves += 1
                else:
                    endpoint.save_failures -= 1
                    self.counters.storage_failures += 1
                if crash == "after":
                    endpoint.online = False
                    self.counters.crashes += 1
                    break
                followup = endpoint.node.on_persist_result(
                    effect.request_id, success, self.now
                )
            elif isinstance(effect, (AttachMedia, DetachMedia)):
                try:
                    if isinstance(effect, AttachMedia):
                        _require(
                            effect.producer == endpoint.stored.pairing.producer,
                            f"{node_id}: attach precedes durable pairing",
                        )
                        endpoint.driver.attach(effect.producer, effect.media)
                    else:
                        endpoint.driver.detach()
                except DriverError:
                    self.counters.driver_failures += 1
                # Production serializes device commands and discards stale
                # completions. Report only the final command's status after
                # this batch's already-numbered wire effects have drained.
                driver_status = endpoint.driver.poll()
            elif not isinstance(effect, Log):
                raise SimulationError(f"unhandled effect: {effect!r}")
            pending.extendleft(reversed(followup))
        self._arm(endpoint)
        if driver_status is not None and endpoint.online:
            self._process(
                endpoint, endpoint.node.on_driver_status(driver_status, self.now)
            )


def _copy_state(state: PersistedState) -> PersistedState:
    return PersistedState(seq=state.seq, pairing=state.pairing)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SimulationError(message)


@dataclass(frozen=True)
class ScenarioResult:
    name: str
    seed: int
    passed: bool
    checks: tuple[str, ...]
    snapshot: Body
    failure: str | None = None
    trace: tuple[Transmission, ...] = field(default=(), repr=False)

    def to_dict(self) -> Body:
        return {
            "name": self.name,
            "seed": self.seed,
            "passed": self.passed,
            "checks": list(self.checks),
            "failure": self.failure,
            "snapshot": self.snapshot,
        }


SCENARIOS = (
    "clean",
    "loss",
    "duplicates",
    "delay",
    "reordering",
    "partition-conflict",
    "producer-recovery",
    "driver-recovery",
    "storage-recovery",
    "crash-recovery",
    "observer",
)


def _mesh(net: Simulation) -> None:
    net.add_node("prod-a", Role.PRODUCER)
    net.add_node("prod-b", Role.PRODUCER)
    net.add_node("consumer", Role.CONSUMER)
    net.run(3)


def _active(net: Simulation, producer: str, version: Version) -> None:
    net.assert_agreement("consumer", Pairing(producer, version))
    endpoint = net.nodes["consumer"]
    _require(endpoint.node.node_state is NodeState.ACTIVE, "consumer is not active")
    _require(endpoint.driver.attached_to == producer, "driver route differs")


def _scenario(net: Simulation, name: str, checks: list[str]) -> None:
    if name == "observer":
        net.add_observer()
    _mesh(net)
    version: Version = (1, "cli-a")
    if name in {"clean", "loss", "duplicates", "delay", "reordering", "observer"}:
        policies = {
            "loss": FaultPolicy(drop=0.35),
            "duplicates": FaultPolicy(dup=1),
            "delay": FaultPolicy(latency=0.15),
            "reordering": FaultPolicy(reorder=0.7, jitter=0.1),
        }
        net.set_faults(policies.get(name, FaultPolicy()))
        for _ in range(5):
            net.intent("consumer", "prod-a", version=version, origin="cli-a")
            net.run(0.5)
        net.run(5)
        net.clear_faults()
        # A CLI-style retry after link recovery gives a finite liveness
        # guarantee even if every earlier copy was dropped.
        net.intent("consumer", "prod-a", version=version, origin="cli-a")
        net.run(5)
        _active(net, "prod-a", version)
        checks.extend(("eventual-agreement", "durable-route", "idempotent-retry"))
        fault_count = {
            "loss": net.counters.dropped,
            "duplicates": net.counters.duplicated,
            "reordering": net.counters.reordered,
        }.get(name)
        if fault_count is not None:
            _require(fault_count > 0, f"{name}: fault schedule exercised no fault")
            checks.append("fault-exercised")
        if name == "observer":
            baseline = run_scenario("clean", seed=net.seed)
            _require(baseline.passed, "unobserved baseline failed")
            _require(
                baseline.snapshot == net.snapshot(),
                "observer changed transmissions, scheduling or state",
            )
            view = net.observers["snooper"].nodes["consumer"]
            _require(view.pairing_producer == "prod-a", "observer missed route")
            checks.append("observer-noninterference")
    elif name == "partition-conflict":
        net.partition([["prod-a", "consumer", "cli-a"], ["prod-b", "cli-z"]])
        net.intent("consumer", "prod-a", version=version, origin="cli-a")
        winner: Version = (1, "cli-z")
        net.intent("consumer", "prod-b", version=winner, origin="cli-z")
        net.run(5)
        _require(
            net.nodes["consumer"].node.pairing == Pairing("prod-a", version),
            "partition failed to isolate competing intent",
        )
        net.heal()
        net.run(12)
        _active(net, "prod-b", winner)
        checks.extend(("partition-isolation", "total-order-tie-break", "anti-entropy"))
    elif name == "driver-recovery":
        net.nodes["consumer"].driver.fail_attach_times(2)
        net.intent("consumer", "prod-a", version=version, origin="cli-a")
        net.run(0.1)
        _require(
            net.nodes["consumer"].node.node_state is NodeState.ERROR,
            "driver failure did not surface",
        )
        _require(
            net.nodes["consumer"].stored.pairing == Pairing("prod-a", version),
            "driver failure lost retained pairing",
        )
        net.run(12)
        _active(net, "prod-a", version)
        _require(net.counters.driver_failures == 2, "driver faults not exercised")
        checks.extend(("driver-error-visible", "retained-pairing", "retry-recovery"))
    elif name == "storage-recovery":
        net.intent("consumer", "prod-a", version=version, origin="cli-a")
        net.run(3)
        _active(net, "prod-a", version)
        retained = Pairing("prod-a", version)
        replacement: Version = (2, "cli-a")
        net.fail_saves("consumer")
        net.intent("consumer", "prod-b", version=replacement, origin="cli-a")
        net.run(0.1)
        endpoint = net.nodes["consumer"]
        _require(endpoint.node.pairing == retained, "failed save applied")
        _require(endpoint.stored.pairing == retained, "failed save overwrote route")
        _require(endpoint.driver.attached_to == "prod-a", "failed save changed driver")
        _require(
            not any(
                entry.sender == "consumer"
                and entry.frame is not None
                and entry.frame.type is FrameType.ACK
                and entry.frame.body.get("applied") is True
                and entry.frame.body.get("version") == list(replacement)
                for entry in net.transcript
            ),
            "failed save acknowledged",
        )
        net.intent("consumer", "prod-b", version=replacement, origin="cli-a")
        net.run(6)
        _active(net, "prod-b", replacement)
        checks.extend(("failed-save-retains-route", "no-false-ack", "save-retry"))
    elif name == "crash-recovery":
        # A write that never reaches the core's completion callback is
        # nevertheless durable and must be restored after the crash.
        net.crash_during_next_save("consumer", after_write=True)
        net.intent("consumer", "prod-a", version=version, origin="cli-a")
        net.run(0.1)
        endpoint = net.nodes["consumer"]
        _require(not endpoint.online, "crash injection did not execute")
        _require(endpoint.driver.attached_to is None, "driver ran before feedback")
        net.restart("consumer", wall_time=WALL_TIME - 10)
        net.run(8)
        _active(net, "prod-a", version)
        checks.extend(("crash-after-write", "pairing-restored", "clock-rollback-seq"))
    elif name == "producer-recovery":
        net.intent("consumer", "prod-a", version=version, origin="cli-a")
        net.run(3)
        _active(net, "prod-a", version)
        net.set_online("prod-a", False)
        net.run(PEER_TIMEOUT + 2)
        _require(
            net.nodes["consumer"].node.pairing == Pairing("prod-a", version),
            "producer loss discarded pairing",
        )
        _require("peer-lost" in net.events("consumer"), "peer loss not reported")
        net.restart("prod-a")
        net.run(8)
        _active(net, "prod-a", version)
        checks.extend(("peer-loss-visible", "retained-pairing", "producer-return"))
    else:
        raise ValueError(f"unknown scenario: {name}")


def run_scenario(name: str, *, seed: int = 0, trace: bool = False) -> ScenarioResult:
    if name not in SCENARIOS:
        raise ValueError(f"unknown scenario: {name}")
    net = Simulation(seed=seed)
    checks: list[str] = []
    failure: str | None = None
    try:
        _scenario(net, name, checks)
    except SimulationError as exc:
        failure = str(exc)
    return ScenarioResult(
        name,
        seed,
        failure is None,
        tuple(checks),
        net.snapshot(),
        failure,
        tuple(net.transcript) if trace else (),
    )


def run_suite(
    scenarios: Sequence[str] = SCENARIOS, *, seed: int = 0, trace: bool = False
) -> list[ScenarioResult]:
    return [run_scenario(name, seed=seed, trace=trace) for name in scenarios]


def configure_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--scenario", action="append", choices=SCENARIOS, help="repeat; default: all"
    )
    parser.add_argument("--seed", type=int, default=0, help="deterministic fault seed")
    parser.add_argument("--json", action="store_true", help="emit one JSON summary")
    parser.add_argument(
        "--trace", action="store_true", help="also emit transmission JSON lines"
    )


def cmd_simulate(args: argparse.Namespace, emit: Callable[[str], None]) -> int:
    results = run_suite(args.scenario or SCENARIOS, seed=args.seed, trace=args.trace)
    passed = all(result.passed for result in results)
    if args.json:
        emit(
            json.dumps(
                {
                    "schema_version": 1,
                    "seed": args.seed,
                    "passed": passed,
                    "scenarios": [result.to_dict() for result in results],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    else:
        for result in results:
            verdict = "PASS" if result.passed else "FAIL"
            detail = result.failure or ", ".join(result.checks)
            emit(f"{verdict} {result.name}: {detail}")
        count = sum(result.passed for result in results)
        emit(f"{count}/{len(results)} scenarios passed")
    if args.trace:
        for result in results:
            for transmission in result.trace:
                emit(
                    json.dumps(
                        {"scenario": result.name, **transmission.to_dict()},
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
    return 0 if passed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m yoke.simulation",
        description="deterministic offline control-plane failure scenarios",
    )
    configure_parser(parser)
    return cmd_simulate(parser.parse_args(argv), print)


if __name__ == "__main__":
    raise SystemExit(main())
