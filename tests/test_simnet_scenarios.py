"""Deterministic simulation scenarios: one per failure-model row.

Each test drives real ``Node`` state machines through the real wire
codec on the virtual ``SimNet`` bus (tests/simnet.py). No sockets, no
wall clock; every run is reproducible from the net seed.

Failure model (doc/design/ARCHITECTURE.md):
  intent lost, consumer offline, conflicting intents, dup/reorder/replay,
  producer disappears, driver failure, agent reboot (state kept/lost),
  forged frames, partition — plus intent rejections under faults.
"""

from __future__ import annotations

import itertools

import pytest

from tests.simnet import T0, FaultPolicy, SimNet
from yoke.core.frames import encode
from yoke.core.types import (
    Body,
    FrameType,
    Kind,
    NodeState,
    Pairing,
    Role,
    Version,
)

CLI = "cli-sim"


def mesh(
    net: SimNet,
    producers: int = 1,
    consumers: int = 1,
    kind: Kind = Kind.HDMI,
) -> None:
    """Add prod-1..N and con-1..M, then let discovery settle."""
    for i in range(1, producers + 1):
        net.add_node(f"prod-{i}", Role.PRODUCER, kind)
    for i in range(1, consumers + 1):
        net.add_node(f"con-{i}", Role.CONSUMER, kind)
    net.run(3.0)


def applied_acks(net: SimNet, node_id: str, version: Version) -> list[Body]:
    return [
        ack
        for ack in net.acks(node_id)
        if ack["version"] == [version[0], version[1]] and ack["applied"] is True
    ]


# -- row: intent lost in transit ---------------------------------------------


def test_intent_lost_in_transit_cli_resend_converges() -> None:
    net = SimNet(seed=1)
    mesh(net, producers=1, consumers=1)
    # every datagram from the CLI origin is lost
    net.set_faults(FaultPolicy(drop=1.0), src=CLI)
    version, _ = net.send_intent("pair", "con-1", "prod-1", version=(1, CLI))
    net.run(1.0)
    assert net.pairing_of("con-1") == Pairing(None, (0, ""))
    assert net.acks("con-1") == []
    # CLI-style retry: same version, fresh seq, after the fault clears
    net.clear_faults()
    net.send_intent("pair", "con-1", "prod-1", version=version)
    net.run(3.0)
    assert net.agree("con-1") == Pairing("prod-1", version)
    assert net.state_of("con-1") is NodeState.ACTIVE
    assert applied_acks(net, "con-1", version)


# -- row: consumer offline during intent -------------------------------------


def test_consumer_offline_anti_entropy_from_bystander_converges() -> None:
    net = SimNet(seed=2)
    net.add_node("prod-1", Role.PRODUCER, Kind.HDMI)
    net.add_node("watch-1", Role.PRODUCER, Kind.HDMI)  # bystander
    net.add_node("con-1", Role.CONSUMER, Kind.HDMI)
    net.run(3.0)
    net.set_offline("con-1")
    version, _ = net.send_intent("pair", "con-1", "prod-1", version=(1, CLI))
    net.run(2.0)  # bystanders cache the intent; the consumer hears nothing
    assert net.pairing_of("con-1") == Pairing(None, (0, ""))
    net.set_offline("con-1", False)
    net.run(6.0)  # consumer announces v0 -> anti-entropy resend -> apply
    resends = net.intent_resends()
    assert resends, "no anti-entropy re-issue happened"
    for entry in resends:
        # re-issued under a non-author envelope origin (ADR 0009) ...
        assert entry.origin in {"prod-1", "watch-1"}
        # ... with authorship preserved in the body's version
        assert entry.body is not None
        assert entry.body["version"][1] == CLI
    assert applied_acks(net, "con-1", version)
    assert net.agree("con-1") == Pairing("prod-1", version)
    assert net.state_of("con-1") is NodeState.ACTIVE


# -- row: conflicting concurrent intents -------------------------------------


def test_conflicting_concurrent_intents_origin_tiebreak_wins_everywhere() -> None:
    net = SimNet(seed=3)
    mesh(net, producers=2, consumers=2)
    net.set_faults(FaultPolicy(jitter=0.05))  # shuffle arrival order per node
    net.send_intent("pair", "con-1", "prod-1", version=(1, "cli-aa"), origin="cli-aa")
    net.send_intent("pair", "con-1", "prod-2", version=(1, "cli-bb"), origin="cli-bb")
    net.run(5.0)
    winner: Version = (1, "cli-bb")  # same counter -> origin id tie-break
    assert net.agree("con-1") == Pairing("prod-2", winner)
    assert net.state_of("con-1") is NodeState.ACTIVE
    for node_id in ("prod-1", "prod-2", "con-2"):
        assert net.cached_version(node_id, "con-1") == winner
    # the other consumer is untouched (no cross-talk)
    assert net.pairing_of("con-2") == Pairing(None, (0, ""))


# -- row: duplicate / reordered / replayed frames -----------------------------


def _dup_reorder_scenario(policy: FaultPolicy | None) -> SimNet:
    net = SimNet(seed=4)
    mesh(net, producers=2, consumers=2)
    if policy is not None:
        net.set_faults(policy)
    net.send_intent("pair", "con-1", "prod-1", version=(1, CLI))
    net.run(2.0)
    net.send_intent("pair", "con-2", "prod-1", version=(1, CLI))
    net.run(2.0)
    net.send_intent("pair", "con-1", "prod-2", version=(2, CLI))
    net.run(6.0)
    return net


def test_duplicate_reorder_replay_do_not_change_final_state() -> None:
    clean = _dup_reorder_scenario(None)
    faulty = _dup_reorder_scenario(FaultPolicy(dup=0.5, jitter=0.3))
    for consumer in ("con-1", "con-2"):
        assert faulty.pairing_of(consumer) == clean.pairing_of(consumer)
        assert faulty.agree(consumer) == clean.agree(consumer)
    assert faulty.pairing_of("con-1") == Pairing("prod-2", (2, CLI))
    assert faulty.pairing_of("con-2") == Pairing("prod-1", (1, CLI))
    # replay a captured old intent verbatim: dropped as stale, no change
    first_raw = next(entry.raw for entry in faulty.log if entry.ftype is FrameType.INT)
    stale_before = faulty.drops_of("con-1")["stale"]
    faulty.inject_raw(first_raw, sender="attacker")
    faulty.run(2.0)
    assert faulty.drops_of("con-1")["stale"] > stale_before
    assert faulty.pairing_of("con-1") == Pairing("prod-2", (2, CLI))
    assert faulty.pairing_of("con-2") == Pairing("prod-1", (1, CLI))


# -- row: producer disappears -------------------------------------------------


def test_producer_disappears_pending_then_returns_active() -> None:
    net = SimNet(seed=5)
    mesh(net, producers=1, consumers=1)
    version, _ = net.send_intent("pair", "con-1", "prod-1", version=(1, CLI))
    net.run(2.0)
    assert net.state_of("con-1") is NodeState.ACTIVE
    net.partition([["prod-1"], ["con-1", CLI]])
    net.run(8.0)  # > PEER_TIMEOUT
    slugs = net.event_slugs("con-1")
    assert "peer-lost" in slugs
    assert "media-pending" in slugs
    assert net.state_of("con-1") is NodeState.PENDING
    assert net.pairing_of("con-1") == Pairing("prod-1", version)  # retained
    net.heal()
    net.run(5.0)
    slugs = net.event_slugs("con-1")
    assert "peer-returned" in slugs
    assert net.state_of("con-1") is NodeState.ACTIVE
    assert net.nodes["con-1"].driver.attached_to == "prod-1"


# -- row: driver failure ------------------------------------------------------


def test_driver_failure_backoff_retries_then_scripted_recovery() -> None:
    net = SimNet(seed=6)
    mesh(net, producers=1, consumers=1)
    net.nodes["con-1"].driver.fail_attach_times(7)
    version, _ = net.send_intent("pair", "con-1", "prod-1", version=(1, CLI))
    net.run(120.0)
    # the ack precedes the driver failure: reason stays "applied"
    assert applied_acks(net, "con-1", version)
    errors = net.event_times("con-1", "driver-error")
    retries = net.event_times("con-1", "driver-retry")
    assert len(errors) == 7  # initial attach + 6 failed retries
    assert len(retries) == 7  # 7th retry succeeds
    marks = [errors[0], *retries]
    deltas = [b - a for a, b in itertools.pairwise(marks)]
    assert deltas == pytest.approx([1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0], abs=1e-6)
    assert "media-up" in net.event_slugs("con-1")
    assert net.state_of("con-1") is NodeState.ACTIVE
    assert net.agree("con-1") == Pairing("prod-1", version)


# -- row: agent reboot (state kept) -------------------------------------------


def test_reboot_keeping_state_reapplies_pairing_and_seq_floor() -> None:
    net = SimNet(seed=7)
    net.add_node("prod-1", Role.PRODUCER, Kind.HDMI)
    net.add_node("watch-1", Role.PRODUCER, Kind.HDMI)
    net.add_node("con-1", Role.CONSUMER, Kind.HDMI)
    net.run(3.0)
    version, _ = net.send_intent("pair", "con-1", "prod-1", version=(1, CLI))
    net.run_until(T0 + 80.0)
    assert net.state_of("con-1") is NodeState.ACTIVE
    stale_before = {n: net.drops_of(n)["stale"] for n in ("prod-1", "watch-1")}
    reboot_at = net.now
    sn = net.reboot("con-1")
    # pairing re-applied from disk before the first announce
    assert sn.node.pairing == Pairing("prod-1", version)
    assert sn.node.node_state is NodeState.PENDING
    net.run(6.0)
    assert net.state_of("con-1") is NodeState.ACTIVE
    assert net.nodes["con-1"].driver.attached_to == "prod-1"
    assert net.agree("con-1") == Pairing("prod-1", version)
    # seq floor respected: nobody dropped its post-reboot frames as stale
    for node_id, before in stale_before.items():
        assert net.drops_of(node_id)["stale"] == before
        peer = net.nodes[node_id].node.peers["con-1"]
        assert peer.last_seen > reboot_at
    post = [
        entry
        for entry in net.log
        if entry.origin == "con-1" and entry.time >= reboot_at
    ]
    assert post and all(
        entry.seq is not None and entry.seq >= int(reboot_at) for entry in post
    )


# -- row: agent reboot (state lost) -------------------------------------------


def test_reboot_losing_state_converges_back_via_anti_entropy() -> None:
    net = SimNet(seed=8)
    net.add_node("prod-1", Role.PRODUCER, Kind.HDMI)
    net.add_node("watch-1", Role.PRODUCER, Kind.HDMI)
    net.add_node("con-1", Role.CONSUMER, Kind.HDMI)
    net.run(3.0)
    version, _ = net.send_intent("pair", "con-1", "prod-1", version=(1, CLI))
    net.run_until(T0 + 80.0)
    stale_before = {n: net.drops_of(n)["stale"] for n in ("prod-1", "watch-1")}
    reboot_at = net.now
    sn = net.reboot("con-1", lose_state=True)
    assert sn.node.pairing == Pairing(None, (0, ""))
    net.run(8.0)
    resends = [e for e in net.intent_resends() if e.time >= reboot_at]
    assert resends and all(e.origin in {"prod-1", "watch-1"} for e in resends)
    assert net.agree("con-1") == Pairing("prod-1", version)
    assert net.state_of("con-1") is NodeState.ACTIVE
    applied_after = [
        t for t in net.event_times("con-1", "pairing-applied") if t >= reboot_at
    ]
    assert applied_after
    # its fresh time-floored seq re-entered above previously sent values
    for node_id, before in stale_before.items():
        assert net.drops_of(node_id)["stale"] == before


# -- row: forged / altered frames ---------------------------------------------


def test_forged_frame_counted_as_auth_drop_and_never_mutates_pairing() -> None:
    net = SimNet(seed=9)
    mesh(net, producers=1, consumers=1)
    body: Body = {
        "op": "pair",
        "consumer": "con-1",
        "producer": "prod-1",
        "version": [9, "attacker"],
    }
    forged = encode(FrameType.INT, "attacker", 1, net.now, body, b"wrong-psk")
    net.inject_raw(forged, sender="attacker")
    net.run(3.0)
    assert net.drops_of("con-1")["auth"] == 1
    assert net.drops_of("prod-1")["auth"] == 1
    rx_drops = [b for b in net.events("con-1") if b.get("event") == "rx-drop"]
    assert rx_drops and rx_drops[-1]["detail"]["auth"] == 1
    assert net.pairing_of("con-1") == Pairing(None, (0, ""))
    assert net.acks("con-1") == []


# -- row: partition -----------------------------------------------------------


def test_partition_intent_one_side_heal_converges_to_highest_version() -> None:
    net = SimNet(seed=10)
    mesh(net, producers=2, consumers=2)
    net.send_intent("pair", "con-1", "prod-1", version=(1, "cli-a"), origin="cli-a")
    net.run(2.0)
    net.partition([["prod-1", "con-1", "cli-a"], ["prod-2", "con-2", "cli-b"]])
    # side A: applied by the consumer itself
    net.send_intent("pair", "con-1", "prod-1", version=(2, "cli-a"), origin="cli-a")
    # side B: cannot reach con-1; only cached by the B-side bystanders
    winner: Version = (3, "cli-b")
    net.send_intent("pair", "con-1", "prod-2", version=winner, origin="cli-b")
    net.run(10.0)
    assert net.pairing_of("con-1") == Pairing("prod-1", (2, "cli-a"))
    assert net.cached_version("prod-2", "con-1") == winner
    net.heal()
    net.run(10.0)
    resends = [
        e
        for e in net.intent_resends()
        if e.body is not None and e.body["version"][1] == "cli-b"
    ]
    assert resends and all(e.origin in {"prod-2", "con-2"} for e in resends)
    assert net.agree("con-1") == Pairing("prod-2", winner)
    assert net.state_of("con-1") is NodeState.ACTIVE
    for node_id in ("prod-1", "prod-2", "con-2"):
        assert net.cached_version(node_id, "con-1") == winner


# -- row: rejections under faults ---------------------------------------------


def test_kind_mismatch_rejected_under_faults() -> None:
    net = SimNet(seed=11)
    net.add_node("prod-u", Role.PRODUCER, Kind.USB)
    net.add_node("con-1", Role.CONSUMER, Kind.HDMI)
    net.run(3.0)  # consumer must know the producer's kind to reject
    net.set_faults(FaultPolicy(dup=0.3, jitter=0.05))
    net.send_intent("pair", "con-1", "prod-u", version=(1, CLI))
    net.run(3.0)
    acks = net.acks("con-1")
    assert acks
    assert acks[0]["applied"] is False
    assert acks[0]["reason"] == "kind-mismatch"
    assert acks[0]["version"] == [1, CLI]
    assert net.pairing_of("con-1") == Pairing(None, (0, ""))
    assert net.state_of("con-1") is NodeState.IDLE
    # ADR 0009: the rejection ack evicts the cached intent everywhere,
    # so the doomed version is not re-offered
    assert net.cached_version("prod-u", "con-1") is None
    assert net.rejected_version("prod-u", "con-1") == (1, CLI)


def test_self_pair_rejected_under_faults() -> None:
    net = SimNet(seed=12)
    net.add_node("prod-1", Role.PRODUCER, Kind.HDMI)
    net.add_node("con-1", Role.CONSUMER, Kind.HDMI)
    net.run(3.0)
    net.set_faults(FaultPolicy(dup=0.3, jitter=0.05))
    net.send_intent("pair", "con-1", "con-1", version=(1, CLI))
    net.run(3.0)
    acks = net.acks("con-1")
    assert acks
    assert acks[0]["applied"] is False
    assert acks[0]["reason"] == "self-pair"
    assert net.pairing_of("con-1") == Pairing(None, (0, ""))
    assert net.state_of("con-1") is NodeState.IDLE
    # rejection eviction (ADR 0009) applies to self-pair as well
    assert net.cached_version("prod-1", "con-1") is None
    assert net.rejected_version("prod-1", "con-1") == (1, CLI)


# -- regression: re-issued anti-entropy survives author seq advance -----------


def test_reissued_resend_converges_after_author_seq_advance() -> None:
    # Regression for the livelock fixed by ADR 0009: with verbatim
    # resends, once the consumer had seen any higher-seq frame from the
    # intent's author (here, a later intent for a different consumer),
    # replay defense stale-dropped every re-offer of the missed intent
    # forever. Re-issued resends carry the relayer's own origin and
    # fresh seq, so the returning consumer accepts and converges.
    net = SimNet(seed=13)
    net.add_node("prod-1", Role.PRODUCER, Kind.HDMI)
    net.add_node("con-1", Role.CONSUMER, Kind.HDMI)
    net.add_node("con-2", Role.CONSUMER, Kind.HDMI)
    net.run(3.0)
    # con-1 misses the intent addressed to it (partitioned away) ...
    missed: Version = (1, CLI)
    net.partition([["con-1"], ["prod-1", "con-2", CLI]])
    net.send_intent("pair", "con-1", "prod-1", version=missed)
    net.run(1.0)
    net.heal()
    # ... then hears a later, higher-seq frame from the same author
    net.send_intent("pair", "con-2", "prod-1", version=(1, CLI))
    net.run(20.0)
    assert net.pairing_of("con-2") == Pairing("prod-1", (1, CLI))
    resends = [
        e
        for e in net.intent_resends()
        if e.body is not None and e.body.get("consumer") == "con-1"
    ]
    assert resends, "a bystander should have re-issued the missed intent"
    for entry in resends:
        assert entry.origin in {"prod-1", "con-2"}  # relayer's own origin
        assert entry.body is not None
        assert entry.body["version"][1] == CLI  # authorship preserved
    assert applied_acks(net, "con-1", missed)
    assert net.agree("con-1") == Pairing("prod-1", missed)
    assert net.state_of("con-1") is NodeState.ACTIVE
    assert net.quiescent()


# -- ADR 0009: rejected intents are evicted, not re-offered forever -----------


def test_rejected_intent_evicted_then_higher_valid_intent_converges() -> None:
    # A rejected highest-version intent (kind-mismatch here) must not be
    # re-offered forever: bystanders observe the rejection ack, evict
    # the cached version, pin it as rejected, and the mesh quiesces. A
    # subsequent higher-version valid intent still converges (ADR 0009).
    net = SimNet(seed=14)
    net.add_node("prod-u", Role.PRODUCER, Kind.USB)
    net.add_node("prod-1", Role.PRODUCER, Kind.HDMI)
    net.add_node("con-1", Role.CONSUMER, Kind.HDMI)
    net.run(3.0)
    bad: Version = (1, CLI)
    net.send_intent("pair", "con-1", "prod-u", version=bad)  # kind-mismatch
    net.run(5.0)
    acks = net.acks("con-1")
    assert acks and acks[0]["applied"] is False
    assert acks[0]["reason"] == "kind-mismatch"
    for bystander in ("prod-u", "prod-1"):
        assert net.cached_version(bystander, "con-1") is None  # evicted
        assert net.rejected_version(bystander, "con-1") == bad  # pinned
    net.run(10.0)
    assert net.intent_resends() == []  # never re-offered
    assert net.quiescent()
    assert net.pairing_of("con-1") == Pairing(None, (0, ""))
    good: Version = (2, CLI)
    net.send_intent("pair", "con-1", "prod-1", version=good)
    net.run(5.0)
    assert net.agree("con-1") == Pairing("prod-1", good)
    assert net.state_of("con-1") is NodeState.ACTIVE


# -- scenario: shared HDMI producer, two independent consumers -----------------


def test_two_consumers_one_hdmi_producer_both_active() -> None:
    # HDMI producers are inherently shareable: each decoder joins the
    # encoder's multicast group independently, so two consumers pairing
    # to the same producer both go active, each under its own
    # per-consumer version (ARCHITECTURE.md, "What yoke is not").
    net = SimNet(seed=15)
    mesh(net, producers=1, consumers=2)
    v1, _ = net.send_intent("pair", "con-1", "prod-1", version=(1, CLI))
    net.run(2.0)
    v2, _ = net.send_intent("pair", "con-2", "prod-1", version=(1, CLI))
    net.run(4.0)
    assert net.agree("con-1") == Pairing("prod-1", v1)
    assert net.agree("con-2") == Pairing("prod-1", v2)
    for consumer in ("con-1", "con-2"):
        assert net.state_of(consumer) is NodeState.ACTIVE
        assert net.nodes[consumer].driver.attached_to == "prod-1"
        assert applied_acks(net, consumer, (1, CLI))
    assert net.quiescent()


# -- scenario: re-pair a consumer from one producer to another -----------------


def test_repair_consumer_from_one_producer_to_another() -> None:
    net = SimNet(seed=16)
    mesh(net, producers=2, consumers=1)
    first, _ = net.send_intent("pair", "con-1", "prod-1", version=(1, CLI))
    net.run(2.0)
    assert net.pairing_of("con-1") == Pairing("prod-1", first)
    assert net.state_of("con-1") is NodeState.ACTIVE
    second, _ = net.send_intent("pair", "con-1", "prod-2", version=(2, CLI))
    net.run(4.0)
    # NOTE: re-pairing replaces the attachment with a direct re-attach,
    # not a DetachMedia/AttachMedia sequence: the driver contract treats
    # attach-while-attached as a switch (SimDriver: "idempotent
    # re-attach"; an IGMP driver leaves the old group as part of joining
    # the new one). Only an explicit unpair emits DetachMedia.
    calls = net.nodes["con-1"].driver.calls
    assert [c[1] for c in calls if c[0] == "attach"] == ["prod-1", "prod-2"]
    assert ("detach",) not in calls
    assert net.nodes["con-1"].driver.attached_to == "prod-2"
    assert net.agree("con-1") == Pairing("prod-2", second)
    assert net.state_of("con-1") is NodeState.ACTIVE
    assert applied_acks(net, "con-1", second)
    assert net.quiescent()


# -- scenario: producer reboots midstream --------------------------------------


def test_producer_reboot_midstream_consumer_recovers() -> None:
    net = SimNet(seed=17)
    mesh(net, producers=1, consumers=1)
    version, _ = net.send_intent("pair", "con-1", "prod-1", version=(1, CLI))
    net.run(2.0)
    assert net.state_of("con-1") is NodeState.ACTIVE
    # the producer crashes mid-stream: silent for longer than
    # PEER_TIMEOUT before its replacement process comes up
    net.set_offline("prod-1")
    net.run(8.0)  # > PEER_TIMEOUT
    slugs = net.event_slugs("con-1")
    assert "peer-lost" in slugs
    assert "media-pending" in slugs
    assert net.state_of("con-1") is NodeState.PENDING
    assert net.pairing_of("con-1") == Pairing("prod-1", version)  # retained
    # producers are passive, so losing persisted state is harmless
    net.reboot("prod-1", lose_state=True)
    net.run(5.0)
    assert "peer-returned" in net.event_slugs("con-1")
    assert net.state_of("con-1") is NodeState.ACTIVE
    assert net.nodes["con-1"].driver.attached_to == "prod-1"
    assert net.agree("con-1") == Pairing("prod-1", version)
    assert net.quiescent()


# -- scenario: forged tear-down of a live pairing under a wrong PSK ------------


def test_forged_frame_wrong_psk_ignored() -> None:
    # SimNet's bus verifies every datagram against one shared PSK, so a
    # per-node PSK mismatch cannot be modeled per-link; the equivalent
    # field failure is a mis-keyed (or malicious) sender whose frames
    # hit the group: every receiver drops them as auth and the live
    # pairing is untouched. Here the forgery tries to unpair an ACTIVE
    # consumer.
    net = SimNet(seed=18)
    mesh(net, producers=1, consumers=1)
    version, _ = net.send_intent("pair", "con-1", "prod-1", version=(1, CLI))
    net.run(2.0)
    assert net.state_of("con-1") is NodeState.ACTIVE
    body: Body = {"op": "unpair", "consumer": "con-1", "version": [9, "attacker"]}
    forged = encode(FrameType.INT, "attacker", 1, net.now, body, b"not-the-psk")
    auth_before = {n: net.drops_of(n)["auth"] for n in ("prod-1", "con-1")}
    net.inject_raw(forged, sender="attacker")
    net.run(3.0)
    for node_id, before in auth_before.items():
        assert net.drops_of(node_id)["auth"] == before + 1
    rx_drops = [b for b in net.events("con-1") if b.get("event") == "rx-drop"]
    assert rx_drops
    assert rx_drops[-1]["detail"]["auth"] == auth_before["con-1"] + 1
    assert net.agree("con-1") == Pairing("prod-1", version)
    assert net.state_of("con-1") is NodeState.ACTIVE
    assert net.nodes["con-1"].driver.attached_to == "prod-1"
    assert net.quiescent()


# -- scenario: unpair then re-pair to a different producer ---------------------


def test_unpair_then_repair_same_consumer() -> None:
    net = SimNet(seed=19)
    mesh(net, producers=2, consumers=1)
    v_pair, _ = net.send_intent("pair", "con-1", "prod-1", version=(1, CLI))
    net.run(2.0)
    assert net.pairing_of("con-1") == Pairing("prod-1", v_pair)
    assert net.state_of("con-1") is NodeState.ACTIVE
    v_unpair, _ = net.send_intent("unpair", "con-1", version=(2, CLI))
    net.run(2.0)
    assert net.pairing_of("con-1") == Pairing(None, v_unpair)
    assert net.state_of("con-1") is NodeState.IDLE
    assert net.nodes["con-1"].driver.attached_to is None
    # NOTE: unpair emits no "media-down" event: _apply_unpair sets the
    # node IDLE before DetachMedia runs, so when the driver's IDLE
    # status is fed back the state is already IDLE and the transition
    # branch does not fire. The teardown surfaces as "pairing-applied"
    # (producer null) plus the immediate idle announce; "media-down"
    # only fires when the driver reports IDLE while the node still
    # believes media is up (spontaneous media loss).
    assert "media-down" not in net.event_slugs("con-1")
    v_repair, _ = net.send_intent("pair", "con-1", "prod-2", version=(3, CLI))
    net.run(4.0)
    calls = net.nodes["con-1"].driver.calls
    assert [c[0] for c in calls] == ["attach", "detach", "attach"]
    assert (calls[0][1], calls[2][1]) == ("prod-1", "prod-2")
    assert net.agree("con-1") == Pairing("prod-2", v_repair)
    assert net.state_of("con-1") is NodeState.ACTIVE
    assert net.event_slugs("con-1").count("pairing-applied") == 3
    # applied versions are strictly increasing across the sequence
    applied = [a["version"] for a in net.acks("con-1") if a["applied"] is True]
    assert applied == [[1, CLI], [2, CLI], [3, CLI]]
    assert net.quiescent()
