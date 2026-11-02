"""The installed control harness uses real protocol transitions, entirely offline."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from yoke.core.frames import encode
from yoke.core.types import (
    NS_PER_S,
    SEQ_FUTURE_MARGIN_S,
    FrameType,
    NodeState,
    Pairing,
    Role,
)
from yoke.simulation import (
    SCENARIOS,
    WALL_TIME,
    FaultPolicy,
    Simulation,
    SimulationError,
    main,
    run_scenario,
    run_suite,
)


@pytest.mark.parametrize("seed", [0, 1, 7, 42])
@pytest.mark.parametrize("name", SCENARIOS)
def test_failure_scenarios(name: str, seed: int) -> None:
    result = run_scenario(name, seed=seed)
    assert result.passed, result.to_dict()
    assert result.checks


def test_suite_is_reproducible_including_transmission_bytes() -> None:
    first = run_suite(seed=17, trace=True)
    second = run_suite(seed=17, trace=True)
    assert first == second
    assert all(result.passed for result in first)


def test_simulation_opens_no_socket_and_reads_no_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("simulation attempted external I/O or a clock read")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(time, "time", forbidden)
    monkeypatch.setattr(time, "time_ns", forbidden)
    monkeypatch.setattr(time, "monotonic", forbidden)
    assert all(result.passed for result in run_suite(seed=5))


def test_cli_json_and_trace_are_machine_readable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--scenario", "clean", "--seed", "9", "--json", "--trace"]) == 0
    lines = capsys.readouterr().out.splitlines()
    summary = json.loads(lines[0])
    assert summary["schema_version"] == 1
    assert summary["passed"] is True
    assert [result["name"] for result in summary["scenarios"]] == ["clean"]
    transmissions = [json.loads(line) for line in lines[1:]]
    assert transmissions
    assert all(entry["scenario"] == "clean" for entry in transmissions)
    assert any(entry["type"] == "ack" for entry in transmissions)


def test_cli_reports_scenario_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(self: Simulation, consumer: str, expected: Pairing) -> None:
        raise SimulationError("injected agreement failure")

    monkeypatch.setattr(Simulation, "assert_agreement", broken)
    assert main(["--scenario", "clean", "--json"]) == 1
    summary = json.loads(capsys.readouterr().out)
    assert summary["passed"] is False
    assert summary["scenarios"][0]["failure"] == "injected agreement failure"


@pytest.mark.parametrize(
    "options",
    [
        {"drop": -0.1},
        {"drop": 1.1},
        {"dup": float("nan")},
        {"latency": float("inf")},
        {"jitter": -1.0},
        {"reorder": True},
    ],
)
def test_fault_policy_rejects_invalid_options(options: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        FaultPolicy(**options)


def _pair_net() -> Simulation:
    net = Simulation(seed=3)
    net.add_node("producer", Role.PRODUCER)
    net.add_node("consumer", Role.CONSUMER)
    net.run(3)
    return net


def test_link_override_and_queued_partition_packets() -> None:
    net = _pair_net()
    net.set_faults(FaultPolicy(drop=1))
    net.set_faults(FaultPolicy(latency=1), src="cli-a", dst="consumer")
    net.intent("consumer", "producer", version=(1, "cli-a"), origin="cli-a")
    net.partition([["producer", "consumer"], ["cli-a"]])
    net.run(1.1)
    assert net.nodes["consumer"].node.pairing == Pairing(None, (0, ""))
    net.heal()
    net.clear_faults()
    net.intent("consumer", "producer", version=(1, "cli-a"), origin="cli-a")
    net.run(5)
    net.assert_agreement("consumer", Pairing("producer", (1, "cli-a")))


def test_crash_before_save_preserves_old_committed_route() -> None:
    net = _pair_net()
    first = Pairing("producer", (1, "cli-a"))
    net.intent("consumer", "producer", version=first.version, origin="cli-a")
    net.run(2)
    net.crash_during_next_save("consumer")
    net.intent("consumer", None, version=(2, "cli-a"), origin="cli-a")
    net.run(0.1)
    endpoint = net.nodes["consumer"]
    assert not endpoint.online
    assert endpoint.stored.pairing == first
    # Isolate external anti-entropy to inspect only durable restart state.
    net.partition([["consumer"], ["producer", "cli-a"]])
    net.restart("consumer", wall_time=WALL_TIME - 60)
    assert endpoint.node.pairing == first
    assert endpoint.node.node_state is NodeState.PENDING


def test_wrong_key_unpair_cannot_change_retained_pairing() -> None:
    net = _pair_net()
    expected = Pairing("producer", (1, "cli-a"))
    net.intent("consumer", "producer", version=expected.version, origin="cli-a")
    net.run(2)
    forged = encode(
        FrameType.INT,
        "attacker",
        int(WALL_TIME) * 1_000_000_000,
        WALL_TIME,
        {"op": "unpair", "consumer": "consumer", "version": [9, "attacker"]},
        b"wrong-key",
    )
    net.inject(forged, sender="attacker")
    net.run(3)
    net.assert_agreement("consumer", expected)
    assert net.nodes["consumer"].driver.attached_to == "producer"
    assert "rx-drop" in net.events("consumer")


def test_unpair_and_unknown_replacement_quiesce_driver() -> None:
    net = _pair_net()
    net.intent("consumer", "producer", version=(1, "cli-a"), origin="cli-a")
    net.run(2)
    net.intent("consumer", "unknown", version=(2, "cli-a"), origin="cli-a")
    net.run(2)
    endpoint = net.nodes["consumer"]
    assert endpoint.node.pairing == Pairing("unknown", (2, "cli-a"))
    assert endpoint.node.node_state is NodeState.PENDING
    assert endpoint.driver.attached_to is None
    net.intent("consumer", None, version=(3, "cli-a"), origin="cli-a")
    net.run(2)
    net.assert_agreement("consumer", Pairing(None, (3, "cli-a")))
    assert endpoint.node.node_state is NodeState.IDLE


def test_duplicate_nodes_and_overlapping_partitions_are_rejected() -> None:
    net = _pair_net()
    with pytest.raises(ValueError, match="duplicate node"):
        net.add_node("producer", Role.PRODUCER)
    with pytest.raises(ValueError, match="two groups"):
        net.partition([["consumer"], ["consumer", "producer"]])


def test_virtual_clock_and_event_budget_are_bounded() -> None:
    net = _pair_net()
    with pytest.raises(ValueError, match="backwards"):
        net.run_until(1)
    with pytest.raises(ValueError, match="finite"):
        net.run(float("inf"))
    with pytest.raises(SimulationError, match="event budget"):
        net.run(100, max_steps=1)


def test_observers_do_not_change_fault_draws_or_transmissions() -> None:
    def drive(observed: bool) -> Simulation:
        net = Simulation(seed=51)
        if observed:
            net.add_observer()
        net.add_node("producer", Role.PRODUCER)
        net.add_node("consumer", Role.CONSUMER)
        net.run(3)
        net.set_faults(FaultPolicy(drop=0.2, dup=0.5, jitter=0.05, reorder=0.5))
        for counter in range(1, 8):
            net.intent(
                "consumer", "producer", version=(counter, "cli-a"), origin="cli-a"
            )
            net.run(0.5)
        net.clear_faults()
        net.run(8)
        return net

    baseline, observed = drive(False), drive(True)
    assert baseline.snapshot() == observed.snapshot()
    assert baseline.transcript == observed.transcript
    assert baseline.counters.dropped > 0
    assert baseline.counters.duplicated > 0
    assert baseline.counters.reordered > 0


@pytest.mark.parametrize("sequence_delta", [-1, 0, 1])
def test_observer_and_node_share_future_sequence_boundary(sequence_delta: int) -> None:
    net = _pair_net()
    observer = net.add_observer()
    ceiling = (int(net.wall_time + net.now) + SEQ_FUTURE_MARGIN_S) * NS_PER_S
    raw = encode(
        FrameType.INT,
        "probe-cli",
        ceiling + sequence_delta,
        net.wall_time + net.now,
        {
            "op": "pair",
            "consumer": "consumer",
            "producer": "producer",
            "version": [1, "probe-cli"],
        },
        net.psk,
    )
    net.inject(raw, sender="probe-cli")
    net.run(0.1)
    if sequence_delta <= 0:
        assert observer.counts["stale"] == 0
        assert observer.intents["consumer"].version == (1, "probe-cli")
        assert net.nodes["consumer"].node.pairing == Pairing(
            "producer", (1, "probe-cli")
        )
    else:
        assert observer.counts["stale"] == 1
        assert "consumer" not in observer.intents
        assert net.nodes["consumer"].node.pairing == Pairing(None, (0, ""))
        # A rejected future sequence must not poison the replay watermark.
        net.intent("consumer", "producer", version=(1, "probe-cli"), origin="probe-cli")
        net.run(5)
        net.assert_agreement("consumer", Pairing("producer", (1, "probe-cli")))
        assert observer.nodes["consumer"].pairing_producer == "producer"
        assert observer.intents["consumer"].version == (1, "probe-cli")
        drops = [
            entry.frame.body["detail"]
            for entry in net.transcript
            if entry.sender == "consumer"
            and entry.frame is not None
            and entry.frame.type is FrameType.EVT
            and entry.frame.body.get("event") == "rx-drop"
        ]
        assert drops[-1]["stale"] == 1


def test_observer_attached_later_uses_current_clock_without_changing_execution() -> (
    None
):
    baseline, observed = _pair_net(), _pair_net()
    # Both clock origins advance before attachment; future-bound admission
    # must still use Unix time rather than the small virtual timer value.
    baseline.run(10)
    observed.run(10)
    observer = observed.add_observer()
    for net in (baseline, observed):
        net.intent("consumer", "producer", version=(1, "cli-a"), origin="cli-a")
        net.run(5)
    assert baseline.snapshot() == observed.snapshot()
    assert baseline.transcript == observed.transcript
    assert observer.nodes["consumer"].pairing_producer == "producer"
    assert observer.counts["stale"] == 0


def test_recording_can_be_disabled_without_changing_execution() -> None:
    def drive(record: bool) -> Simulation:
        net = Simulation(seed=4, record=record)
        net.add_node("producer", Role.PRODUCER)
        net.add_node("consumer", Role.CONSUMER)
        net.run(3)
        net.intent("consumer", "producer", version=(1, "cli-a"), origin="cli-a")
        net.run(5)
        return net

    recorded, unrecorded = drive(True), drive(False)
    assert recorded.transcript
    assert unrecorded.transcript == []
    assert recorded.snapshot() == unrecorded.snapshot()


def test_benchmark_repeats_validate_work_and_report_environment() -> None:
    root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [
            sys.executable,
            str(root / "benchmarks/control.py"),
            "--nodes",
            "4",
            "--intents",
            "20",
            "--repeat",
            "2",
            "--observers",
            "1",
        ],
        cwd=root,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    )
    report = json.loads(completed.stdout)
    assert report["benchmark"] == "control"
    assert report["environment"]["python"]
    assert report["workload"]["intents"] == 20
    assert report["result"]["counters"]["deliveries"] > 20
    assert len(report["timing"]["wall_seconds"]) == 2
    assert len(report["timing"]["cpu_seconds"]) == 2
    assert report["result"]["nodes"]["con-0"]["pairing"] == {
        "producer": "prod-b",
        "version": [10, "bench-cli"],
    }
