"""Pure descriptor parsing, topology checks, rewrites and control routing."""

from __future__ import annotations

import pytest

from tests.usbsim import build
from yoke.usb import ch9
from yoke.usb.ch9 import Setup, Speed, urb_interval
from yoke.usb.descriptors import (
    DescriptorError,
    UdcBudget,
    UnsupportedError,
    build_presentation,
    check_supported,
    parse_configuration,
    parse_device,
    rewrite_bos,
    rewrite_configuration,
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
    chunk_sizes,
    decide,
    in_write_plan,
    parse_cbw,
)

DEVICE = build.device(vendor=0x1209, product=0x77, bcd_usb=0x0201, max_packet0=8)
HID = build.hid(63)
BODY = (
    build.interface(0, endpoints=1, cls=3, subclass=1, protocol=1, string=4)
    + HID
    + build.endpoint(0x81, 3, 8, 10)
    + build.interface(1, endpoints=2, cls=0xFF)
    + build.endpoint(0x82, 2, 512)
    + build.endpoint(0x03, 2, 512)
    + build.interface(1, alternate=1, endpoints=0, cls=0xFF)
)
CONFIG = build.configuration(BODY, interfaces=2, attributes=0xE0, max_power_ma=100)
BOS = build.bos(build.usb2_extension(lpm=True))
SIMPLE = build.interface(0, endpoints=0, cls=0xFF)


def test_parse_configuration_structure() -> None:
    config = parse_configuration(CONFIG)
    assert config.value == 1 and set(config.interfaces) == {0, 1}
    assert set(config.interfaces[1]) == {0, 1}
    assert config.interfaces[0][0].hid_report_lengths == (63,)
    assert [e.address for e in config.interfaces[1][0].endpoints] == [0x82, 0x03]
    assert parse_device(DEVICE).vendor == 0x1209


@pytest.mark.parametrize(
    "raw",
    [
        CONFIG[:-1],  # wTotalLength mismatch
        CONFIG[:9] + bytes([1, 4]) + CONFIG[11:],  # bLength 1
        build.configuration(build.endpoint(0x81, 2, 64), interfaces=0),  # orphan EP
        build.configuration(
            build.interface(0, endpoints=2, cls=0xFF) + build.endpoint(0x81, 2, 64),
            interfaces=1,
        ),  # bNumEndpoints mismatch
        build.configuration(
            build.interface(0, endpoints=1, cls=0xFF) + build.endpoint(0x81, 2, 0),
            interfaces=1,
        ),  # wMaxPacketSize 0 (crashes UDC drivers per raw_gadget.c)
    ],
)
def test_malformed_configurations_are_rejected(raw: bytes) -> None:
    with pytest.raises(DescriptorError):
        parse_configuration(raw)


DUPLICATE_ENDPOINTS = {
    "within one alternate setting": (
        build.interface(0, endpoints=2, cls=0xFF)
        + build.endpoint(0x81, 2, 512)
        + build.endpoint(0x81, 2, 512),
        1,
        "duplicate endpoint 0x81",
    ),
    "across interfaces": (
        build.interface(0, endpoints=1, cls=0xFF)
        + build.endpoint(0x02, 2, 512)
        + build.interface(1, endpoints=2, cls=0xFF)
        + build.endpoint(0x81, 2, 512)
        + build.endpoint(0x02, 2, 512),
        2,
        "duplicate endpoint 0x02 in interfaces 0 and 1",
    ),
    "across an interface and another's alternate setting": (
        build.interface(0, endpoints=1, cls=0xFF)
        + build.endpoint(0x81, 3, 8, 10)
        + build.interface(1, endpoints=0, cls=0xFF)
        + build.interface(2, endpoints=0, cls=0xFF)
        + build.interface(2, alternate=1, endpoints=1, cls=0xFF)
        + build.endpoint(0x81, 2, 512),
        3,
        "duplicate endpoint 0x81 in interfaces 0 and 2",
    ),
}


@pytest.mark.parametrize("case", sorted(DUPLICATE_ENDPOINTS))
def test_duplicate_endpoint_addresses_are_rejected(case: str) -> None:
    body, interfaces, message = DUPLICATE_ENDPOINTS[case]
    with pytest.raises(DescriptorError) as raised:
        parse_configuration(build.configuration(body, interfaces=interfaces))
    assert str(raised.value) == message


def test_alternate_settings_of_one_interface_may_reuse_addresses() -> None:
    body = (
        build.interface(0, endpoints=1, cls=0xFF)
        + build.endpoint(0x81, 2, 64)
        + build.interface(1, endpoints=0, cls=0xFF)
        + build.interface(0, alternate=1, endpoints=2, cls=0xFF)
        + build.endpoint(0x81, 2, 512)
        + build.endpoint(0x02, 2, 512)
    )
    config = parse_configuration(build.configuration(body, interfaces=2))
    assert [e.address for e in config.interfaces[0][1].endpoints] == [0x81, 0x02]


@pytest.mark.parametrize(
    ("speed", "device", "body", "match"),
    [
        (Speed.SUPER, DEVICE, SIMPLE, "super-speed"),
        (Speed.HIGH, build.device(vendor=1, product=1, device_class=9), SIMPLE, "hub"),
        (
            Speed.FULL,
            DEVICE,
            build.interface(0, endpoints=0, cls=1)
            + build.interface(0, alternate=1, endpoints=1, cls=1)
            + build.endpoint(0x01, 1, 192, 1),
            "isochronous",
        ),
        (
            Speed.LOW,
            DEVICE,
            build.interface(0, endpoints=1, cls=0xFF) + build.endpoint(0x81, 2, 8),
            "low-speed bulk",
        ),
        (
            Speed.HIGH,
            DEVICE,
            build.interface(0, endpoints=8, cls=0xFF)
            + b"".join(build.endpoint(0x80 | n, 2, 512) for n in range(1, 9)),
            "UDC budget",
        ),
    ],
)
def test_unsupported_topologies_have_clear_reasons(
    speed: Speed, device: bytes, body: bytes, match: str
) -> None:
    configs = [parse_configuration(build.configuration(body, interfaces=1))]
    with pytest.raises(UnsupportedError, match=match):
        check_supported(parse_device(device), configs, speed, UdcBudget())


def test_endpoint_number_limit_comes_from_budget() -> None:
    body = build.interface(0, endpoints=1, cls=0xFF) + build.endpoint(0x89, 2, 512)
    configs = [parse_configuration(build.configuration(body, interfaces=1))]
    check_supported(parse_device(DEVICE), configs, Speed.HIGH, UdcBudget())
    with pytest.raises(UnsupportedError, match="number limit"):
        check_supported(
            parse_device(DEVICE), configs, Speed.HIGH, UdcBudget(max_number=7)
        )


def test_power_and_lpm_rewrites_are_byte_exact() -> None:
    rewritten = rewrite_configuration(CONFIG, 500)
    assert rewritten[7] == 0x80 and rewritten[8] == 250  # bus powered, no wakeup
    assert rewritten[:7] == CONFIG[:7] and rewritten[9:] == CONFIG[9:]
    assert rewrite_configuration(CONFIG, 0)[8] == 0
    with pytest.raises(ValueError):
        rewrite_configuration(CONFIG, 501)
    cleared = rewrite_bos(BOS)
    assert BOS[8] & 0x02 and not cleared[8] & 0x02
    assert cleared[:8] == BOS[:8] and cleared[9:] == BOS[9:]


def test_urb_intervals_follow_usb_fill_int_urb() -> None:
    assert urb_interval(Speed.HIGH, ch9.XFER_INT, 4) == 8  # microframes
    assert urb_interval(Speed.HIGH, ch9.XFER_INT, 0) == 1
    assert urb_interval(Speed.FULL, ch9.XFER_INT, 10) == 10  # frames
    assert urb_interval(Speed.LOW, ch9.XFER_INT, 0) == 1
    assert urb_interval(Speed.HIGH, ch9.XFER_BULK, 4) == 0


class FakeRemote:
    def __init__(self) -> None:
        self.requests: list[Setup] = []
        self.answers: dict[tuple[int, int, int], bytes | None] = {
            (0x80, 0x0100, 0): DEVICE,
            (0x80, 0x0200, 0): CONFIG,
            (0x80, 0x0600, 0): build.qualifier(),
            (0x80, 0x0700, 0): None,
            (0x80, 0x0F00, 0): BOS,
            (0x80, 0x0300, 0): build.langids(0x0409, 0x0407),
            (0x81, 0x2200, 0): bytes(63),
        }
        for index in (1, 2, 3, 4):
            for lang in (0x0409, 0x0407):
                self.answers[(0x80, 0x0300 | index, lang)] = build.string(f"s{index}")

    def __call__(self, setup: Setup) -> bytes | None:
        self.requests.append(setup)
        data = self.answers.get((setup.request_type, setup.value, setup.index))
        return None if data is None else data[: setup.length]


def test_build_presentation_fetches_once_and_rewrites() -> None:
    remote = FakeRemote()
    shown = build_presentation(remote, Speed.HIGH, power_ma=300, ep0_size=64)
    assert shown.speed == Speed.HIGH and shown.device_raw[7] == 64
    assert shown.config_raw[0][7] == 0x80 and shown.config_raw[0][8] == 150
    assert shown.qualifier is not None and shown.qualifier[7] == 64
    assert shown.other_speed is None and shown.bos == rewrite_bos(BOS)
    assert shown.strings[(4, 0x0407)] == build.string("s4")
    assert shown.class_descriptors[(0, 0x22, 0)] == bytes(63)
    assert all(s.is_in for s in remote.requests)
    assert not any(s.request == ch9.SET_ADDRESS for s in remote.requests)


def test_low_speed_presents_at_full_speed_without_endpoint_rewrites() -> None:
    remote = FakeRemote()
    remote.answers[(0x80, 0x0100, 0)] = build.device(
        vendor=1, product=2, bcd_usb=0x0110, max_packet0=8
    )
    body = build.interface(0, endpoints=1, cls=3, protocol=1) + build.endpoint(
        0x81, 3, 8, 10
    )
    raw = build.configuration(body, interfaces=1)
    remote.answers[(0x80, 0x0200, 0)] = raw
    shown = build_presentation(remote, Speed.LOW, power_ma=500)
    assert shown.speed == Speed.FULL and shown.remote_speed == Speed.LOW
    assert shown.config_raw[0][9:] == raw[9:]  # endpoints untouched
    assert shown.device_raw[7] == 64 and shown.qualifier is None


def test_control_routing_table() -> None:
    shown = build_presentation(FakeRemote(), Speed.HIGH, power_ma=500)
    policy = ProxyPolicy()
    state = ControlState()

    def route(setup: Setup) -> object:
        return decide(setup, shown, state, policy)

    assert route(ch9.get_descriptor(1, 0, 8)) == Reply(shown.device_raw)
    assert route(ch9.get_descriptor(7, 0, 9)) == Stall("descriptor absent")
    assert route(ch9.get_descriptor(3, 0xEE, 18)) == Forward(cache=True)
    state.descriptor_misses[(0x03EE, 0)] = (None, 18)
    assert isinstance(route(ch9.get_descriptor(3, 0xEE, 18)), Stall)
    assert route(Setup(0, ch9.SET_ADDRESS, 5)) == Ack()
    assert route(Setup(0, ch9.SET_CONFIGURATION, 1)) == SetConfiguration(1)
    assert isinstance(route(Setup(0, ch9.SET_CONFIGURATION, 9)), Stall)
    assert isinstance(route(Setup(1, ch9.SET_INTERFACE, 1, 1)), Stall)  # unconfigured
    state.configuration, state.alternates = 1, {0: 0, 1: 0}
    assert route(Setup(1, ch9.SET_INTERFACE, 1, 1)) == SetInterface(1, 1)
    assert route(Setup(0x81, ch9.GET_INTERFACE, 0, 1, 1)) == Reply(b"\0")
    assert route(Setup(0x80, ch9.GET_STATUS, 0, 0, 2)) == Reply(b"\0\0")
    assert route(Setup(0x82, ch9.GET_STATUS, 0, 0x82, 2)) == Reply(b"\0\0")
    assert route(Setup(2, ch9.CLEAR_FEATURE, 0, 0x82)) == EndpointHalt(0x82, False)
    assert isinstance(route(Setup(0, ch9.SET_FEATURE, 1)), Stall)  # remote wakeup
    assert route(Setup(0x21, 0x0A, 0, 0)) == Forward()
    assert route(Setup(0xC0, 0x01, 0, 0, 12)) == Forward()
    hid = Setup(0x81, ch9.GET_DESCRIPTOR, 0x2200, 0, 63)
    assert route(hid) == Reply(bytes(63))
    assert decide(hid, shown, state, ProxyPolicy(descriptor_cache=False)) == Forward()


def test_in_write_plan_preserves_packets() -> None:
    assert in_write_plan([(512, 512), (512, 512), (13, 512)], 512, 65536) == (
        3,
        1037,
        False,
    )
    assert in_write_plan([(512, 512), (0, 512)], 512, 65536) == (2, 512, True)
    assert in_write_plan([(0, 512)], 512, 65536) == (1, 0, False)
    assert in_write_plan([(512, 512), (512, 512)], 512, 512) == (1, 512, False)
    assert in_write_plan([(1024, 2048), (512, 512)], 512, 65536) == (1, 1024, True)


def test_cbw_parsing_and_chunking() -> None:
    cbw = b"USBC" + bytes(4) + (4096).to_bytes(4, "little") + bytes([0x80, 0, 10])
    assert parse_cbw(cbw + bytes(16)) == (4096, True)
    assert parse_cbw(cbw + bytes(15)) is None
    assert parse_cbw(b"USBX" + cbw[4:] + bytes(16)) is None
    assert chunk_sizes(70000, 65536) == [65536, 4464]
    assert chunk_sizes(0, 512) == []


@pytest.mark.parametrize(
    "kwargs",
    [{"power_ma": 600}, {"ep0_size": 9}, {"out_window": 0}, {"max_transfer": 1000}],
)
def test_policy_validation(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        ProxyPolicy(**kwargs)  # type: ignore[arg-type]
