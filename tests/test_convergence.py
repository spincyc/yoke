"""Seeded randomized convergence properties for the simulated mesh.

For each fixed seed: build a random mesh (2-4 producers, 2-4 consumers,
mixed hdmi/usb), run a random schedule of intents from a few CLI-style
origins interleaved with faults (drops, dups, reorder, one partition and
heal, one state-keeping reboot), then settle fault-free and assert:

(a) every consumer's pairing equals the highest-version intent targeting
    it that at least one surviving node ever received (an intent whose
    every copy was dropped before any node heard it cannot win, and a
    version the consumer rejected cannot win — rejection acks evict it
    from every cache, ADR 0009);
(b) pairings are independent per consumer — the winning version must be
    one that was addressed to that consumer (no cross-talk);
(c) idempotence — replaying the consumer's full delivered-frame log into
    a fresh node yields the same pairing;
(d) the mesh goes quiescent (no non-periodic work pending).

Origins freely target multiple consumers: since ADR 0009 anti-entropy
re-issues cached intents under the relayer's own origin and seq, an
author's later traffic can no longer stale-out a missed intent (the
verbatim-resend livelock this suite originally caught). The workload
also includes occasional invalid self-pair intents to exercise
rejection eviction under faults. Intents are re-sent a few times like
the real CLI (CLI_RETRY). On failure the seed and a compact event-trace
tail are printed for reproduction. Everything is stdlib + pytest; no
sockets, no wall clock.
"""

from __future__ import annotations

import heapq
import random

import pytest

from tests.simnet import T0, FaultPolicy, SimNet, replay_into_fresh
from yoke.core.types import VERSION_ZERO, Kind, Pairing, Role, Version, version_key

SEEDS = list(range(12))
CLI_SENDS = 5  # sends per intent, mirroring CLI_RETRY
RETRY_SPACING = 0.4
SETTLE = 40.0

# one scheduled action: (kind, payload...)
_Action = tuple[object, ...]


def _build_mesh(net: SimNet, rng: random.Random) -> tuple[list[str], list[str]]:
    n_prod = rng.randint(2, 4)
    n_cons = rng.randint(2, 4)
    producers: list[str] = []
    prod_kinds: dict[str, Kind] = {}
    for i in range(1, n_prod + 1):
        kind = rng.choice([Kind.HDMI, Kind.USB])
        node_id = f"prod-{i}"
        net.add_node(node_id, Role.PRODUCER, kind)
        producers.append(node_id)
        prod_kinds[node_id] = kind
    available_kinds = sorted({k.value for k in prod_kinds.values()})
    consumers: list[str] = []
    for i in range(1, n_cons + 1):
        kind = Kind(rng.choice(available_kinds))
        node_id = f"con-{i}"
        net.add_node(node_id, Role.CONSUMER, kind)
        consumers.append(node_id)
    return producers, consumers


def _matching_producers(net: SimNet, producers: list[str], consumer: str) -> list[str]:
    kind = net.nodes[consumer].config.kind
    return [p for p in producers if net.nodes[p].config.kind is kind]


def _run_schedule(
    net: SimNet,
    rng: random.Random,
    producers: list[str],
    consumers: list[str],
) -> dict[str, dict[Version, tuple[str, str | None]]]:
    """Execute intents + faults chronologically; return, per consumer,
    every intent addressed to it keyed by version."""
    origins = [f"cli-{s}" for s in ("aa", "bb", "cc")[: rng.randint(1, 3)]]
    net.set_faults(
        FaultPolicy(
            drop=rng.uniform(0.0, 0.3),
            dup=rng.uniform(0.0, 0.3),
            jitter=rng.uniform(0.0, 0.4),
        )
    )
    schedule: list[tuple[float, int, _Action]] = []
    tie = 0

    def push(t: float, action: _Action) -> None:
        nonlocal tie
        tie += 1
        heapq.heappush(schedule, (t, tie, action))

    for _ in range(rng.randint(5, 15)):
        origin = rng.choice(origins)
        consumer = rng.choice(consumers)
        matching = _matching_producers(net, producers, consumer)
        roll = rng.random()
        if roll < 0.1:
            # invalid on purpose: self-pair is rejected by the consumer
            # and its version evicted everywhere via the ack (ADR 0009)
            op, producer = "pair", consumer
        elif not matching or roll < 0.25:
            op, producer = "unpair", None
        else:
            op, producer = "pair", rng.choice(matching)
        push(rng.uniform(5.0, 60.0), ("intent", consumer, op, producer, origin))
    all_ids = producers + consumers + origins
    split = rng.sample(all_ids, rng.randint(1, len(all_ids) - 1))
    other = [i for i in all_ids if i not in split]
    t_part = rng.uniform(10.0, 40.0)
    push(t_part, ("partition", split, other))
    push(t_part + rng.uniform(5.0, 15.0), ("heal",))
    push(rng.uniform(20.0, 60.0), ("reboot", rng.choice(producers + consumers)))

    intents: dict[str, dict[Version, tuple[str, str | None]]] = {}
    while schedule:
        t, _, action = heapq.heappop(schedule)
        net.run_until(T0 + t)
        kind = action[0]
        if kind == "intent":
            _, consumer, op, producer, origin = action
            assert isinstance(consumer, str) and isinstance(op, str)
            assert isinstance(origin, str)
            assert producer is None or isinstance(producer, str)
            version, _raw = net.send_intent(op, consumer, producer, origin=origin)
            intents.setdefault(consumer, {})[version] = (op, producer)
            for k in range(1, CLI_SENDS):
                push(
                    t + RETRY_SPACING * k,
                    ("retry", consumer, op, producer, version, origin),
                )
        elif kind == "retry":
            _, consumer, op, producer, version, origin = action
            assert isinstance(consumer, str) and isinstance(op, str)
            assert isinstance(origin, str) and isinstance(version, tuple)
            assert producer is None or isinstance(producer, str)
            net.send_intent(op, consumer, producer, version=version, origin=origin)
        elif kind == "partition":
            _, split_ids, other_ids = action
            assert isinstance(split_ids, list) and isinstance(other_ids, list)
            net.partition([split_ids, other_ids])
        elif kind == "heal":
            net.heal()
        else:  # reboot, state kept
            _, target = action
            assert isinstance(target, str)
            net.reboot(target, lose_state=False)
    return intents


@pytest.mark.parametrize("seed", SEEDS)
def test_random_mesh_converges(seed: int) -> None:
    rng = random.Random(seed)
    net = SimNet(seed=seed)
    producers, consumers = _build_mesh(net, rng)
    net.run(3.0)  # discovery
    intents = _run_schedule(net, rng, producers, consumers)
    # fault-free settling
    net.heal()
    net.clear_faults()
    net.run(SETTLE)
    try:
        # (d) quiescent: only periodic announces remain
        assert net.quiescent(), "mesh did not go quiescent"
        for consumer in consumers:
            # (a) winner = highest version any surviving node received
            winner = net.observed_winner(consumer)
            pairing = net.pairing_of(consumer)
            if winner == VERSION_ZERO:
                assert pairing == Pairing(None, VERSION_ZERO), (
                    f"{consumer} paired without any observed intent: {pairing}"
                )
            else:
                # (b) the winning version must have been addressed to
                # this consumer (no cross-talk between consumers)
                assert winner in intents.get(consumer, {}), (
                    f"{consumer} converged to {winner}, which was never addressed to it"
                )
                op, producer = intents[consumer][winner]
                # a rejected (self-pair) version can never win: its
                # eviction removes it from every cache before settling
                assert not (op == "pair" and producer == consumer), (
                    f"{consumer}: rejected self-pair version {winner} won"
                )
                expected = Pairing(producer if op == "pair" else None, winner)
                assert pairing == expected, (
                    f"{consumer}: got {pairing}, expected {expected}"
                )
                assert version_key(pairing.version) == version_key(winner)
            # every node's view of this consumer matches its own state
            assert net.agree(consumer) == pairing
            # (c) idempotence: replaying the delivered-frame log into a
            # fresh consumer yields the same pairing
            assert replay_into_fresh(net, consumer) == pairing, (
                f"{consumer}: replay of delivered log diverged"
            )
    except AssertionError:
        print(f"\n--- seed={seed} failed; trace tail ---")
        print(net.trace_tail(100))
        raise
