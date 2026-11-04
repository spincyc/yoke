"""Offline tests for the simulated USB device models (``tests/usbsim/devices``).

The models are simulated software evidence. These tests check them against
the specification behavior they encode through the DeviceModel API (control
transfers, URBs, CLEAR_FEATURE), never against hardware.
"""

from __future__ import annotations

import struct
from collections.abc import Callable

import pytest

from tests.usbsim import build
from tests.usbsim.device import DeviceModel
from tests.usbsim.devices import (
    ALL_DEVICES,
    KEYBOARD_REPORT_DESCRIPTOR,
    MOUSE_REPORT_DESCRIPTOR,
    RECOVERED,
    VERSION,
    CdcAcm,
    CompositeDevice,
    DisconnectingDevice,
    FullSpeedOnly,
    HidKeyboard,
    HidMouse,
    HubDevice,
    IsoAudioDevice,
    Loopback,
    ManyEndpointsDevice,
    MassStorage,
    StallingDevice,
    SuperSpeedDevice,
)
from yoke.usb import ch9
from yoke.usb.ch9 import Setup, Speed
from yoke.usb.descriptors import (
    UdcBudget,
    UnsupportedError,
    check_supported,
    iter_descriptors,
    parse_configuration,
    parse_device,
)
from yoke.usb.usbip import EPIPE, ESHUTDOWN

Result = tuple[int, int, bytes]
Factory = Callable[[], DeviceModel]

FUNCTIONAL: dict[str, Factory] = {**ALL_DEVICES, "full_speed_only": FullSpeedOnly}
REJECTED: dict[str, Factory] = {
    "hub": HubDevice,
    "iso_audio": IsoAudioDevice,
    "many_endpoints": ManyEndpointsDevice,
    "super_speed": SuperSpeedDevice,
}
EVERY: dict[str, Factory] = {**FUNCTIONAL, **REJECTED}


class Recorder:
    """A completion callback that records ``(status, actual, data)``."""

    def __init__(self) -> None:
        self.results: list[Result] = []

    def __call__(self, status: int, actual: int, data: bytes) -> None:
        self.results.append((status, actual, data))


def read(model: DeviceModel, address: int, length: int) -> list[Result]:
    """Submit an IN URB; the returned list fills when it completes."""
    recorder = Recorder()
    model.submit_in(address, length, recorder)
    return recorder.results


def write(
    model: DeviceModel, address: int, data: bytes, *, zlp: bool = False
) -> list[Result]:
    """Submit an OUT URB; the returned list fills when it completes."""
    recorder = Recorder()
    model.submit_out(address, data, zlp, recorder)
    return recorder.results


def descriptor(
    model: DeviceModel, dtype: int, index: int = 0, *, lang: int = 0, recip: int = 0
) -> bytes | None:
    setup = ch9.get_descriptor(dtype, index, 0xFFFF, lang=lang, recip=recip)
    return model.control(setup)


def endpoint_halted(model: DeviceModel, address: int) -> bool:
    status = model.control(Setup(0x82, ch9.GET_STATUS, 0, address, 2))
    assert status is not None
    return status == b"\x01\x00"


def pattern(length: int) -> bytes:
    return bytes(i & 0xFF for i in range(length))


# --- descriptors and support checks -------------------------------------------


def test_registry_names_the_eight_functional_models() -> None:
    assert set(ALL_DEVICES) == {
        "keyboard",
        "mouse",
        "mass_storage",
        "cdc_acm",
        "composite",
        "loopback",
        "stalling",
        "disconnecting",
    }
    assert ESHUTDOWN == 108 and EPIPE == 32


def test_product_ids_are_distinct_under_the_test_vid() -> None:
    devices = [parse_device(factory().device_descriptor) for factory in EVERY.values()]
    assert {d.vendor for d in devices} == {0x1209}
    assert len({d.product for d in devices}) == len(devices)


@pytest.mark.parametrize("name", sorted(EVERY))
def test_descriptors_parse_and_are_served(name: str) -> None:
    model = EVERY[name]()
    raw_device = descriptor(model, ch9.DT_DEVICE)
    assert raw_device is not None
    device = parse_device(raw_device)
    raw_config = descriptor(model, ch9.DT_CONFIG)
    assert raw_config == model.config_descriptors[0]
    config = parse_configuration(raw_config)
    assert config.value == model.configuration == 1
    default_endpoints = {
        endpoint.address
        for alts in config.interfaces.values()
        for endpoint in alts[0].endpoints
    }
    assert default_endpoints == set(model.pipes)

    assert descriptor(model, ch9.DT_STRING) == build.langids(0x0409)
    indices = {device.i_manufacturer, device.i_product, device.i_serial}
    indices |= {
        alt.string_index for a in config.interfaces.values() for alt in a.values()
    }
    for index in indices - {0}:
        text = descriptor(model, ch9.DT_STRING, index, lang=0x0409)
        assert text is not None and len(text) > 2

    qualifier = descriptor(model, ch9.DT_DEVICE_QUALIFIER)
    other = descriptor(model, ch9.DT_OTHER_SPEED_CONFIG)
    if model.speed == Speed.HIGH:
        assert qualifier is not None and qualifier[:2] == bytes([10, 6])
        assert other is not None and other[1] == ch9.DT_OTHER_SPEED_CONFIG
        full = parse_configuration(other)
        assert {e.address for e in full.all_endpoints()} == {
            e.address for e in config.all_endpoints()
        }
        assert all(e.packet_size <= 64 for e in full.all_endpoints())
    else:
        assert qualifier is None and other is None

    for number, alts in config.interfaces.items():
        for alt in alts.values():
            for index, length in enumerate(alt.hid_report_lengths):
                report = descriptor(
                    model,
                    ch9.DT_HID_REPORT,
                    index,
                    lang=number,
                    recip=ch9.RECIP_INTERFACE,
                )
                assert report is not None and len(report) == length


def check(model: DeviceModel) -> None:
    device = parse_device(model.device_descriptor)
    configs = [parse_configuration(raw) for raw in model.config_descriptors]
    check_supported(device, configs, model.speed, UdcBudget())


@pytest.mark.parametrize("name", sorted(FUNCTIONAL))
def test_functional_models_are_supported(name: str) -> None:
    check(FUNCTIONAL[name]())


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("hub", "hub"),
        ("iso_audio", "isochronous"),
        ("many_endpoints", "UDC budget"),
        ("super_speed", "super-speed"),
    ],
)
def test_rejection_fixtures_are_unsupported(name: str, reason: str) -> None:
    with pytest.raises(UnsupportedError, match=reason):
        check(REJECTED[name]())


# --- HID -----------------------------------------------------------------------


def test_keyboard_press_queues_boot_reports() -> None:
    keyboard = HidKeyboard()
    device = parse_device(keyboard.device_descriptor)
    assert (keyboard.speed, device.bcd_usb, device.max_packet0) == (
        Speed.LOW,
        0x0110,
        8,
    )
    alt = parse_configuration(keyboard.config_descriptors[0]).alt(0, 0)
    assert alt is not None
    assert (alt.interface_class, alt.interface_subclass, alt.interface_protocol) == (
        3,
        1,
        1,
    )
    endpoint = alt.endpoints[0]
    assert (endpoint.address, endpoint.packet_size, endpoint.interval) == (0x81, 8, 10)

    pending = read(keyboard, 0x81, 8)
    assert pending == []
    assert keyboard.press(0x04, 0x05, modifiers=0x02)
    report = bytes([0x02, 0, 0x04, 0x05, 0, 0, 0, 0])
    assert pending == [(0, 8, report)]
    assert keyboard.control(Setup(0xA1, 0x01, 0x0100, 0, 8)) == report
    assert keyboard.release()
    assert read(keyboard, 0x81, 8) == [(0, 8, bytes(8))]
    assert keyboard.press(*range(4, 11))  # seven keys: ErrorRollOver
    assert read(keyboard, 0x81, 8) == [(0, 8, bytes([0, 0, 1, 1, 1, 1, 1, 1]))]
    with pytest.raises(ValueError):
        keyboard.press(0x100)


def test_keyboard_set_report_sets_leds() -> None:
    keyboard = HidKeyboard()
    led_bits = bytes.fromhex("95 05 75 01 05 08 19 01 29 05 91 02")
    assert led_bits in KEYBOARD_REPORT_DESCRIPTOR
    assert len(KEYBOARD_REPORT_DESCRIPTOR) == 63
    assert keyboard.class_descriptors[(0, 0x22, 0)] == KEYBOARD_REPORT_DESCRIPTOR

    assert keyboard.control(Setup(0x21, 0x09, 0x0200, 0, 1), b"\x03") == b""
    assert keyboard.leds == 0x03
    assert keyboard.control(Setup(0xA1, 0x01, 0x0200, 0, 1)) == b"\x03"
    assert keyboard.control(Setup(0x21, 0x09, 0x0300, 0, 1), b"\x01") is None
    assert keyboard.control(Setup(0x21, 0x09, 0x0200, 1, 1), b"\x01") is None
    assert keyboard.leds == 0x03


def test_keyboard_protocol_defaults_and_resets() -> None:
    keyboard = HidKeyboard()
    get_protocol = Setup(0xA1, 0x03, 0, 0, 1)
    get_idle = Setup(0xA1, 0x02, 0, 0, 1)
    assert keyboard.control(get_protocol) == b"\x01"
    assert keyboard.control(Setup(0x21, 0x0B, 0, 0, 0)) == b""
    assert keyboard.control(get_protocol) == b"\x00"
    assert keyboard.control(Setup(0x21, 0x0B, 2, 0, 0)) is None
    assert keyboard.control(get_idle) == bytes([125])
    assert keyboard.control(Setup(0x21, 0x0A, 0x0400, 0, 0)) == b""
    assert keyboard.control(get_idle) == b"\x04"
    assert keyboard.reset()
    assert keyboard.control(get_protocol) == b"\x01"
    assert keyboard.control(get_idle) == bytes([125])


def test_mouse_reports_and_full_speed_only() -> None:
    mouse = HidMouse()
    device = parse_device(mouse.device_descriptor)
    assert (mouse.speed, device.bcd_usb) == (Speed.FULL, 0x0200)
    assert descriptor(mouse, ch9.DT_DEVICE_QUALIFIER) is None
    alt = parse_configuration(mouse.config_descriptors[0]).alt(0, 0)
    assert alt is not None
    assert (alt.interface_subclass, alt.interface_protocol) == (1, 2)
    assert alt.hid_report_lengths == (len(MOUSE_REPORT_DESCRIPTOR),)
    endpoint = alt.endpoints[0]
    assert (endpoint.address, endpoint.packet_size, endpoint.interval) == (0x81, 4, 10)

    assert mouse.move(5, -3, buttons=0x01, wheel=-1)
    report = bytes([0x01, 5, 0xFD, 0xFF])
    assert read(mouse, 0x81, 4) == [(0, 4, report)]
    assert mouse.control(Setup(0xA1, 0x01, 0x0100, 0, 4)) == report
    assert mouse.control(Setup(0x21, 0x09, 0x0200, 0, 1), b"\x01") is None
    with pytest.raises(ValueError):
        mouse.move(128, 0)
    with pytest.raises(ValueError):
        mouse.move(0, 0, buttons=8)


# --- Mass storage ---------------------------------------------------------------


def cbw(tag: int, length: int, cb: bytes, *, data_in: bool = True) -> bytes:
    flags = 0x80 if data_in else 0x00
    head = b"USBC" + struct.pack("<IIBBB", tag, length, flags, 0, len(cb))
    return head + cb.ljust(16, b"\0")


def csw(tag: int, residue: int, status: int) -> bytes:
    return b"USBS" + struct.pack("<IIB", tag, residue, status)


def rw10(opcode: int, lba: int, count: int) -> bytes:
    return struct.pack(">BBIBHB", opcode, 0, lba, 0, count, 0)


def scsi(
    msc: MassStorage,
    tag: int,
    cb: bytes,
    length: int,
    *,
    data_in: bool = True,
    data_out: bytes = b"",
) -> tuple[list[Result], bytes]:
    """Run one command in Linux usb-storage URB order: CBW, data, CSW."""
    assert write(msc, 0x02, cbw(tag, length, cb, data_in=data_in)) == [(0, 31, b"")]
    data: list[Result] = []
    if length and data_in:
        data = read(msc, 0x81, length)
    elif length:
        data = write(msc, 0x02, data_out)
    status = read(msc, 0x81, 13)
    assert len(status) == 1 and status[0][:2] == (0, 13)
    return data, status[0][2]


def sense_of(msc: MassStorage, tag: int) -> tuple[int, int, int]:
    data, status = scsi(msc, tag, bytes([0x03, 0, 0, 0, 18, 0]), 18)
    assert status == csw(tag, 0, 0)
    sense = data[0][2]
    assert len(sense) == 18 and sense[0] == 0x70
    return sense[2], sense[12], sense[13]


def test_msc_inquiry_and_read_capacity() -> None:
    msc = MassStorage()
    data, status = scsi(msc, 1, bytes([0x12, 0, 0, 0, 36, 0]), 36)
    assert status == csw(1, 0, 0)
    inquiry = data[0][2]
    assert data[0][:2] == (0, 36)
    assert inquiry[0] == 0x00 and inquiry[1] & 0x80 and inquiry[8:16] == b"YOKE    "

    # The allocation length is obeyed and the short packet ends the transfer.
    data, status = scsi(msc, 2, bytes([0x12, 0, 0, 0, 5, 0]), 36)
    assert data == [(0, 5, inquiry[:5])]
    assert status == csw(2, 31, 0)

    data, status = scsi(msc, 3, bytes([0x25]) + bytes(9), 8)
    assert data == [(0, 8, struct.pack(">II", 2047, 512))]
    assert status == csw(3, 0, 0)
    assert msc.commands == [0x12, 0x12, 0x25]


def test_msc_class_requests_and_simple_commands() -> None:
    msc = MassStorage()
    assert msc.control(Setup(0xA1, 0xFE, 0, 0, 1)) == b"\x00"
    assert msc.control(Setup(0xA1, 0xFE, 0, 1, 1)) is None
    assert msc.control(Setup(0x21, 0xFF, 0, 0, 0)) == b""
    assert msc.bot_resets == 1

    assert scsi(msc, 1, bytes(6), 0) == ([], csw(1, 0, 0))
    assert scsi(msc, 2, bytes([0x1E, 0, 0, 0, 1, 0]), 0) == ([], csw(2, 0, 0))
    assert msc.prevent_removal
    data, status = scsi(msc, 3, bytes([0x1A, 0, 0x3F, 0, 192, 0]), 192)
    assert data == [(0, 4, bytes([3, 0, 0, 0]))]
    assert status == csw(3, 188, 0)
    # Host expects no data but the device would send some: phase error.
    assert scsi(msc, 4, rw10(0x28, 0, 1), 0) == ([], csw(4, 0, 2))


def test_msc_write_then_read_round_trip() -> None:
    msc = MassStorage()
    payload = pattern(1024)
    data, status = scsi(
        msc, 7, rw10(0x2A, 10, 2), 1024, data_in=False, data_out=payload
    )
    assert data == [(0, 1024, b"")]
    assert status == csw(7, 0, 0)
    assert msc.disk[10 * 512 : 12 * 512] == payload

    data, status = scsi(msc, 8, rw10(0x28, 10, 2), 1024)
    assert data == [(0, 1024, payload)]
    assert status == csw(8, 0, 0)
    assert msc.commands == [0x2A, 0x28]


def test_msc_out_of_range_lba_reports_sense() -> None:
    msc = MassStorage()
    assert write(msc, 0x02, cbw(5, 1024, rw10(0x28, 2047, 2))) == [(0, 31, b"")]
    assert read(msc, 0x81, 1024) == [(-EPIPE, 0, b"")]
    assert endpoint_halted(msc, 0x81)
    assert msc.clear_halt(0x81)
    assert read(msc, 0x81, 13) == [(0, 13, csw(5, 1024, 1))]
    assert sense_of(msc, 6) == (0x05, 0x21, 0x00)
    assert sense_of(msc, 7) == (0x00, 0x00, 0x00)  # reported once

    # Data OUT for a failing WRITE is accepted and discarded.
    before = bytes(msc.disk)
    data, status = scsi(
        msc, 8, rw10(0x2A, 2048, 1), 512, data_in=False, data_out=b"\xaa" * 512
    )
    assert data == [(0, 512, b"")]
    assert status == csw(8, 512, 1)
    assert msc.disk == before
    assert sense_of(msc, 9) == (0x05, 0x21, 0x00)


@pytest.mark.parametrize("urb_first", [False, True], ids=["linux-order", "urb-first"])
def test_msc_unknown_opcode_stalls_data_in(urb_first: bool) -> None:
    msc = MassStorage()
    data = read(msc, 0x81, 64) if urb_first else []
    assert write(msc, 0x02, cbw(9, 64, bytes([0xC5]))) == [(0, 31, b"")]
    if not urb_first:
        data = read(msc, 0x81, 64)
    assert data == [(-EPIPE, 0, b"")]
    assert msc.control(Setup(0x02, ch9.CLEAR_FEATURE, 0, 0x81, 0)) == b""
    assert read(msc, 0x81, 13) == [(0, 13, csw(9, 64, 1))]
    assert sense_of(msc, 10) == (0x05, 0x20, 0x00)


def test_msc_unknown_opcode_discards_data_out() -> None:
    msc = MassStorage()
    data, status = scsi(
        msc, 4, bytes([0xC5]), 1024, data_in=False, data_out=bytes(1024)
    )
    assert data == [(0, 1024, b"")]
    assert status == csw(4, 1024, 1)


def test_msc_holds_the_next_cbw_until_the_status_is_sent() -> None:
    msc = MassStorage()
    write(msc, 0x02, cbw(1, 64, bytes([0xC5])))
    assert read(msc, 0x81, 64) == [(-EPIPE, 0, b"")]
    held = write(msc, 0x02, cbw(2, 0, bytes(6)))
    assert held == []
    assert msc.clear_halt(0x81)
    assert held == [(0, 31, b"")]
    assert read(msc, 0x81, 13) == [(0, 13, csw(1, 64, 1))]
    assert read(msc, 0x81, 13) == [(0, 13, csw(2, 0, 0))]


@pytest.mark.parametrize(
    "bad",
    [
        b"USBX" + cbw(1, 0, bytes(6))[4:],
        cbw(1, 0, bytes(6))[:30],
        cbw(1, 0, bytes(6)) + b"\0",
    ],
    ids=["signature", "short", "long"],
)
def test_msc_invalid_cbw_stalls_until_class_reset(bad: bytes) -> None:
    msc = MassStorage()
    assert write(msc, 0x02, bad) == [(0, len(bad), b"")]
    assert read(msc, 0x81, 13) == [(-EPIPE, 0, b"")]
    # BOT 6.6.1: CLEAR_FEATURE does not lift the halt before the reset.
    for _ in range(2):
        assert msc.clear_halt(0x81)
        assert read(msc, 0x81, 13) == [(-EPIPE, 0, b"")]
        assert msc.clear_halt(0x02)
        assert write(msc, 0x02, cbw(2, 0, bytes(6))) == [(-EPIPE, 0, b"")]
    assert msc.commands == []

    assert msc.control(Setup(0x21, 0xFF, 0, 0, 0)) == b""
    assert msc.bot_resets == 1
    assert msc.clear_halt(0x81) and msc.clear_halt(0x02)
    assert scsi(msc, 3, bytes(6), 0) == ([], csw(3, 0, 0))
    assert msc.commands == [0x00]


# --- CDC ACM --------------------------------------------------------------------


def test_cdc_descriptors() -> None:
    acm = CdcAcm()
    device = parse_device(acm.device_descriptor)
    assert (device.device_class, device.device_subclass, device.device_protocol) == (
        0xEF,
        2,
        1,
    )
    raw = acm.config_descriptors[0]
    assert build.iad(0, 2, 2, 2, 1) in raw
    assert [t for t, _ in iter_descriptors(raw[9:])].count(0x24) == 4
    config = parse_configuration(raw)
    control, data = config.alt(0, 0), config.alt(1, 0)
    assert control is not None and data is not None
    assert (control.interface_class, control.interface_subclass) == (2, 2)
    notify = control.endpoints[0]
    assert (notify.address, notify.packet_size, notify.interval) == (0x83, 10, 16)
    assert data.interface_class == 0x0A
    assert [(e.address, e.packet_size) for e in data.endpoints] == [
        (0x81, 64),
        (0x02, 64),
    ]


def test_cdc_line_coding_round_trip() -> None:
    acm = CdcAcm()
    get = Setup(0xA1, 0x21, 0, 0, 7)
    default = struct.pack("<IBBB", 115200, 0, 0, 8)
    assert acm.control(get) == default
    coding = struct.pack("<IBBB", 9600, 2, 2, 7)
    assert acm.control(Setup(0x21, 0x20, 0, 0, 7), coding) == b""
    assert acm.control(get) == coding
    invalid = struct.pack("<IBBB", 9600, 0, 0, 9)
    assert acm.control(Setup(0x21, 0x20, 0, 0, 7), invalid) is None
    assert acm.control(get) == coding
    assert acm.reset()
    assert acm.control(get) == default


def test_cdc_dtr_queues_serial_state() -> None:
    acm = CdcAcm()
    assert acm.control(Setup(0x21, 0x22, 0x0003, 0, 0)) == b""
    assert acm.control_lines == 0x0003
    raised = bytes.fromhex("a1 20 0000 0000 0200 0300")
    assert read(acm, 0x83, 10) == [(0, 10, raised)]
    assert acm.control(Setup(0x21, 0x22, 0x0001, 0, 0)) == b""  # RTS only
    assert not acm.in_pipe(0x83).packets
    assert acm.control(Setup(0x21, 0x22, 0x0000, 0, 0)) == b""
    dropped = bytes.fromhex("a1 20 0000 0000 0200 0000")
    assert read(acm, 0x83, 10) == [(0, 10, dropped)]


def test_cdc_echo_preserves_packet_boundaries() -> None:
    acm = CdcAcm()
    full = pattern(64)
    assert write(acm, 0x02, full, zlp=True) == [(0, 64, b"")]
    assert write(acm, 0x02, b"abc") == [(0, 3, b"")]
    assert write(acm, 0x02, b"defg") == [(0, 4, b"")]
    assert write(acm, 0x02, b"") == [(0, 0, b"")]
    # Each echoed short packet or ZLP ends a large IN buffer.
    assert read(acm, 0x81, 512) == [(0, 64, full)]
    assert read(acm, 0x81, 512) == [(0, 3, b"abc")]
    assert read(acm, 0x81, 512) == [(0, 4, b"defg")]
    assert read(acm, 0x81, 512) == [(0, 0, b"")]
    assert not acm.in_pipe(0x81).packets
    assert acm.received == full + b"abcdefg"


# --- Composite -------------------------------------------------------------------


def test_composite_descriptors() -> None:
    composite = CompositeDevice()
    device = parse_device(composite.device_descriptor)
    assert (composite.speed, device.bcd_usb) == (Speed.HIGH, 0x0201)
    bos = descriptor(composite, ch9.DT_BOS)
    assert bos == build.bos(build.usb2_extension(lpm=True))
    config = parse_configuration(composite.config_descriptors[0])
    keyboard, vendor = config.alt(0, 0), config.alt(1, 0)
    assert keyboard is not None and vendor is not None
    assert (keyboard.interface_class, keyboard.interface_protocol) == (3, 1)
    endpoint = keyboard.endpoints[0]
    assert (endpoint.address, endpoint.packet_size, endpoint.interval) == (0x81, 8, 4)
    assert vendor.interface_class == 0xFF
    assert [(e.address, e.packet_size) for e in vendor.endpoints] == [
        (0x82, 512),
        (0x03, 512),
    ]
    for alt in (keyboard, vendor):
        assert alt.string_index
        assert descriptor(composite, ch9.DT_STRING, alt.string_index, lang=0x0409)


def test_composite_vendor_requests() -> None:
    composite = CompositeDevice()
    assert composite.control(Setup(0xC0, 0x01, 0, 0, 64)) == VERSION == b"YOKE-SIM-1.0"
    assert composite.control(Setup(0x40, 0x02, 0, 0, 5), b"hello") == b""
    assert composite.control(Setup(0xC0, 0x03, 0, 0, 64)) == b"hello"
    assert composite.control(Setup(0x40, 0x02, 0, 0, 65), bytes(65)) is None
    assert composite.control(Setup(0xC0, 0x03, 0, 0, 64)) == b"hello"
    assert composite.control(Setup(0x40, 0x04)) == b""
    assert composite.control(Setup(0x40, 0x04)) == b""
    assert composite.pings == 2
    assert composite.control(Setup(0x40, 0x04, 0, 0, 1), b"x") is None
    assert composite.control(Setup(0xC0, 0x7F, 0, 0, 8)) is None
    assert composite.control(Setup(0xC1, 0x01, 0, 1, 64)) is None
    assert composite.pings == 2


def test_composite_bulk_loopback_with_zlp_and_keyboard() -> None:
    composite = CompositeDevice()
    payload = pattern(1024)
    assert write(composite, 0x03, payload, zlp=True) == [(0, 1024, b"")]
    assert read(composite, 0x82, 4096) == [(0, 1024, payload)]
    assert not composite.in_pipe(0x82).packets

    # A full packet does not end the transfer; the next short packet does.
    assert write(composite, 0x03, payload[:512]) == [(0, 512, b"")]
    assert not composite.in_pipe(0x82).packets
    assert write(composite, 0x03, b"tail") == [(0, 4, b"")]
    assert read(composite, 0x82, 4096) == [(0, 516, payload[:512] + b"tail")]

    assert composite.press(0x04)
    assert read(composite, 0x81, 8) == [(0, 8, bytes([0, 0, 4, 0, 0, 0, 0, 0]))]


# --- Loopback --------------------------------------------------------------------


def test_loopback_speeds_and_packet_sizes() -> None:
    high = Loopback()
    assert (high.speed, high.packet_size) == (Speed.HIGH, 512)
    assert descriptor(high, ch9.DT_DEVICE_QUALIFIER) is not None
    full = Loopback(Speed.FULL)
    assert full.packet_size == 64
    assert descriptor(full, ch9.DT_DEVICE_QUALIFIER) is None
    assert Loopback(Speed.FULL, 8).in_pipe(0x81).packet_size == 8
    for speed, size in ((Speed.HIGH, 64), (Speed.FULL, 512), (Speed.LOW, None)):
        with pytest.raises(ValueError):
            Loopback(speed, size)

    only = FullSpeedOnly()
    device = parse_device(only.device_descriptor)
    assert (only.speed, device.bcd_usb) == (Speed.FULL, 0x0200)
    assert descriptor(only, ch9.DT_DEVICE_QUALIFIER) is None


@pytest.mark.parametrize("speed", [Speed.HIGH, Speed.FULL])
def test_loopback_echoes_transfers(speed: Speed) -> None:
    loopback = Loopback(speed)
    size = loopback.packet_size
    payload = pattern(2 * size)
    assert write(loopback, 0x02, payload, zlp=True) == [(0, 2 * size, b"")]
    assert read(loopback, 0x81, 8 * size) == [(0, 2 * size, payload)]
    odd = b"x" * (size + 1)
    assert write(loopback, 0x02, odd) == [(0, size + 1, b"")]
    assert read(loopback, 0x81, 8 * size) == [(0, size + 1, odd)]
    assert loopback.received == payload + odd

    assert loopback.emit(b"direct")
    assert read(loopback, 0x81, size) == [(0, 6, b"direct")]
    assert loopback.emit(payload[:size], zlp=True)
    assert read(loopback, 0x81, 8 * size) == [(0, size, payload[:size])]
    assert not loopback.in_pipe(0x81).packets


# --- Fault injection -------------------------------------------------------------


@pytest.mark.parametrize("urb_first", [True, False], ids=["urb-first", "arm-first"])
def test_stalling_in_sends_partial_data_then_recovers(urb_first: bool) -> None:
    device = StallingDevice()
    pending = read(device, 0x81, 4096) if urb_first else []
    # 1100 bytes round down to two whole 512-byte packets.
    assert device.control(Setup(0x40, 0x11, 0x81, 1100)) == b""
    if not urb_first:
        pending = read(device, 0x81, 4096)
    assert pending == [(-EPIPE, 1024, pattern(1024))]
    assert endpoint_halted(device, 0x81)
    assert device.control(Setup(0x02, ch9.CLEAR_FEATURE, 0, 0x81, 0)) == b""
    assert not endpoint_halted(device, 0x81)
    assert read(device, 0x81, 512) == [(0, len(RECOVERED), RECOVERED)]
    assert device.control(Setup(0xC0, 0x12, 0, 0, 1)) == b"\x01"


def test_stalling_in_halts_the_urb_after_the_partial_data() -> None:
    device = StallingDevice()
    assert device.control(Setup(0x40, 0x11, 0x81, 512)) == b""
    assert read(device, 0x81, 512) == [(0, 512, pattern(512))]
    assert read(device, 0x81, 512) == [(-EPIPE, 0, b"")]


def test_stalling_out_stalls_the_next_packet() -> None:
    device = StallingDevice()
    assert device.control(Setup(0x40, 0x11, 0x02, 0)) == b""
    assert write(device, 0x02, b"lost") == [(-EPIPE, 0, b"")]
    assert device.received == b""
    assert endpoint_halted(device, 0x02)
    assert device.clear_halt(0x02)
    assert write(device, 0x02, b"kept") == [(0, 4, b"")]
    assert device.received == b"kept"
    assert device.control(Setup(0xC0, 0x12, 0, 0, 1)) == b"\x01"


def test_stalling_vendor_requests_and_interrupt_endpoint() -> None:
    device = StallingDevice()
    assert device.control(Setup(0xC0, 0x10, 0, 0, 8)) is None
    assert device.control(Setup(0x40, 0x11, 0x85, 0)) is None
    assert device.control(Setup(0xC0, 0x12, 0, 0, 1)) == b"\x00"
    assert device.control(Setup(0x40, 0x11, 0x83, 0)) == b""
    assert read(device, 0x83, 16) == [(-EPIPE, 0, b"")]
    assert device.clear_halt(0x83)
    assert device.emit(0x83, b"irq")
    assert read(device, 0x83, 16) == [(0, 3, b"irq")]
    with pytest.raises(ValueError):
        device.emit(0x02, b"")


def test_disconnect_fires_mid_transfer() -> None:
    device = DisconnectingDevice()
    events: list[tuple[str, int]] = []
    device.unplug_listeners.append(lambda: events.append(("listener", 0)))
    device.submit_in(0x81, 512, lambda s, a, d: events.append(("in", s)))
    device.arm_disconnect(1024)
    device.submit_out(
        0x02, pattern(4096), False, lambda s, a, d: events.append(("out", s))
    )
    # Pending URBs complete with -ESHUTDOWN before the listener runs.
    assert events == [("in", -ESHUTDOWN), ("out", -ESHUTDOWN), ("listener", 0)]
    assert device.unplugged
    assert device.received == pattern(1024)  # two of eight packets landed
    assert read(device, 0x81, 512) == [(-ESHUTDOWN, 0, b"")]
    assert descriptor(device, ch9.DT_DEVICE) is None
    assert not device.emit(b"gone")

    device.replug()
    assert not device.unplugged
    assert device.emit(b"back")
    assert read(device, 0x81, 512) == [(0, 4, b"back")]
    assert write(device, 0x02, b"more") == [(0, 4, b"")]
    with pytest.raises(ValueError):
        device.arm_disconnect(0)
