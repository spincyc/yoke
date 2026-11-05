"""Differential tests: the C core's pure functions against the Python reference.

ADR 0027: the Python implementation is the executable oracle, and a
divergence is a bug (or a recorded decision). ``probe.c`` exposes the USB/IP
codec, the descriptor builder, control routing, IN coalescing and CBW
parsing over a line protocol; the same inputs go to ``yoke.usb.usbip``,
``yoke.usb.descriptors`` and ``yoke.usb.policy``. Inputs are the usbsim
device models plus seeded random and corrupted variants.
"""

from __future__ import annotations

import random
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from conftest import HERE, build, library_sources
from tests.test_usb_descriptors import DUPLICATE_ENDPOINTS
from tests.usbsim import build as descriptors
from tests.usbsim.device import DeviceModel
from tests.usbsim.devices import (
    ALL_DEVICES,
    HubDevice,
    IsoAudioDevice,
    ManyEndpointsDevice,
    SuperSpeedDevice,
)

from yoke.usb import usbip
from yoke.usb.ch9 import Setup, Speed
from yoke.usb.descriptors import (
    DescriptorError,
    Presentation,
    UdcBudget,
    UnsupportedError,
    build_presentation,
)
from yoke.usb.policy import (
    Ack,
    ControlState,
    EndpointHalt,
    Forward,
    ProxyPolicy,
    Reply,
    SetConfiguration,
    SetInterface,
    Stall,
    decide,
    in_write_plan,
    parse_cbw,
)

DEVICES: dict[str, Callable[[], DeviceModel]] = {
    **ALL_DEVICES,
    "hub": HubDevice,
    "iso": IsoAudioDevice,
    "many": ManyEndpointsDevice,
    "super": SuperSpeedDevice,
}


class Probe:
    def __init__(self, binary: Path) -> None:
        self.process = subprocess.Popen(
            [str(binary)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

    def send(self, line: str) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(line + "\n")
        self.process.stdin.flush()

    def line(self) -> str:
        assert self.process.stdout is not None
        text = self.process.stdout.readline()
        assert text, "probe exited"
        return text.rstrip("\n")

    def ask(self, line: str) -> str:
        self.send(line)
        return self.line()

    def close(self) -> None:
        assert self.process.stdin is not None
        self.process.stdin.close()
        assert self.process.wait(10) == 0


@pytest.fixture(scope="module")
def probe() -> Iterator[Probe]:
    binary = build("probe", [HERE / "probe.c", *library_sources()])
    probe = Probe(binary)
    yield probe
    probe.close()


# --- USB/IP codec -----------------------------------------------------------


def random_header(rng: random.Random) -> bytes:
    command = rng.choice([1, 2, 3, 4, 1, 3, 5, 0, 0x59580001])
    words = [command] + [rng.getrandbits(32) for _ in range(11)]
    if rng.random() < 0.7:
        words[3] = rng.choice([0, 1, 2])
        words[4] = rng.randrange(18)
        words[6] = rng.choice([0, 8, 512, 65536, usbip.MAX_TRANSFER, 0x80000000])
    return b"".join(w.to_bytes(4, "big") for w in words)


def test_usbip_headers_decode_like_the_reference(probe: Probe) -> None:
    rng = random.Random(27)
    for _ in range(3000):
        raw = random_header(rng)
        answer = probe.ask(f"hdr {raw.hex()}")
        try:
            message = usbip.decode_header(raw)
        except usbip.ProtocolError:
            assert answer == "err", raw.hex()
            continue
        fields = answer.split()
        assert fields[0] == "ok", raw.hex()
        assert int(fields[2]) == message.seqnum
        if isinstance(message, usbip.CmdSubmit):
            assert int(fields[6]) == message.transfer_flags
            assert int(fields[7]) == message.transfer_buffer_length
            assert int(fields[10]) == message.interval
            assert bytes.fromhex(fields[11]) == message.setup
            assert fields[-1] == message.pack().hex()
        elif isinstance(message, usbip.RetSubmit):
            assert int(fields[12]) == message.status
            assert int(fields[13]) == message.actual_length
        elif isinstance(message, usbip.CmdUnlink):
            assert int(fields[15]) == message.unlink_seqnum
        else:
            assert int(fields[12]) == message.status


def test_device_records_decode_like_the_reference(probe: Probe) -> None:
    rng = random.Random(28)
    good = usbip.DeviceInfo(
        "/sys/devices/x/1-1", "1-1", 1, 2, 3, 0x1209, 0x5EED, 0x100, 0, 0, 0, 1, 1, 1
    ).pack()
    for case in range(800):
        raw = bytearray(good)
        for _ in range(case % 6):
            raw[rng.randrange(len(raw))] = rng.randrange(256)
        answer = probe.ask(f"devinfo {bytes(raw).hex()}")
        try:
            info = usbip.DeviceInfo.unpack(bytes(raw))
        except usbip.ProtocolError:
            assert answer == "err"
            continue
        fields = answer.split(" ")
        assert fields[0] == "ok"
        assert (
            blob(fields[1]) == info.path.encode()
            and blob(fields[2]) == info.busid.encode()
        )
        assert [int(f) for f in fields[3:]] == [
            info.busnum,
            info.devnum,
            info.speed,
            info.vendor,
            info.product,
            info.bcd_device,
            info.device_class,
            info.device_subclass,
            info.device_protocol,
            info.configuration_value,
            info.num_configurations,
            info.num_interfaces,
        ]


# --- descriptor builder -----------------------------------------------------

Fetch = Callable[[Setup], "bytes | None"]


def answers(device: DeviceModel, corrupt: random.Random | None = None) -> Fetch:
    """The device's GET_DESCRIPTOR answers as the stub returns them."""

    def fetch(setup: Setup) -> bytes | None:
        data = device.control(setup, b"")
        if data is None:
            return None
        data = bytearray(data[: setup.length])
        if corrupt is not None and data and corrupt.random() < 0.15:
            data[corrupt.randrange(len(data))] = corrupt.randrange(256)
            if corrupt.random() < 0.3:
                del data[corrupt.randrange(len(data)) :]
        return bytes(data)

    return fetch


def python_build(
    fetch: Fetch, speed: int, budget: UdcBudget
) -> tuple[list[bytes], Presentation | BaseException]:
    requests: list[bytes] = []

    def recording(setup: Setup) -> bytes | None:
        requests.append(setup.pack())
        return fetch(setup)

    try:
        return requests, build_presentation(
            recording, speed, power_ma=500, budget=budget
        )
    except (DescriptorError, UnsupportedError) as exc:
        return requests, exc


def c_build(
    probe: Probe, fetch: Fetch, speed: int, budget: UdcBudget
) -> tuple[list[bytes], list[str]]:
    requests: list[bytes] = []
    probe.send(
        f"build {speed} 500 64 {budget.max_in} {budget.max_out} {budget.max_number} 0"
    )
    while True:
        line = probe.line()
        if not line.startswith("req "):
            break
        raw = bytes.fromhex(line[4:])
        requests.append(raw)
        data = fetch(Setup.unpack(raw))
        probe.send("stall" if data is None else f"ans {data.hex()}")
    result = [line]
    if line == "done":
        while (line := probe.line()) != "end":
            result.append(line)
    return requests, result


def blob(text: str) -> bytes | None:
    return None if text == "-" else b"" if text == "=" else bytes.fromhex(text)


def assert_same(python: Presentation | BaseException, c: list[str], label: str) -> None:
    if isinstance(python, BaseException):
        code = -4 if isinstance(python, UnsupportedError) else -3
        assert c[0].startswith(f"fail {code} "), (label, python, c[0])
        assert c[0].split(" ", 2)[2] == str(python), label
        return
    assert c[0] == "done", (label, c[0])
    lines = c[1:]
    speed = lines.pop(0).split()
    assert [int(speed[2]), int(speed[3])] == [python.remote_speed, python.speed]
    configs: list[bytes] = []
    strings: dict[tuple[int, int], bytes | None] = {}
    classes: dict[tuple[int, int, int], bytes] = {}
    simple: dict[str, bytes | None] = {}
    for line in lines:
        kind, *rest = line.split(" ")
        if kind == "config":
            configs.append(blob(rest[0]) or b"")
        elif kind == "string":
            strings[(int(rest[0]), int(rest[1]))] = blob(rest[2])
        elif kind == "class":
            classes[(int(rest[0]), int(rest[1]), int(rest[2]))] = blob(rest[3]) or b""
        else:
            simple[kind] = blob(rest[0])
    assert simple["device"] == python.device_raw, label
    assert configs == list(python.config_raw), label
    assert simple["qualifier"] == python.qualifier, label
    assert simple["other"] == python.other_speed, label
    assert simple["bos"] == python.bos, label
    assert strings == python.strings, label
    assert classes == python.class_descriptors, label


@pytest.mark.parametrize("name", sorted(DEVICES))
def test_builder_matches_reference_requests_and_presentation(
    probe: Probe, name: str
) -> None:
    device = DEVICES[name]()
    budget = UdcBudget()
    python_requests, python = python_build(answers(device), device.speed, budget)
    c_requests, c = c_build(probe, answers(device), device.speed, budget)
    assert c_requests == python_requests
    assert_same(python, c, name)


def test_builder_agrees_on_corrupted_and_bounded_answers(probe: Probe) -> None:
    compared = 0
    for seed in range(400):
        names = sorted(ALL_DEVICES)
        name = names[seed % len(names)]
        budget = UdcBudget(
            max_in=1 + seed % 7, max_out=1 + seed % 5, max_number=4 + seed % 12
        )
        fetch_py = answers(ALL_DEVICES[name](), random.Random(seed))
        fetch_c = answers(ALL_DEVICES[name](), random.Random(seed))
        speed = [Speed.LOW, Speed.FULL, Speed.HIGH, Speed.SUPER][seed % 4]
        try:
            python_requests, python = python_build(fetch_py, speed, budget)
        except (IndexError, ValueError):
            continue  # a reference crash on malformed input (e.g. empty LANGID table)
        c_requests, c = c_build(probe, fetch_c, speed, budget)
        assert c_requests == python_requests, (seed, name)
        assert_same(python, c, f"{name}/{seed}")
        compared += 1
    assert compared > 300


def fixed_answers(device: bytes, config: bytes) -> Fetch:
    """A device that has exactly one configuration and nothing else."""

    def fetch(setup: Setup) -> bytes | None:
        kind, index = setup.value >> 8, setup.value & 0xFF
        if setup.request == 6 and kind == 1:
            return device[: setup.length]
        if setup.request == 6 and kind == 2 and index == 0:
            return config[: setup.length]
        return None

    return fetch


REUSED_BY_ALTERNATES = (
    descriptors.interface(0, endpoints=1, cls=0xFF)
    + descriptors.endpoint(0x81, 2, 64)
    + descriptors.interface(1, endpoints=0, cls=0xFF)
    + descriptors.interface(0, alternate=1, endpoints=2, cls=0xFF)
    + descriptors.endpoint(0x81, 2, 512)
    + descriptors.endpoint(0x02, 2, 512),
    2,
    "",
)


@pytest.mark.parametrize("case", [*sorted(DUPLICATE_ENDPOINTS), "alternates"])
def test_duplicate_endpoints_are_judged_like_the_reference(
    probe: Probe, case: str
) -> None:
    body, interfaces, message = (
        REUSED_BY_ALTERNATES if case == "alternates" else DUPLICATE_ENDPOINTS[case]
    )
    device = descriptors.device(vendor=0x1209, product=0x77, bcd_usb=0x0200)
    config = descriptors.configuration(body, interfaces=interfaces)
    fetch = fixed_answers(device, config)
    budget = UdcBudget()
    python_requests, python = python_build(fetch, Speed.HIGH, budget)
    c_requests, c = c_build(probe, fetch, Speed.HIGH, budget)
    assert c_requests == python_requests
    assert_same(python, c, case)
    if message:
        assert isinstance(python, DescriptorError) and str(python) == message
    else:
        assert isinstance(python, Presentation)


# --- control routing ---------------------------------------------------------


def built(probe: Probe, name: str) -> Presentation:
    device = ALL_DEVICES[name]()
    budget = UdcBudget()
    _, python = python_build(answers(device), device.speed, budget)
    _, c = c_build(probe, answers(device), device.speed, budget)
    assert isinstance(python, Presentation) and c[0] == "done"
    return python


def setups(rng: random.Random, presentation: Presentation) -> Iterator[Setup]:
    addresses = [0, 0x80, 0x81, 0x02, 0x83, 0x8F] + [
        e.address for c in presentation.configurations for e in c.all_endpoints()
    ]
    for _ in range(500):
        recipient = rng.randrange(4)
        request = rng.randrange(14)
        direction = rng.choice([0, 0x80])
        kind = rng.choice([0, 0, 0, 0x20, 0x40])
        value = rng.choice([0, 1, 2, rng.randrange(65536)])
        index = rng.choice(
            [0, 1, 2, rng.choice(addresses), 0x0409, rng.randrange(65536)]
        )
        if request == 6:
            dtype = rng.choice([1, 2, 3, 6, 7, 15, 0x22, 0x21, 9, 10])
            value = dtype << 8 | rng.choice([0, 0, 1, 2, 3, 0xEE])
        length = rng.choice([0, 1, 2, 9, 18, 64, 255, 4096])
        yield Setup(direction | kind | recipient, request, value, index, length)


@pytest.mark.parametrize("name", ["composite", "keyboard", "mass_storage", "stalling"])
def test_decide_matches_the_reference(probe: Probe, name: str) -> None:
    presentation = built(probe, name)
    rng = random.Random(name)
    configs = [0] + [c.value for c in presentation.configurations] + [7]
    for setup in setups(rng, presentation):
        configuration = rng.choice(configs)
        config = presentation.configuration(configuration)
        alternates = (
            {n: rng.choice(sorted(alts)) for n, alts in config.interfaces.items()}
            if config is not None
            else {}
        )
        cache = rng.random() < 0.9
        state = ControlState(configuration=configuration, alternates=alternates)
        expected = decide(
            setup, presentation, state, ProxyPolicy(descriptor_cache=cache)
        )
        mask = sum(1 << n for n in alternates if n < 32)
        alts = bytes(alternates.get(n, 0) for n in range(32)).hex()
        line = probe.ask(
            f"decide {configuration} {mask} {alts} 0 {int(cache)} {setup.pack().hex()}"
        )
        reason = probe.line()
        kind, data, _r, value, iface, alt, addr, halt, fwd_cache = line.split()
        label = f"{name} {setup} cfg={configuration} cache={cache} -> {expected}"
        if isinstance(expected, Reply):
            assert kind == "reply" and blob(data) == expected.data, label
        elif isinstance(expected, Ack):
            assert kind == "ack", label
        elif isinstance(expected, Stall):
            assert kind == "stall" and reason == f"reason {expected.reason}", label
        elif isinstance(expected, Forward):
            assert kind == "forward" and int(fwd_cache) == expected.cache, label
        elif isinstance(expected, SetConfiguration):
            assert kind == "setconf" and int(value) == expected.value, label
        elif isinstance(expected, SetInterface):
            assert kind == "setif", label
            assert (int(iface), int(alt)) == (expected.interface, expected.alternate)
        elif isinstance(expected, EndpointHalt):
            assert kind == "halt", label
            assert (int(addr), bool(int(halt))) == (expected.address, expected.halt)


# --- IN coalescing and BOT --------------------------------------------------


def test_in_write_plan_matches_the_reference(probe: Probe) -> None:
    rng = random.Random(29)
    for _ in range(3000):
        packet = rng.choice([8, 64, 512, 1024])
        limit = rng.choice([512, 4096, 65536])
        results = []
        for _ in range(rng.randrange(1, 12)):
            requested = packet * rng.randrange(1, 5)
            actual = requested if rng.random() < 0.7 else rng.randrange(requested + 1)
            results.append((actual, requested))
        pairs = " ".join(f"{a}:{r}" for a, r in results)
        count, total, zero = probe.ask(f"plan {packet} {limit} {pairs}").split()
        assert (int(count), int(total), bool(int(zero))) == in_write_plan(
            results, packet, limit
        ), (packet, limit, results)


def test_parse_cbw_matches_the_reference(probe: Probe) -> None:
    rng = random.Random(30)
    good = b"USBC" + bytes([1, 0, 0, 0, 0, 2, 0, 0, 0x80, 0, 10]) + bytes(16)
    for case in range(2000):
        raw = bytearray(good)
        for _ in range(case % 4):
            raw[rng.randrange(len(raw))] = rng.randrange(256)
        if case % 7 == 0:
            raw = raw[: rng.randrange(40)]
        answer = probe.ask(f"cbw {bytes(raw).hex()}")
        expected = parse_cbw(bytes(raw))
        if expected is None:
            assert answer == "none"
        else:
            assert answer == f"ok {expected[0]} {int(expected[1])}"


def halt_mask(addresses: set[int]) -> int:
    """yusb_control_state_t.halted: bit (number + 16 * is_in)."""
    return sum(1 << ((a & 0x0F) + (16 if a & 0x80 else 0)) for a in addresses)


@pytest.mark.parametrize("name", ["composite", "keyboard", "mass_storage", "stalling"])
def test_decide_with_halts_and_descriptor_misses_matches_the_reference(
    probe: Probe, name: str
) -> None:
    """Stateful routing: locally halted endpoints and remembered forwarded
    GET_DESCRIPTOR answers (data, short data, STALL) in both cores."""
    presentation = built(probe, name)
    rng = random.Random(f"{name}-stateful")
    configs = [c.value for c in presentation.configurations]
    keys = [
        (t << 8 | i, x)
        for t in (3, 0x21, 0x22, 15, 6, 9)
        for i in (0, 1, 5, 0xEE)
        for x in (0, 0x0409)
    ]
    compared = 0
    for _ in range(60):
        assert probe.ask("forget") == "ok"
        misses: dict[tuple[int, int], tuple[bytes | None, int]] = {}
        for _ in range(rng.randrange(1, 12)):
            value, index = rng.choice(keys)
            fetched = rng.choice([2, 4, 9, 18, 64, 255])
            stalled = rng.random() < 0.3
            data = None if stalled else rng.randbytes(rng.randrange(fetched + 1))
            setup = Setup(0x80, 6, value, index, fetched)
            answer = probe.ask(
                f"remember {setup.pack().hex()} {int(stalled)} "
                f"{(data or b'').hex() or '='}"
            )
            assert answer == "ok"
            misses[(value, index)] = (data, fetched)
        configuration = rng.choice(configs)
        config = presentation.configuration(configuration)
        assert config is not None
        alternates = {n: rng.choice(sorted(a)) for n, a in config.interfaces.items()}
        active = ControlState(
            configuration=configuration, alternates=alternates
        ).active_endpoints(presentation)
        addresses = sorted(active | {0x81, 0x02, 0x8F})
        halted = {a for a in addresses if rng.random() < 0.5 and a & 0x0F}
        state = ControlState(
            configuration=configuration,
            alternates=alternates,
            halted=halted,
            descriptor_misses=dict(misses),
        )
        mask = sum(1 << n for n in alternates if n < 32)
        alts = bytes(alternates.get(n, 0) for n in range(32)).hex()
        for _ in range(40):
            pick = rng.random()
            if pick < 0.4:
                value, index = rng.choice([*misses, *keys[:4]])
                length = rng.choice([1, 2, 4, 9, 18, 64, 255, 4096])
                setup = Setup(0x80, 6, value, index, length)
            elif pick < 0.8:
                setup = Setup(0x82, 0, 0, rng.choice(addresses), 2)
            else:
                setup = Setup(
                    rng.choice([0x00, 0x02]),
                    rng.choice([1, 3]),
                    0,
                    rng.choice(addresses),
                    0,
                )
            cache = rng.random() < 0.9
            expected = decide(
                setup, presentation, state, ProxyPolicy(descriptor_cache=cache)
            )
            line = probe.ask(
                f"decide {configuration} {mask} {alts} {halt_mask(halted)} "
                f"{int(cache)} {setup.pack().hex()}"
            )
            reason = probe.line()
            kind, data_hex, _r, _v, _i, _a, addr, halt, fwd_cache = line.split()
            label = f"{name} {setup} halted={halted} misses={misses} -> {expected}"
            if isinstance(expected, Reply):
                assert kind == "reply" and blob(data_hex) == expected.data, label
            elif isinstance(expected, Stall):
                assert kind == "stall" and reason == f"reason {expected.reason}", label
            elif isinstance(expected, Forward):
                assert kind == "forward" and int(fwd_cache) == expected.cache, label
            elif isinstance(expected, EndpointHalt):
                assert kind == "halt", label
                assert (int(addr), bool(int(halt))) == (expected.address, expected.halt)
            else:
                assert kind == "ack" and isinstance(expected, Ack), label
            compared += 1
    assert probe.ask("forget") == "ok"
    assert compared == 60 * 40
