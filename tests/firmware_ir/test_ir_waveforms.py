"""Exact pulse timelines from the host RMT model against reference protocols.

HOST SIMULATION: the waveform is what the target encoder code would write to
the RMT channel memory, with carrier ticks computed as ESP-IDF v5.5.6's
rmt_apply_carrier() does on an 80 MHz group clock. It assumes the carrier
restarts at each mark (bench item); it is not a scope capture.
"""

from __future__ import annotations

from typing import Any

import pytest
from conftest import Endpoint
from irwave import (
    carrier_actual,
    decode_nec,
    decode_rc5,
    decode_sirc,
    ideal_edges_us,
    nec_us,
    pronto_from_us,
    pronto_to_sendir,
    raw_capture,
    rc5_us,
    segments,
    sendir,
    sirc_us,
    to_cycles,
)

CARRIERS = [36000, 38000, 40000, 56000]


def build(protocol: str, carrier: int) -> tuple[str, int, list[int]]:
    """(sendir line, programmed carrier Hz, cycles)."""
    if protocol == "pronto-sirc":
        pronto = pronto_from_us(sirc_us(address=1, command=21), carrier)
        line, frequency = pronto_to_sendir(pronto, connector=1, ident=77)
        cycles = [int(value) for value in line.split(",")[6:]]
        return line, frequency, cycles
    durations = nec_us(0x5A, 0x3C)[0] if protocol == "nec" else rc5_us(5, 53, 1)
    cycles = to_cycles(durations, carrier)
    return sendir(1, 77, carrier, 1, 1, cycles), carrier, cycles


def transmit(endpoint: Endpoint, line: str) -> tuple[dict[str, Any], list[Any]]:
    client = endpoint.connect()
    sent_at = endpoint.now()
    client.send(line)
    for _ in range(100):
        if endpoint.counters()["idle"]:
            break
        endpoint.advance(20_000)
    assert client.line() == "completeir,1:1,77"
    wave = endpoint.waveform(0)
    trans = wave["transmissions"][-1]
    # Model command-to-first-edge latency: zero virtual time (CPU cost is
    # measured separately by the host bench).
    assert trans["start"] == sent_at
    return trans, segments(wave["halves"], trans["start"], trans["end"])


@pytest.mark.parametrize("carrier", CARRIERS)
@pytest.mark.parametrize("protocol", ["nec", "rc5", "pronto-sirc"])
def test_waveform_matches_reference(
    endpoint: Endpoint, protocol: str, carrier: int
) -> None:
    line, frequency, cycles = build(protocol, carrier)
    if protocol == "pronto-sirc":
        assert abs(frequency - carrier) / carrier < 0.005  # Pronto word quantization
    trans, runs = transmit(endpoint, line)
    # Envelope: alternating mark/space, one run per programmed duration.
    assert [run.level for run in runs] == [1 - i % 2 for i in range(len(cycles))]
    # Every edge within half an output tick of N/f (no cumulative drift).
    origin = trans["start"]
    for run, ideal in zip(runs, ideal_edges_us(cycles, frequency), strict=True):
        assert abs(run.start + run.ticks - origin - ideal) <= 0.5 + 1e-9
    assert trans["end"] - origin == round(sum(cycles) * 1e6 / frequency)
    # Carrier: ESP-IDF integer group-clock divider, 1/3 duty.
    actual = carrier_actual(trans["total"])
    assert abs(actual - frequency) / frequency < 0.0005
    assert trans["high"] + trans["low"] == trans["total"]
    assert abs(trans["high"] / trans["total"] - 0.33) < 0.002
    # Each mark carries its programmed number of carrier periods (+-1).
    for run, count in zip(runs[::2], cycles[::2], strict=False):
        assert abs(run.ticks * actual / 1e6 - count) <= 1
    durations = [run.ticks for run in runs]
    if protocol == "nec":
        assert decode_nec(durations) == (0x5A, 0x3C)
    elif protocol == "rc5":
        assert decode_rc5(durations) == (1, 5, 53)
    else:
        assert decode_sirc(durations) == (1, 21)


def learned_line(
    endpoint: Endpoint, client: Any, capture: list[tuple[int, int]]
) -> str:
    flat = ",".join(f"{level},{ticks}" for level, ticks in capture)
    assert endpoint.control(f"capture {flat}") == "ok"
    return str(client.line())


def test_learning_round_trip(endpoint_factory: Any) -> None:
    endpoint = endpoint_factory("--learning", "1", "--outputs", "2")
    learner, sender, other = endpoint.connect(), endpoint.connect(), endpoint.connect()
    learner.send("get_IRL")
    assert learner.line() == "IR Learner Enabled"
    other.send("get_IRL")
    assert other.line() == "IR Learner Unavailable"
    frame = nec_us(0x10, 0x20)[0]
    cycles = to_cycles(frame, 38000)
    trans, runs = transmit_on(endpoint, sender, sendir(1, 77, 38000, 1, 1, cycles))
    # A wideband learning sensor sees every carrier pulse.
    raw = raw_capture(runs, trans["total"], trans["high"])
    fields = learned_line(endpoint, learner, raw).split(",")
    assert fields[:2] == ["sendir", "1:1"] and fields[2:6] == ["0", fields[3], "1", "1"]
    assert abs(int(fields[3]) - carrier_actual(trans["total"])) < 0.002 * 38000
    learned = [int(value) for value in fields[6:]]
    assert len(learned) == len(cycles)
    assert all(abs(a - b) <= 1 for a, b in zip(learned[:-1], cycles[:-1], strict=True))
    assert learned[-1] == round(0.05 * int(fields[3]))  # lead-out is not observable
    # A demodulating receiver: envelope only, frequency assumed (38 kHz).
    envelope = [(run.level, run.ticks) for run in runs[:-1]]
    fields = learned_line(endpoint, learner, envelope).split(",")
    assert fields[3] == "38000"
    demodulated = [int(value) for value in fields[6:]]
    assert all(
        abs(a - b) <= 1 for a, b in zip(demodulated[:-1], cycles[:-1], strict=True)
    )
    # Any other command from the learner ends learning (iTach behaviour).
    learner.send("getversion")
    assert learner.line().startswith("Make=Yoke")
    assert not endpoint.counters()["learning"]
    other.send("get_IRL")
    assert other.line() == "IR Learner Enabled"
    endpoint.advance(60_000_000)
    assert other.line() == "IR Learner Disabled"  # bounded session
    assert endpoint.counters()["learned"] == 2


def transmit_on(
    endpoint: Endpoint, client: Any, line: str
) -> tuple[dict[str, Any], list[Any]]:
    client.send(line)
    for _ in range(100):
        if endpoint.counters()["idle"]:
            break
        endpoint.advance(20_000)
    assert client.line() == "completeir,1:1,77"
    wave = endpoint.waveform(0)
    trans = wave["transmissions"][-1]
    return trans, segments(wave["halves"], trans["start"], trans["end"])
