"""iTach-subset IR endpoint behaviour over real localhost TCP (host simulation).

The unchanged yoke_ir core runs behind real sockets; RMT output is modelled
with the target encoder code and a virtual clock (see conftest.py). These
tests cannot establish GPIO, LED drive, carrier accuracy on silicon or W5500
behaviour; see doc/design/IR-ENDPOINT.md for the bench procedure.
"""

from __future__ import annotations

from typing import Any

import pytest
from conftest import Endpoint
from irwave import nec_us, segments, sendir, to_cycles

NEC_FRAME, NEC_REPEAT = nec_us(0x00, 0x12)
NEC_CYCLES = to_cycles(NEC_FRAME + NEC_REPEAT, 38000)
NEC_OFFSET = len(to_cycles(NEC_FRAME, 38000)) + 1


def nec(ident: int = 1, repeat: int = 1, connector: int = 1) -> str:
    return sendir(connector, ident, 38000, repeat, NEC_OFFSET, NEC_CYCLES)


def frame_us(cycles: list[int], carrier_hz: int = 38000) -> int:
    return round(sum(cycles) * 1e6 / carrier_hz)


FULL_US = frame_us(NEC_CYCLES)
# Edges are rounded on cumulative cycles, so the repeated part is the
# difference of two rounded edges.
REPEAT_US = FULL_US - frame_us(NEC_CYCLES[: NEC_OFFSET - 1])


def transmissions(endpoint: Endpoint, output: int = 0) -> list[dict[str, Any]]:
    return endpoint.waveform(output)["transmissions"]  # type: ignore[no-any-return]


def test_boot_self_test_passes(endpoint: Endpoint) -> None:
    assert endpoint.control("selftest") == "ok"


def test_identity_is_honest_and_never_an_itach(endpoint: Endpoint) -> None:
    client = endpoint.connect()
    client.send("getdevices")
    assert client.lines(3) == ["device,0,0 ETHERNET", "device,1,3 IR", "endlistdevices"]
    client.send("getversion")
    identity = client.line()
    assert identity.startswith("Make=Yoke;")
    for foreign in ("iTach", "GlobalCache", "Global Cache", "710-"):
        assert foreign not in identity
    client.send("getversion,0")
    client.send("getversion,1")
    assert client.lines(2) == [f"version,0,{identity}", f"version,1,{identity}"]
    client.send("getversion,2")
    assert client.line() == "ERR_0:0,002"


def test_completeir_only_after_physical_completion(endpoint: Endpoint) -> None:
    client = endpoint.connect()
    client.send(nec(4444))
    endpoint.sync()
    assert client.pending() == b""
    start = transmissions(endpoint)[0]["start"]
    endpoint.advance(FULL_US - 1)
    assert client.pending() == b""
    endpoint.advance(1)
    assert client.line() == "completeir,1:1,4444"
    (done,) = transmissions(endpoint)
    assert done["end"] == start + FULL_US
    assert done["frames"] == 1 and not done["stopped"]


def run_to_idle(endpoint: Endpoint, step: int = 50_000, limit: int = 400) -> None:
    for _ in range(limit):
        if endpoint.counters()["idle"]:
            return
        endpoint.advance(step)
    raise AssertionError("transmission never completed")


def envelope(endpoint: Endpoint, index: int, output: int = 0) -> list[tuple[int, int]]:
    wave = endpoint.waveform(output)
    trans = wave["transmissions"][index]
    runs = segments(wave["halves"], trans["start"], trans["end"])
    return [(run.level, run.ticks) for run in runs]


def test_specification_examples(endpoint: Endpoint) -> None:
    """Worked examples from the Global Cache iTach API v1.5, section 5.4."""
    client = endpoint.connect()
    client.send("sendir,1:2,2445,40000,1,1,4,5,6,5")
    run_to_idle(endpoint)
    assert client.line() == "completeir,1:2,2445"
    # Compressed pairs (A = 4,5; B = 8,9) expand to the identical timeline.
    client.send("sendir,1:2,2445,40000,1,1,4,5,4,5,8,9,4,5,8,9,8,9")
    run_to_idle(endpoint)
    client.send("sendir,1:2,2445,40000,1,1,4,5A8,9ABB")
    run_to_idle(endpoint)
    assert client.lines(2) == ["completeir,1:2,2445"] * 2
    assert envelope(endpoint, 1, 1) == envelope(endpoint, 2, 1)
    # Preamble once, then <offset> repeats: same pulses as the long form.
    long_form = "34,48" + ",24,12,24,960" * 4
    client.send(f"sendir,1:2,4444,34500,1,1,{long_form}")
    run_to_idle(endpoint)
    client.send("sendir,1:2,34,34500,4,3,34,48,24,12,24,960")
    run_to_idle(endpoint)
    assert client.lines(2) == ["completeir,1:2,4444", "completeir,1:2,34"]
    # Equal within one tick per edge: the repeated part reuses the rounding
    # of its first occurrence instead of re-rounding each copy.
    long_runs, offset_runs = envelope(endpoint, 3, 1), envelope(endpoint, 4, 1)
    assert [level for level, _ in long_runs] == [level for level, _ in offset_runs]
    pairs = zip(long_runs, offset_runs, strict=True)
    assert all(abs(a - b) <= 1 for (_, a), (_, b) in pairs)
    assert abs(sum(a for _, a in long_runs) - sum(b for _, b in offset_runs)) <= 3
    for line, error in (
        ("sendir,5:3,3456,23400,1,1,24,48,24,960", "ERR_0:0,002"),
        ("sendir,1:2,23333,40000,2,3,24,48,24,48,960", "ERR_1:2,010"),
        ("sendir,1:3,0,40000,2,2,24,48,24,960", "ERR_1:3,007"),
    ):
        client.send(line)
        assert client.line() == error


OK_TAIL = "38000,1,1,24,24"
MALFORMED = [
    ("foo", "ERR_01"),
    ("GETDEVICES", "ERR_01"),
    ("getdevices,1", "ERR_01"),
    ("sendir", "ERR_01"),
    ("sendir,", "ERR_0:0,017"),
    ("sendir,1:1", "ERR_0:0,017"),
    ("sendir,11", "ERR_0:0,017"),
    (f"sendir,0:1,1,{OK_TAIL}", "ERR_0:0,002"),
    (f"sendir,2:1,1,{OK_TAIL}", "ERR_0:0,002"),
    (f"sendir,1:4,1,{OK_TAIL}", "ERR_0:0,003"),
    (f"sendir,1:0,1,{OK_TAIL}", "ERR_0:0,003"),
    (f"sendir,1:1,65536,{OK_TAIL}", "ERR_1:1,004"),
    (f"sendir,1:1,x,{OK_TAIL}", "ERR_1:1,004"),
    ("sendir,1:1,1,14999,1,1,24,24", "ERR_1:1,005"),
    ("sendir,1:1,1,500001,1,1,24,24", "ERR_1:1,005"),
    ("sendir,1:1,1,38k,1,1,24,24", "ERR_1:1,005"),
    ("sendir,1:1,1,38000,0,1,24,24", "ERR_1:1,006"),
    ("sendir,1:1,1,38000,51,1,24,24", "ERR_1:1,012"),
    ("sendir,1:1,1,38000,1,0,24,24", "ERR_1:1,007"),
    ("sendir,1:1,1,38000,1,2,24,24", "ERR_1:1,007"),
    ("sendir,1:1,1,38000,1,385,24,24", "ERR_1:1,007"),
    ("sendir,1:1,1,38000,1,3,24,24", "ERR_1:1,007"),
    ("sendir,1:1,1,38000,1,1,", "ERR_1:1,008"),
    ("sendir,1:1,1,38000,1,1,24", "ERR_1:1,010"),
    ("sendir,1:1,1,38000,1,1,24,0", "ERR_1:1,009"),
    ("sendir,1:1,1,38000,1,1,24,65536", "ERR_1:1,009"),
    ("sendir,1:1,1,38000,1,1,24,,24", "ERR_1:1,009"),
    ("sendir,1:1,1,38000,1,1,24,24,", "ERR_1:1,009"),
    ("sendir,1:1,1,38000,1,1,24,x", "ERR_1:1,009"),
    ("sendir,1:1,1,38000,1,1,24,-24", "ERR_1:1,009"),
    ("sendir,1:1,1,15000,1,1,65535,65535", "ERR_1:1,009"),  # frame > 2 s bound
    ("sendir,1:1,1,38000,1,1," + ",".join(["24"] * 522), "ERR_1:1,020"),
    ("sendir,1:1,1,38000,1,1,A", "ERR_1:1,022"),
    ("sendir,1:1,1,38000,1,1,24A", "ERR_1:1,021"),
    ("sendir,1:1,1,38000,1,1,24,24B", "ERR_1:1,022"),
    ("stopir", "ERR_01"),
    ("stopir,1:1,5", "ERR_0:0,017"),
    ("stopir,1:4", "ERR_0:0,003"),
    ("stopir,2:1", "ERR_0:0,002"),
    ("getversion,x", "ERR_0:0,002"),
    ("\x00\xff\x01getversion", "ERR_01"),
]


@pytest.mark.parametrize(("line", "expected"), MALFORMED)
def test_malformed_input_is_rejected_with_spec_codes(
    endpoint: Endpoint, line: str, expected: str
) -> None:
    client = endpoint.connect()
    client.send(line)
    assert client.line() == expected
    assert endpoint.counters()["idle"]
    client.send("getdevices")  # the connection stays usable
    assert client.lines(3)[2] == "endlistdevices"


def test_line_framing_bounds_and_terminators(endpoint: Endpoint) -> None:
    client = endpoint.connect()
    client.send("getversion", b"\r\n")
    assert client.line().startswith("Make=Yoke")
    client.send_raw(b"\r\n\r\n\n\r")  # empty lines are ignored
    client.send_raw(b"x" * 4095 + b"\r")  # fits exactly: parsed, then rejected
    assert client.line() == "ERR_01"
    client.send_raw(b"y" * 5000)  # no CR before the buffer is full
    assert client.line() == "ERR_15"
    client.send_raw(b"z" * 300 + b"\rgetversion\r")  # rest of the oversize line
    assert client.line().startswith("Make=Yoke")
    # The largest frame-valid iTach line (zero-padded 520 durations) fits.
    padded = ",".join(["00020"] * 520)
    client.send(f"sendir,1:1,65535,500000,50,383,{padded}")
    run_to_idle(endpoint)
    assert client.line() == "completeir,1:1,65535"


def test_partial_line_times_out(endpoint: Endpoint) -> None:
    client = endpoint.connect()
    client.send_raw(b"getver")
    endpoint.advance(9_999_000)
    assert client.pending() == b""
    endpoint.advance(1_000)
    assert client.line() == "ERR_11"
    client.send("getversion")
    assert client.line().startswith("Make=Yoke")


def test_busy_is_per_connector_and_per_connection(endpoint: Endpoint) -> None:
    first, second = endpoint.connect(), endpoint.connect()
    first.send(nec(1, repeat=3))
    second.send(nec(2))
    assert second.line() == "busyIR,1:1,2"
    second.send(nec(3, connector=2))  # independent connector
    run_to_idle(endpoint)
    assert first.line() == "completeir,1:1,1"
    assert second.line() == "completeir,1:2,3"
    assert endpoint.counters()["busy"] == 1
    second.send(nec(4))  # free again
    run_to_idle(endpoint)
    assert second.line() == "completeir,1:1,4"


def test_stopir_halts_and_notifies_both_connections(endpoint: Endpoint) -> None:
    owner, other = endpoint.connect(), endpoint.connect()
    owner.send(nec(9, repeat=10))
    endpoint.advance(30_000)
    other.send("stopir,1:1")
    assert other.line() == "stopir,1:1"
    assert owner.line() == "stopir,1:1"
    (trans,) = transmissions(endpoint)
    assert trans["stopped"] and trans["end"] == 30_000
    wave = endpoint.waveform()
    assert sum(h[2] for h in wave["halves"]) == 30_000  # cut at the stop instant
    run_to_idle(endpoint)
    endpoint.advance(1_000_000)
    assert owner.pending() == b""  # never a completeir after stopir
    other.send("stopir,1:1")  # idle connector: always answered
    assert other.line() == "stopir,1:1"
    assert endpoint.counters()["stopped"] == 1


def frame_starts(endpoint: Endpoint, index: int = 0) -> int:
    return int(transmissions(endpoint)[index]["frames"])


def test_smooth_repeat_resets_the_count(endpoint: Endpoint) -> None:
    client = endpoint.connect()
    client.send(nec(5, repeat=3))
    endpoint.advance(30_000)  # inside the first frame's data
    client.send(nec(5, repeat=3))
    endpoint.sync()
    assert client.pending() == b""  # folded: acknowledged by one completeir
    run_to_idle(endpoint)
    assert client.line() == "completeir,1:1,5"
    assert client.pending() == b""
    (trans,) = transmissions(endpoint)
    assert trans["frames"] == 1 + 3
    assert trans["end"] - trans["start"] == FULL_US + 3 * REPEAT_US
    assert endpoint.counters()["folded"] == 1


def test_identical_command_after_the_last_decision_is_a_new_command(
    endpoint: Endpoint,
) -> None:
    client = endpoint.connect()
    client.send(nec(6, repeat=1))
    endpoint.advance(FULL_US - 2_000)  # decision already taken in the final gap
    client.send(nec(6, repeat=1))
    run_to_idle(endpoint)
    assert client.lines(2) == ["completeir,1:1,6"] * 2
    first, second = transmissions(endpoint)
    assert first["frames"] == second["frames"] == 1
    assert second["start"] == first["end"]


def test_held_key_folds_into_the_queued_command(endpoint: Endpoint) -> None:
    """After the last decision, repeats of a held key fold into the queued
    new transmission instead of being refused."""
    client = endpoint.connect()
    client.send(nec(8, repeat=1))
    endpoint.advance(FULL_US - 2_000)
    for _ in range(3):
        client.send(nec(8, repeat=1))
    endpoint.sync()
    assert client.pending() == b""  # no busyIR for the owner's own key
    run_to_idle(endpoint)
    assert client.lines(2) == ["completeir,1:1,8"] * 2
    assert len(transmissions(endpoint)) == 2
    assert endpoint.counters()["busy"] == 0


def test_smooth_repeat_stops_within_bound_after_release(endpoint: Endpoint) -> None:
    """Holding a key (resend every 60 ms) never overshoots repeat+1 frames."""
    client = endpoint.connect()
    repeat = 2
    client.send(nec(7, repeat=repeat))
    last = 0
    for _ in range(30):
        last = endpoint.advance(60_000)
        client.send(nec(7, repeat=repeat))
    endpoint.sync()
    run_to_idle(endpoint)
    wave = endpoint.waveform()
    trans = wave["transmissions"]
    assert all(t["frames"] >= 1 for t in trans)
    # Frame starts are where a 9 ms leader mark begins.
    starts = [
        run.start
        for t in trans
        for run in segments(wave["halves"], t["start"], t["end"])
        if run.level == 1 and run.ticks > 8_000
    ]
    after_release = [start for start in starts if start > last]
    assert 1 <= len(after_release) <= repeat + 1
    assert client.lines(len(trans)) == ["completeir,1:1,7"] * len(trans)


def test_new_command_from_owner_replaces_after_the_frame(endpoint: Endpoint) -> None:
    client = endpoint.connect()
    client.send(nec(10, repeat=20))
    endpoint.advance(20_000)
    client.send(nec(11, repeat=1))
    client.send(nec(12, repeat=1))  # queue depth is one
    assert client.line() == "busyIR,1:1,12"
    run_to_idle(endpoint)
    assert client.lines(2) == ["completeir,1:1,10", "completeir,1:1,11"]
    first, second = transmissions(endpoint)
    assert first["frames"] <= 2  # halted at a frame boundary, not after 20
    assert second["start"] == first["end"]


def test_disconnect_mid_send_finishes_bounded_and_frees(endpoint: Endpoint) -> None:
    owner, other = endpoint.connect(), endpoint.connect()
    owner.send(nec(20, repeat=3))
    endpoint.advance(10_000)
    owner.close()
    endpoint.sync()
    other.send(nec(21))
    assert other.line() == "busyIR,1:1,21"
    run_to_idle(endpoint)
    (trans,) = transmissions(endpoint)
    assert trans["frames"] == 3 and not trans["stopped"]
    assert endpoint.counters()["completeir"] == 0  # nobody left to tell
    other.send(nec(22))
    run_to_idle(endpoint)
    assert other.line() == "completeir,1:1,22"


def test_rate_limit_defers_lines_without_dropping(endpoint_factory: Any) -> None:
    endpoint = endpoint_factory("--rate", "5", "--burst", "3")
    client = endpoint.connect()
    for _ in range(10):
        client.send("getversion")
    endpoint.sync()
    assert client.pending().count(b"\r") == 3
    endpoint.advance(1_000_000)
    assert client.pending().count(b"\r") == 5
    endpoint.advance(1_000_000)
    assert client.pending().count(b"\r") == 2
    assert endpoint.counters()["rate_limited"] >= 1


def test_slow_reader_is_closed_without_blocking_others(endpoint_factory: Any) -> None:
    endpoint = endpoint_factory(
        "--sndbuf", "4096", "--rate", "100000", "--burst", "100000"
    )
    slow = endpoint.connect(rcvbuf=4096)
    fast = endpoint.connect()
    slow.sock.setblocking(False)
    sent = 0
    try:
        while sent < 400_000:
            sent += slow.sock.send(b"getdevices\r" * 100)
            endpoint.sync()
    except BlockingIOError:
        pass
    fast.send("getversion")
    assert fast.line().startswith("Make=Yoke")
    endpoint.advance(10_000_001)
    assert endpoint.counters()["closed_slow"] == 1
    assert endpoint.counters()["open"] == 1
    assert slow.closed_by_peer()
    fast.send("getversion")
    assert fast.line().startswith("Make=Yoke")


def test_connection_limit_and_idle_eviction(endpoint_factory: Any) -> None:
    endpoint = endpoint_factory("--conns", "2")
    first, second = endpoint.connect(), endpoint.connect()
    refused = endpoint.connect()
    assert refused.closed_by_peer()
    assert endpoint.counters()["refused"] == 1
    endpoint.advance(31_000_000)
    second.send("getversion")
    assert second.line().startswith("Make=Yoke")
    newcomer = endpoint.connect()  # evicts the longest-idle holder (first)
    assert first.closed_by_peer()
    newcomer.send("getversion")
    assert newcomer.line().startswith("Make=Yoke")
    assert endpoint.counters()["evicted"] == 1


def test_lan_scope_refuses_peers_outside_the_allowlist(endpoint_factory: Any) -> None:
    endpoint = endpoint_factory("--allow", "10.0.0.0/8")
    client = endpoint.connect()
    assert client.closed_by_peer()
    assert endpoint.counters()["accepted"] == 0


def test_quiesce_finishes_the_frame_and_refuses_new_work(endpoint: Endpoint) -> None:
    client = endpoint.connect()
    client.send(nec(30, repeat=20))
    endpoint.advance(20_000)
    endpoint.control("quiesce")
    client.send(nec(31, connector=2))
    assert client.line() == "busyIR,1:2,31"
    run_to_idle(endpoint)
    assert client.line() == "completeir,1:1,30"
    (trans,) = transmissions(endpoint)
    assert trans["frames"] <= 2 and not trans["stopped"]
    assert endpoint.counters()["idle"]


def test_learning_is_off_by_default(endpoint: Endpoint) -> None:
    client = endpoint.connect()
    client.send("get_IRL")
    client.send("stop_IRL")
    assert client.lines(2) == ["IR Learner Unavailable", "IR Learner Disabled"]
    assert not endpoint.counters()["learning"]
