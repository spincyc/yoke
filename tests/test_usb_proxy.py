"""End-to-end simulation of the computer-side USB data path (ADRs 0021, 0022).

host model <-> simulated UDC/Raw Gadget <-> proxy <-> USB/IP <-> exporter
<-> device model. Assertions compare what the host sees with the device
model through an independent oracle of the documented descriptor rewrites.
Everything is simulated software evidence; nothing here proves hardware
behavior (the ⚠ list in doc/design/USB-DATAPATH.md stays open).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from tests.usbsim import build
from tests.usbsim.classes import CdcHost, HidHost, MassStorageHost
from tests.usbsim.device import DeviceModel, Verdict
from tests.usbsim.devices import (
    ALL_DEVICES,
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
from tests.usbsim.harness import Rig, Transport, make_rig
from tests.usbsim.host import HostProtocolError, HostTimeoutError, StallError
from tests.usbsim.udc import dwc3_like_endpoints
from yoke.usb import ch9
from yoke.usb.ch9 import Setup, Speed
from yoke.usb.client import ImportRejectedError
from yoke.usb.descriptors import UnsupportedError
from yoke.usb.policy import ProxyPolicy

PROFILES = ("linux", "windows", "macos")


@contextmanager
def rig_for(device: DeviceModel, **kwargs: Any) -> Iterator[Rig]:
    rig = make_rig(device, **kwargs)
    try:
        yield rig
    finally:
        rig.close()


def wait_for(predicate: Any, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        time.sleep(0.002)


# --- independent oracle of the documented rewrites ---------------------------


def expected_device(device: DeviceModel) -> bytes:
    out = bytearray(device.device_descriptor)
    out[7] = 64  # bMaxPacketSize0 of the UDC
    return bytes(out)


def expected_config(raw: bytes) -> bytes:
    out = bytearray(raw)
    out[7] = 0x80 | (out[7] & 0x1F)  # bus powered, no remote wakeup
    out[8] = 250  # 500 mA
    return bytes(out)


def expected_bos(raw: bytes) -> bytes:
    out = bytearray(raw)
    offset = raw[0]
    while offset < len(raw):
        if raw[offset + 1] == 16 and raw[offset + 2] == 2:
            out[offset + 3] &= 0xFD  # LPM supported -> clear
        offset += raw[offset]
    return bytes(out)


def remote_answer(device: DeviceModel, setup: Setup) -> bytes | None:
    return device._descriptor(setup)


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("name", sorted(ALL_DEVICES))
def test_enumeration_matches_device_modulo_documented_rewrites(
    name: str, profile: str
) -> None:
    device = ALL_DEVICES[name]()
    with rig_for(device) as rig:
        rig.host.attach()
        seen = rig.host.enumerate(profile)
        assert seen.device == expected_device(device)
        assert seen.configurations == [
            expected_config(c) for c in device.config_descriptors
        ]
        for (index, lang), data in seen.strings.items():
            truth = remote_answer(device, ch9.get_descriptor(3, index, 255, lang=lang))
            assert data == truth, (index, lang)
        presented = Speed.HIGH if device.speed == Speed.HIGH else Speed.FULL
        assert seen.speed == presented == rig.kernel.link_speed
        if seen.qualifier is not None or presented == Speed.FULL:
            assert seen.qualifier is None  # FS-only devices stall the qualifier
        if seen.bos is not None:
            assert device.bos is not None and seen.bos == expected_bos(device.bos)
        assert rig.host.violations == [] and rig.host.deadline_misses == []
        assert device.violations == [] and rig.usbipd.violations == []
        assert device.configuration == 1 and rig.kernel.usb_state == "configured"
        assert rig.kernel.vbus_ma == 500
        assert rig.proxy is not None and rig.proxy.error is None


def test_descriptor_reads_do_not_reach_the_device_after_import() -> None:
    device = CompositeDevice()
    with rig_for(device) as rig:
        imported = len(device.control_log)
        rig.host.attach()
        rig.host.enumerate("linux")
        later = [s for s in device.control_log[imported:] if s.request == 6]
        assert later == []  # served from the cache
        assert rig.host.records and max(r.elapsed for r in rig.host.records) < 0.05


def test_keyboard_class_traffic_and_remote_reset_restores_protocol() -> None:
    device = HidKeyboard()
    with rig_for(device) as rig:
        rig.host.attach()
        rig.host.enumerate("linux")
        hid = HidHost(rig.host)
        assert hid.set_idle()
        report = hid.report_descriptor(device.class_descriptors[(0, 0x22, 0)].__len__())
        assert report == device.class_descriptors[(0, 0x22, 0)]
        device.press(0x04, modifiers=0x02)
        assert hid.read_report() == bytes([0x02, 0, 0x04, 0, 0, 0, 0, 0])
        device.release()
        assert hid.read_report() == bytes(8)
        hid.set_leds(0x02)  # control OUT with data
        wait_for(lambda: device.leds == 0x02)
        hid.set_protocol(0)
        assert hid.get_protocol() == 0
        rig.host.enumerate("windows")  # bus reset -> remote port reset
        wait_for(lambda: device.resets >= 2)
        assert HidHost(rig.host).get_protocol() == 1
        assert "reset_device" in rig.usbipd.stats.tweaks


def test_mouse_reports() -> None:
    device = HidMouse()
    with rig_for(device) as rig:
        rig.host.attach()
        rig.host.enumerate("macos")
        device.move(5, -3, buttons=1, wheel=1)
        assert HidHost(rig.host).read_report(4) == bytes([1, 5, 0xFD, 1])


@pytest.mark.parametrize("bot_sizing", [True, False])
def test_mass_storage_bot_round_trip_and_failed_command(bot_sizing: bool) -> None:
    device = MassStorage()
    with rig_for(device, policy=ProxyPolicy(bot_sizing=bot_sizing)) as rig:
        rig.host.attach()
        rig.host.enumerate("windows")
        storage = MassStorageHost(rig.host)
        assert storage.max_lun() == 0
        assert storage.inquiry()[8:12] != b""
        assert storage.read_capacity() == (2047, 512)
        payload = bytes((i * 7) & 0xFF for i in range(140 * 512))
        storage.write10(10, payload)
        assert bytes(device.disk[10 * 512 : 150 * 512]) == payload
        assert storage.read10(10, 140) == payload
        data, status = storage.command(bytes([0xFF]) + bytes(5), data_in=64)
        assert status.status == 1 and data == b""
        sense, ok = storage.command(bytes([0x03, 0, 0, 0, 18, 0]), data_in=18)
        assert ok.status == 0 and sense[2] & 0x0F == 0x05 and sense[12] == 0x20
        assert rig.proxy is not None and rig.proxy.counters.remote_stalls >= 1
        assert rig.host.violations == []


def test_cdc_acm_line_coding_notification_and_packet_exact_echo() -> None:
    device = CdcAcm()
    with rig_for(device) as rig:
        rig.host.attach()
        rig.host.enumerate("linux")
        cdc = CdcHost(rig.host)
        cdc.set_line_coding(9600, bits=7)
        assert cdc.get_line_coding() == (9600, 0, 0, 7)
        cdc.set_control_lines(0x03)
        assert len(rig.host.transfer_in(0x83, 10)) == 10
        rig.host.transfer_out(0x02, b"a" * 64, zlp=True)
        assert rig.host.transfer_in(0x81, 128) == b"a" * 64  # ZLP terminates
        rig.host.transfer_out(0x02, b"b" * 100)
        assert rig.host.transfer_in(0x81, 512) == b"b" * 100


def test_composite_vendor_requests_and_concurrent_interfaces() -> None:
    device = CompositeDevice()
    with rig_for(device) as rig:
        rig.host.attach()
        rig.host.enumerate("windows")
        host = rig.host
        assert host.control(Setup(0xC0, 0x01, 0, 0, 64)) == b"YOKE-SIM-1.0"
        host.control(Setup(0x40, 0x02, 0, 0, 5), b"hello")
        assert host.control(Setup(0xC0, 0x03, 0, 0, 64)) == b"hello"
        host.control(Setup(0x40, 0x04, 0, 0, 0))
        assert device.pings == 1
        with pytest.raises(StallError):
            host.control(Setup(0xC0, 0x7F, 0, 0, 4))
        reader = threading.Thread(target=lambda: device.press(0x05))
        reader.start()
        host.transfer_out(0x03, bytes(1024), zlp=True)
        assert host.transfer_in(0x82, 4096) == bytes(1024)
        assert HidHost(host).read_report()[2] == 0x05
        reader.join()


def test_full_speed_only_device_and_short_packets() -> None:
    device = FullSpeedOnly()
    with rig_for(device) as rig:
        rig.host.attach()
        seen = rig.host.enumerate("linux")
        assert seen.speed == Speed.FULL and seen.qualifier is None
        for size, zlp in (
            (0, False),
            (1, False),
            (63, False),
            (64, True),
            (129, False),
        ):
            rig.host.transfer_out(0x02, bytes([size & 0xFF]) * size, zlp=zlp)
            assert rig.host.transfer_in(0x81, 1024) == bytes([size & 0xFF]) * size
        device.emit(b"z" * 64, zlp=True)
        assert rig.host.transfer_in(0x81, 1024) == b"z" * 64
        device.emit(b"y" * 64)
        device.emit(b"x" * 10)
        assert rig.host.transfer_in(0x81, 1024) == b"y" * 64 + b"x" * 10


def test_stalls_halts_and_clear_halt_recovery() -> None:
    device = StallingDevice()
    with rig_for(device) as rig:
        rig.host.attach()
        rig.host.enumerate("linux")
        host = rig.host
        with pytest.raises(StallError):
            host.control(Setup(0xC0, 0x10, 0, 0, 1))
        host.control(Setup(0x40, 0x11, 0x81, 1024, 0))  # 1024 bytes then stall
        with pytest.raises(StallError):
            host.transfer_in(0x81, 4096)
        assert host.get_status(ch9.RECIP_ENDPOINT, 0x81) == b"\x01\x00"
        host.clear_halt(0x81)  # answered by the UDC; remote cleared by proxy
        assert host.transfer_in(0x81, 512) == b"RECOVERED"
        host.control(Setup(0x40, 0x11, 0x02, 0, 0))
        host.transfer_out(0x02, bytes(512))  # acknowledged by the UDC first
        with pytest.raises(StallError):
            host.transfer_out(0x02, b"next")  # the device's STALL surfaces here
        host.clear_halt(0x02)
        host.transfer_out(0x02, b"after")
        wait_for(lambda: device.received.endswith(b"after"))
        assert host.control(Setup(0xC0, 0x12, 0, 0, 1))[0] >= 2
        assert rig.proxy is not None
        assert rig.proxy.counters.remote_stalls == 2
        assert rig.proxy.counters.local_stalls == 2
        assert rig.usbipd.stats.tweaks.count("clear_halt 0x81") >= 1


def test_device_disconnect_mid_transfer_presents_disconnect_then_recovers() -> None:
    device = DisconnectingDevice()
    with rig_for(device) as rig:
        rig.host.attach()
        rig.host.enumerate("linux")
        device.arm_disconnect(1024)
        with pytest.raises((HostProtocolError, HostTimeoutError, StallError)):
            rig.host.transfer_out(0x02, bytes(4096), timeout=2)
        assert rig.proxy is not None and rig.proxy.ended.wait(3)
        assert "remote lost" in (rig.proxy.end_reason or "")
        rig.stop()
        assert not rig.kernel.bound  # the computer sees the device go away
        with pytest.raises(ImportRejectedError):
            rig.connect()  # device still unplugged
        device.replug()
        rig.connect()
        rig.host.attach()
        rig.host.enumerate("windows")
        device.emit(b"back")
        assert rig.host.transfer_in(0x81, 512) == b"back"


def test_bus_reset_unlinks_outstanding_urbs_and_reenumerates() -> None:
    device = Loopback()
    with rig_for(device) as rig:
        rig.host.attach()
        rig.host.enumerate("linux")
        depth = rig.policy.in_depth_bulk
        wait_for(lambda: len(device.pipes[0x81].urbs) == depth)
        rig.host.reset()
        wait_for(lambda: rig.usbipd.stats.unlinked >= depth)
        wait_for(lambda: rig.proxy is not None and rig.proxy.state == "default")
        rig.host.enumerate("macos")
        device.emit(b"ok")
        assert rig.host.transfer_in(0x81, 512) == b"ok"


def test_interrupt_endpoint_keeps_its_own_depth() -> None:
    device = HidKeyboard()
    with rig_for(device, policy=ProxyPolicy(in_depth_interrupt=3)) as rig:
        rig.host.attach()
        rig.host.enumerate("linux")
        wait_for(lambda: len(device.pipes[0x81].urbs) == 3)


def test_suspend_resume_and_host_disconnect() -> None:
    device = Loopback()
    with rig_for(device) as rig:
        rig.host.attach()
        rig.host.enumerate("linux")
        assert rig.proxy is not None
        rig.host.suspend()
        wait_for(
            lambda: (
                rig.proxy is not None and rig.proxy.snapshot()["state"] == "suspended"
            )
        )
        rig.host.resume()
        wait_for(
            lambda: (
                rig.proxy is not None and rig.proxy.snapshot()["state"] == "configured"
            )
        )
        device.emit(b"awake")
        assert rig.host.transfer_in(0x81, 512) == b"awake"
        rig.host.disconnect()
        wait_for(lambda: rig.proxy is not None and rig.proxy.state == "disconnected")
        assert rig.proxy.counters.disconnects == 1
        rig.host.attach()
        rig.host.enumerate("windows")
        device.emit(b"again")
        assert rig.host.transfer_in(0x81, 512) == b"again"


def reconnect(rig: Rig) -> None:
    deadline = time.monotonic() + 3
    while True:
        try:
            rig.connect()
            return
        except ImportRejectedError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.01)


@pytest.mark.parametrize("kind", ["memory", "tcp"])
def test_network_reset_presents_disconnect_and_reconnects(kind: str) -> None:
    device = Loopback()
    transport = Transport(kind=kind, latency=0.001, jitter=0.001, seed=3)
    with rig_for(device, transport=transport) as rig:
        rig.host.attach()
        rig.host.enumerate("linux")
        transport.reset()
        assert rig.proxy is not None and rig.proxy.ended.wait(3)
        rig.stop()
        assert not rig.kernel.bound
        reconnect(rig)
        rig.host.attach()
        rig.host.enumerate("linux")
        rig.host.transfer_out(0x02, b"ping")
        assert rig.host.transfer_in(0x81, 512) == b"ping"


def test_exporter_restart_is_survived() -> None:
    device = MassStorage()
    with rig_for(device, transport=Transport(kind="tcp")) as rig:
        rig.host.attach()
        rig.host.enumerate("linux")
        rig.usbipd.stop()
        assert rig.proxy is not None and rig.proxy.ended.wait(3)
        rig.stop()
        rig.transport.address = None  # daemon restarts on a new listener
        reconnect(rig)
        rig.host.attach()
        rig.host.enumerate("windows")
        storage = MassStorageHost(rig.host)
        storage.write10(0, bytes(range(256)) * 4)
        assert storage.read10(0, 2) == bytes(range(256)) * 4


def test_latency_and_jitter_still_meet_control_deadlines() -> None:
    device = CompositeDevice()
    transport = Transport(latency=0.004, jitter=0.002, seed=7)
    with rig_for(device, transport=transport) as rig:
        rig.host.attach()
        for profile in PROFILES:
            rig.host.enumerate(profile)
        assert rig.host.violations == []
        assert rig.host.control(Setup(0xC0, 0x01, 0, 0, 64)) == b"YOKE-SIM-1.0"


def test_round_trip_above_the_no_data_budget_misses_set_configuration() -> None:
    """Documents the RTT budget: SET_CONFIGURATION is forwarded (50 ms limit).

    Real hosts wait about 5 s, so enumeration succeeds; the miss is an
    advisory metric, while cached descriptor reads stay far inside it.
    """
    device = Loopback()
    with rig_for(device, transport=Transport(latency=0.03)) as rig:
        rig.host.attach()
        rig.host.enumerate("linux")
        assert rig.host.violations == []
        assert any("request=9" in v for v in rig.host.deadline_misses)
        cached = [r for r in rig.host.records if r.setup.request == ch9.GET_DESCRIPTOR]
        assert cached and max(r.elapsed for r in cached) < ch9.NO_DATA_DEADLINE


def test_without_descriptor_cache_every_read_is_forwarded() -> None:
    device = CompositeDevice()
    with rig_for(device, policy=ProxyPolicy(descriptor_cache=False)) as rig:
        rig.host.attach()
        rig.host.enumerate("windows")
        assert rig.proxy is not None and rig.proxy.counters.forwarded > 10
        assert rig.host.violations == []


def test_delegating_udc_standard_requests_are_handled_by_the_proxy() -> None:
    device = StallingDevice()
    with rig_for(device, udc_handles_standard=False) as rig:
        rig.host.attach()
        rig.host.enumerate("windows")  # SET_ADDRESS and GET_STATUS reach the proxy
        assert device.violations == []  # SET_ADDRESS never forwarded
        assert rig.host.get_status() == b"\0\0"  # bus powered
        assert rig.host.get_status(ch9.RECIP_ENDPOINT, 0x81) == b"\0\0"
        rig.host.clear_halt(0x81)
        assert "clear_halt 0x81" in rig.usbipd.stats.tweaks
        with pytest.raises(StallError):
            rig.host.control(Setup(0, ch9.SET_FEATURE, 1))  # remote wakeup


@pytest.mark.parametrize(
    ("factory", "reason"),
    [
        (HubDevice, "hub"),
        (IsoAudioDevice, "isochronous"),
        (ManyEndpointsDevice, "UDC budget"),
        (SuperSpeedDevice, "super-speed"),
    ],
)
def test_unsupported_devices_are_rejected_before_presentation(
    factory: type[DeviceModel], reason: str
) -> None:
    with rig_for(factory(), connect=False) as rig:
        with pytest.raises(UnsupportedError, match=reason):
            rig.connect()
        assert not rig.kernel.bound


def test_udc_without_a_required_endpoint_rejects_after_binding() -> None:
    device = CompositeDevice()  # needs endpoint 3 OUT
    with rig_for(device, udc_endpoints=dwc3_like_endpoints(count=2)) as rig:
        assert rig.proxy is not None and rig.proxy.ended.wait(2)
        assert (rig.proxy.rejected or "").startswith("UDC lacks endpoints: 0x03")


def test_link_slower_than_device_is_rejected() -> None:
    device = Loopback()
    with rig_for(device, host_speed=Speed.FULL) as rig:
        rig.host.attach()
        with pytest.raises((StallError, HostProtocolError)):
            rig.host.enumerate("linux")
        assert rig.proxy is not None
        assert (rig.proxy.rejected or "").startswith("computer link is full-speed")


class AltDevice(DeviceModel):
    def __init__(self) -> None:
        body = (
            build.interface(0, endpoints=0, cls=0xFF)
            + build.interface(0, alternate=1, endpoints=2, cls=0xFF)
            + build.endpoint(0x81, 2, 512)
            + build.endpoint(0x02, 2, 512)
        )
        super().__init__(
            speed=Speed.HIGH,
            device=build.device(vendor=0x1209, product=0x99),
            configurations=[build.configuration(body, interfaces=1)],
            strings={1: "Yoke", 2: "Alt", 3: "A1"},
            qualifier=build.qualifier(),
        )

    def on_out(self, address: int, packet: bytes) -> Verdict:
        self.in_pipe(0x81).packets.append(packet)
        self.in_pipe(0x81).pump()
        return Verdict.ACCEPT


def test_set_interface_enables_and_disables_endpoints() -> None:
    device = AltDevice()
    with rig_for(device) as rig:
        rig.host.attach()
        rig.host.enumerate("linux")
        with pytest.raises(LookupError):
            rig.host.transfer_out(0x02, b"x")
        rig.host.set_interface(0, 1)
        assert device.alternates == {0: 1}
        rig.host.transfer_out(0x02, b"alt1")
        assert rig.host.transfer_in(0x81, 512) == b"alt1"
        assert rig.host.control(Setup(0x81, ch9.GET_INTERFACE, 0, 0, 1)) == b"\x01"
        rig.host.set_interface(0, 0)
        with pytest.raises(LookupError):
            rig.host.transfer_in(0x81, 512, timeout=0.2)
        assert "set_interface 0 1" in rig.usbipd.stats.tweaks


def test_out_window_pipelines_without_losing_order() -> None:
    device = Loopback()
    policy = ProxyPolicy(out_window=8)
    with rig_for(device, policy=policy, transport=Transport(latency=0.001)) as rig:
        rig.host.attach()
        rig.host.enumerate("linux")
        payload = bytes(i & 0xFF for i in range(512 * 40)) + b"end"
        rig.host.transfer_out(0x02, payload)
        assert rig.host.transfer_in(0x81, len(payload) + 512) == payload


def test_real_tcp_mass_storage_with_latency() -> None:
    device = MassStorage()
    transport = Transport(kind="tcp", latency=0.0005, jitter=0.0005)
    with rig_for(device, transport=transport) as rig:
        rig.host.attach()
        rig.host.enumerate("macos")
        storage = MassStorageHost(rig.host)
        data = bytes((i * 13) & 0xFF for i in range(64 * 512))
        storage.write10(100, data)
        assert storage.read10(100, 64) == data
        assert rig.host.violations == []


def test_teardown_is_confirmed_and_closes_the_gadget() -> None:
    device = Loopback()
    rig = make_rig(device)
    rig.host.attach()
    rig.host.enumerate("linux")
    assert rig.proxy is not None
    rig.proxy.stop()
    assert rig.proxy.state == "stopped" and not rig.kernel.bound
    assert threading.active_count() < 50
    rig.close()
