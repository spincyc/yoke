"""The native gadget worker end to end in the simulator (ADRs 0021, 0022, 0027).

host model <-> simulated UDC (served over RPC) <-> yoke-gadget-worker-sim
<-> real TCP <-> simulated exporter <-> device model. These are the
scenarios ``tests/test_usb_proxy.py`` and ``tests/test_usb_faults.py`` run
against the Python worker, with the same oracles, so the two
implementations are held to one behavior (ADR 0027: divergence is a test
failure). The worker runs under ASan/UBSan; a sanitizer report fails the
test. Simulated software evidence only.
"""

from __future__ import annotations

import socket
import struct
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from tests.test_usb_proxy import (
    PROFILES,
    AltDevice,
    bot_write_refused_then_recovers,
    expected_bos,
    expected_config,
    expected_device,
    remote_answer,
)
from tests.usbsim.classes import CdcHost, HidHost, MassStorageHost
from tests.usbsim.device import DeviceModel
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
from tests.usbsim.host import HostProtocolError, HostTimeoutError, StallError
from tests.usbsim.native import BUSID, NativeRig, make_native_rig, wait_for
from tests.usbsim.udc import dwc3_like_endpoints

from yoke.usb import ch9
from yoke.usb.ch9 import Setup, Speed
from yoke.usb.control import Target

DEPTH = 16  # in_depth_bulk default, as ProxyPolicy


@contextmanager
def native(device: DeviceModel, **kwargs: Any) -> Iterator[NativeRig]:
    rig = make_native_rig(device, **kwargs)
    try:
        yield rig
    finally:
        rig.close()


@contextmanager
def enumerated(
    device: DeviceModel, profile: str = "linux", **kwargs: Any
) -> Iterator[NativeRig]:
    with native(device, **kwargs) as rig:
        rig.host.attach()
        rig.host.enumerate(profile)
        yield rig


def counter(rig: NativeRig, name: str, at_least: int, timeout: float = 3.0) -> None:
    """Status counters refresh every worker step (0.1 s), as the reference."""
    wait_for(lambda: rig.counters().get(name, 0) >= at_least, timeout)


def handle_of(rig: NativeRig, address: int) -> int:
    eps = rig.kernel._eps
    return next(i for i, e in enumerate(eps) if e.enabled and e.address == address)


# --- presentation ------------------------------------------------------------


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("name", sorted(ALL_DEVICES))
def test_enumeration_matches_device_modulo_documented_rewrites(
    name: str, profile: str
) -> None:
    device = ALL_DEVICES[name]()
    with native(device) as rig:
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
            assert seen.qualifier is None
        if seen.bos is not None:
            assert device.bos is not None and seen.bos == expected_bos(device.bos)
        assert rig.host.violations == [] and rig.host.deadline_misses == []
        assert device.violations == [] and rig.usbipd.violations == []
        assert device.configuration == 1 and rig.kernel.usb_state == "configured"
        assert rig.kernel.vbus_ma == 500
        status = rig.wait_state("configured")
        assert status.detail == "" and status.link_speed == SPEED_NAME[presented]


SPEED_NAME = {Speed.HIGH: "high-speed", Speed.FULL: "full-speed"}


def test_descriptor_reads_do_not_reach_the_device_after_import() -> None:
    device = CompositeDevice()
    with native(device) as rig:
        imported = len(device.control_log)
        rig.host.attach()
        rig.host.enumerate("linux")
        later = [s for s in device.control_log[imported:] if s.request == 6]
        assert later == []
        assert rig.host.records and max(r.elapsed for r in rig.host.records) < 0.05


def test_keyboard_class_traffic_and_remote_reset_restores_protocol() -> None:
    device = HidKeyboard()
    with enumerated(device) as rig:
        hid = HidHost(rig.host)
        assert hid.set_idle()
        report = device.class_descriptors[(0, 0x22, 0)]
        assert hid.report_descriptor(len(report)) == report
        device.press(0x04, modifiers=0x02)
        assert hid.read_report() == bytes([0x02, 0, 0x04, 0, 0, 0, 0, 0])
        device.release()
        assert hid.read_report() == bytes(8)
        hid.set_leds(0x02)
        wait_for(lambda: device.leds == 0x02)
        hid.set_protocol(0)
        assert hid.get_protocol() == 0
        rig.host.enumerate("windows")
        wait_for(lambda: device.resets >= 2)
        assert HidHost(rig.host).get_protocol() == 1
        assert "reset_device" in rig.usbipd.stats.tweaks


def test_mouse_reports() -> None:
    device = HidMouse()
    with enumerated(device, "macos") as rig:
        device.move(5, -3, buttons=1, wheel=1)
        assert HidHost(rig.host).read_report(4) == bytes([1, 5, 0xFD, 1])


@pytest.mark.parametrize("write_behind", [1, 4])
def test_mass_storage_bot_round_trip_and_failed_command(write_behind: int) -> None:
    device = MassStorage()
    options = ("--bot-write-behind", str(write_behind))
    with enumerated(device, "windows", options=options) as rig:
        storage = MassStorageHost(rig.host)
        assert storage.max_lun() == 0
        assert storage.read_capacity() == (2047, 512)
        payload = bytes((i * 7) & 0xFF for i in range(300 * 512))
        storage.write10(10, payload)
        assert bytes(device.disk[10 * 512 : 310 * 512]) == payload
        assert storage.read10(10, 300) == payload
        data, status = storage.command(bytes([0xFF]) + bytes(5), data_in=64)
        assert status.status == 1 and data == b""
        sense, ok = storage.command(bytes([0x03, 0, 0, 0, 18, 0]), data_in=18)
        assert ok.status == 0 and sense[2] & 0x0F == 0x05 and sense[12] == 0x20
        counter(rig, "remote_stalls", 1)
        assert rig.host.violations == []


@pytest.mark.parametrize("write_behind", [1, 4])
def test_mass_storage_refused_write_then_read_and_write_succeed(
    write_behind: int,
) -> None:
    """tests/test_usb_proxy.py's scenario: a data-out STALL ends the BOT plan."""
    device = MassStorage()
    options = ("--bot-write-behind", str(write_behind))
    with enumerated(device, options=options) as rig:
        storage = MassStorageHost(rig.host, timeout=2.0)
        bot_write_refused_then_recovers(storage, device)
        assert rig.host.violations == []


def test_cdc_acm_line_coding_notification_and_packet_exact_echo() -> None:
    device = CdcAcm()
    with enumerated(device) as rig:
        cdc = CdcHost(rig.host)
        cdc.set_line_coding(9600, bits=7)
        assert cdc.get_line_coding() == (9600, 0, 0, 7)
        cdc.set_control_lines(0x03)
        assert len(rig.host.transfer_in(0x83, 10)) == 10
        rig.host.transfer_out(0x02, b"a" * 64, zlp=True)
        assert rig.host.transfer_in(0x81, 128) == b"a" * 64
        rig.host.transfer_out(0x02, b"b" * 100)
        assert rig.host.transfer_in(0x81, 512) == b"b" * 100


def test_composite_vendor_requests_and_concurrent_interfaces() -> None:
    device = CompositeDevice()
    with enumerated(device, "windows") as rig:
        host = rig.host
        assert host.control(Setup(0xC0, 0x01, 0, 0, 64)) == b"YOKE-SIM-1.0"
        host.control(Setup(0x40, 0x02, 0, 0, 5), b"hello")
        assert host.control(Setup(0xC0, 0x03, 0, 0, 64)) == b"hello"
        host.control(Setup(0x40, 0x04, 0, 0, 0))
        assert device.pings == 1
        with pytest.raises(StallError):
            host.control(Setup(0xC0, 0x7F, 0, 0, 4))
        host.transfer_out(0x03, bytes(1024), zlp=True)
        assert host.transfer_in(0x82, 4096) == bytes(1024)
        device.press(0x05)
        assert HidHost(host).read_report()[2] == 0x05


def test_full_speed_only_device_and_short_packets() -> None:
    device = FullSpeedOnly()
    with native(device) as rig:
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
    with enumerated(device) as rig:
        host = rig.host
        with pytest.raises(StallError):
            host.control(Setup(0xC0, 0x10, 0, 0, 1))
        host.control(Setup(0x40, 0x11, 0x81, 1024, 0))
        with pytest.raises(StallError):
            host.transfer_in(0x81, 4096)
        assert host.get_status(ch9.RECIP_ENDPOINT, 0x81) == b"\x01\x00"
        host.clear_halt(0x81)
        assert host.transfer_in(0x81, 512) == b"RECOVERED"
        host.control(Setup(0x40, 0x11, 0x02, 0, 0))
        host.transfer_out(0x02, bytes(512))
        with pytest.raises(StallError):
            host.transfer_out(0x02, b"next")
        host.clear_halt(0x02)
        host.transfer_out(0x02, b"after")
        wait_for(lambda: device.received.endswith(b"after"))
        assert host.control(Setup(0xC0, 0x12, 0, 0, 1))[0] >= 2
        counter(rig, "remote_stalls", 2)
        time.sleep(0.25)
        counters = rig.counters()
        assert counters["remote_stalls"] == 2 and counters["local_stalls"] == 2
        assert rig.usbipd.stats.tweaks.count("clear_halt 0x81") >= 1


def test_device_disconnect_presents_disconnect_then_recovers() -> None:
    device = DisconnectingDevice()
    with enumerated(device) as rig:
        device.arm_disconnect(1024)
        with pytest.raises((HostProtocolError, HostTimeoutError, StallError)):
            rig.host.transfer_out(0x02, bytes(4096), timeout=2)
        status = rig.wait_state("connecting")
        assert "remote lost" in status.detail
        wait_for(lambda: not rig.kernel.bound)  # the computer sees it go away
        time.sleep(0.4)
        assert rig.status().state == "connecting"  # still unplugged: retrying
        device.replug()
        rig.wait_state("presenting", "attached")
        rig.host.attach()
        rig.host.enumerate("windows")
        device.emit(b"back")
        assert rig.host.transfer_in(0x81, 512) == b"back"
        assert rig.status().reconnects >= 1


def test_bus_reset_unlinks_outstanding_urbs_and_reenumerates() -> None:
    device = Loopback()
    with enumerated(device) as rig:
        wait_for(lambda: len(device.pipes[0x81].urbs) == DEPTH)
        rig.host.reset()
        wait_for(lambda: rig.usbipd.stats.unlinked >= DEPTH)
        rig.wait_state("default")
        rig.host.enumerate("macos")
        device.emit(b"ok")
        assert rig.host.transfer_in(0x81, 512) == b"ok"


def test_suspend_resume_and_host_disconnect() -> None:
    device = Loopback()
    with enumerated(device) as rig:
        rig.host.suspend()
        rig.wait_state("suspended")
        rig.host.resume()
        rig.wait_state("configured")
        device.emit(b"awake")
        assert rig.host.transfer_in(0x81, 512) == b"awake"
        rig.host.disconnect()
        rig.wait_state("disconnected")
        counter(rig, "disconnects", 1)
        rig.host.attach()
        rig.host.enumerate("windows")
        device.emit(b"again")
        assert rig.host.transfer_in(0x81, 512) == b"again"


def test_network_reset_presents_disconnect_and_reconnects() -> None:
    device = Loopback()
    with enumerated(device, latency=0.001, jitter=0.001) as rig:
        assert rig.relay is not None
        rig.relay.reset_all()
        wait_for(lambda: not rig.kernel.bound, 5)
        rig.wait_state("presenting", "attached")
        rig.host.attach()
        rig.host.enumerate("linux")
        rig.host.transfer_out(0x02, b"ping")
        assert rig.host.transfer_in(0x81, 512) == b"ping"


def test_exporter_restart_is_survived() -> None:
    device = MassStorage()
    with enumerated(device) as rig:
        rig.usbipd.stop()
        wait_for(lambda: not rig.kernel.bound, 5)
        rig.address = None  # the daemon restarts on a new listener
        rig.connect()
        rig.wait_state("presenting", "attached")
        rig.host.attach()
        rig.host.enumerate("windows")
        storage = MassStorageHost(rig.host)
        storage.write10(0, bytes(range(256)) * 4)
        assert storage.read10(0, 2) == bytes(range(256)) * 4


def test_latency_and_jitter_still_meet_control_deadlines() -> None:
    device = CompositeDevice()
    with native(device, latency=0.004, jitter=0.002) as rig:
        rig.host.attach()
        for profile in PROFILES:
            rig.host.enumerate(profile)
        assert rig.host.violations == []
        assert rig.host.control(Setup(0xC0, 0x01, 0, 0, 64)) == b"YOKE-SIM-1.0"


def test_round_trip_above_the_no_data_budget_misses_set_configuration() -> None:
    device = Loopback()
    with native(device, latency=0.03) as rig:
        rig.host.attach()
        rig.host.enumerate("linux")
        assert rig.host.violations == []
        assert any("request=9" in v for v in rig.host.deadline_misses)
        cached = [r for r in rig.host.records if r.setup.request == ch9.GET_DESCRIPTOR]
        assert cached and max(r.elapsed for r in cached) < ch9.NO_DATA_DEADLINE


def test_delegating_udc_standard_requests_are_handled_by_the_proxy() -> None:
    device = StallingDevice()
    with enumerated(device, "windows", udc_handles_standard=False) as rig:
        assert device.violations == []
        assert rig.host.get_status() == b"\0\0"
        assert rig.host.get_status(ch9.RECIP_ENDPOINT, 0x81) == b"\0\0"
        rig.host.clear_halt(0x81)
        assert "clear_halt 0x81" in rig.usbipd.stats.tweaks
        with pytest.raises(StallError):
            rig.host.control(Setup(0, ch9.SET_FEATURE, 1))


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
    with native(factory(), present=False) as rig:
        rig.connect()
        status = rig.wait_state("rejected")
        assert (
            status.detail.startswith("unsupported device") and reason in status.detail
        )
        assert not rig.kernel.bound


def test_udc_without_a_required_endpoint_rejects_after_binding() -> None:
    device = CompositeDevice()
    with native(
        device, udc_endpoints=dwc3_like_endpoints(count=2), present=False
    ) as rig:
        rig.connect()
        status = rig.wait_state("rejected")
        assert status.detail.startswith("UDC lacks endpoints: 0x03")
        wait_for(lambda: not rig.kernel.bound)


def test_link_slower_than_device_is_rejected() -> None:
    device = Loopback()
    with native(device, host_speed=Speed.FULL) as rig:
        rig.host.attach()
        with pytest.raises((StallError, HostProtocolError)):
            rig.host.enumerate("linux")
        status = rig.wait_state("rejected")
        assert status.detail.startswith("computer link is full-speed")


def test_set_interface_enables_and_disables_endpoints() -> None:
    device = AltDevice()
    with enumerated(device) as rig:
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
    with enumerated(device, options=("--out-window", "8"), latency=0.001) as rig:
        payload = bytes(i & 0xFF for i in range(512 * 40)) + b"end"
        rig.host.transfer_out(0x02, payload)
        assert rig.host.transfer_in(0x81, len(payload) + 512) == payload


def test_real_tcp_mass_storage_with_latency() -> None:
    device = MassStorage()
    with enumerated(device, "macos", latency=0.0005, jitter=0.0005) as rig:
        storage = MassStorageHost(rig.host)
        data = bytes((i * 13) & 0xFF for i in range(64 * 512))
        storage.write10(100, data)
        assert storage.read10(100, 64) == data
        assert rig.host.violations == []


def test_detach_is_confirmed_and_closes_the_gadget() -> None:
    device = Loopback()
    with enumerated(device) as rig:
        begun = time.monotonic()
        status = rig.route(None)
        assert status.state == "idle" and status.target is None
        assert time.monotonic() - begun < 2.0
        assert not rig.kernel.bound
        wait_for(lambda: rig.usbipd.active_connections == 0)
        assert rig.stop() == 0


def test_sigterm_with_threads_blocked_in_ioctls_exits_cleanly() -> None:
    device = MassStorage()
    with enumerated(device) as rig:
        time.sleep(0.3)  # every endpoint thread is now blocked in the fake kernel
        begun = time.monotonic()
        assert rig.stop() == 0
        assert time.monotonic() - begun < 3.0
        assert not rig.kernel.bound


# --- review findings (tests/test_usb_faults.py) ----------------------------


def test_udc_device_name_as_driver_name_fails_like_the_kernel() -> None:
    device = Loopback()
    options = ("--gadget-driver", "fcc00000.usb")
    with native(device, options=options, present=False) as rig:
        rig.connect()
        wait_for(lambda: "RUN" in rig.status().detail, 5)
        status = rig.status()
        assert status.state == "connecting" and "busy" in status.detail.lower()
        assert not rig.kernel.bound


def test_host_clear_halt_with_started_requests_is_survived() -> None:
    device = Loopback()
    with enumerated(device) as rig:
        out_handle = handle_of(rig, 0x02)
        wait_for(lambda: rig.kernel._eps[out_handle].request is not None)
        rig.host.clear_halt(0x02)
        counter(rig, "host_clear_halts", 1)
        rig.host.transfer_out(0x02, b"after clear")
        assert rig.host.transfer_in(0x81, 512) == b"after clear"
        device.emit(b"queued")
        in_handle = handle_of(rig, 0x81)
        wait_for(lambda: rig.kernel._eps[in_handle].request is not None)
        rig.host.clear_halt(0x81)
        assert rig.host.transfer_in(0x81, 512) == b"queued"
        assert "clear_halt 0x02" in rig.usbipd.stats.tweaks
        assert any("clear-halt EPIPE" in line for line in rig.kernel.log)
        assert rig.status().detail == ""


@pytest.mark.parametrize("mode", ["late-submit", "lost-completion"])
def test_unlink_race_orderings_keep_the_connection(mode: str) -> None:
    device = Loopback()
    with enumerated(device, unlink_mode=mode) as rig:
        wait_for(lambda: len(device.pipes[0x81].urbs) == DEPTH)
        rig.host.reset()
        wait_for(lambda: rig.usbipd.stats.unlinked >= DEPTH)
        if mode == "late-submit":
            counter(rig, "late_ret_submits", DEPTH)
        rig.host.enumerate("windows")
        device.emit(b"still connected")
        assert rig.host.transfer_in(0x81, 512) == b"still connected"
        assert rig.status().reconnects == 0


def test_persistent_stall_does_not_spin_and_reset_recovery_releases_it() -> None:
    device = MassStorage()
    with enumerated(device) as rig:
        rig.host.transfer_out(0x02, b"USBX" + bytes(27))  # invalid CBW
        time.sleep(0.2)
        before = rig.usbipd.stats.submits
        time.sleep(1.0)
        assert rig.usbipd.stats.submits - before < 30
        storage = MassStorageHost(rig.host)
        storage.reset_recovery()
        assert storage.inquiry()[:1] == b"\x00"
        assert rig.host.violations == []


def test_zero_length_in_requests_use_the_out_pipe() -> None:
    device = CompositeDevice()
    with enumerated(device) as rig:
        with pytest.raises(StallError):
            rig.host.control(Setup(0xC0, 0x7E, 0, 0, 0))
        assert not any("BOGUS" in v for v in rig.usbipd.violations)
        assert device.control_log[-1].request == 0x7E


def test_cbw_shaped_payload_in_data_phase_is_not_parsed() -> None:
    device = MassStorage()
    with enumerated(device) as rig:
        storage = MassStorageHost(rig.host)
        fake = struct.pack("<4sIIBBB16s", b"USBC", 99, 4096, 0x80, 0, 10, bytes(16))
        _, status = storage.command(bytes([0xEA]) + bytes(5), data_out=fake)
        assert status.status == 1
        assert storage.inquiry()[:1] == b"\x00"
        time.sleep(0.1)
        assert len(device.pipes[0x81].urbs) == 0  # no stale BOT sizes queued


def test_route_change_aborts_a_session_build_in_progress() -> None:
    device = Loopback()
    silent = socket.create_server(("127.0.0.1", 0))  # accepts, never answers
    try:
        with native(device, present=False) as rig:
            port = silent.getsockname()[1]
            rig.route(Target("127.0.0.1", port, BUSID, None))
            time.sleep(0.3)  # connected; the import reply never comes
            assert rig.status().state == "connecting"
            begun = time.monotonic()
            status = rig.route(None)
            assert time.monotonic() - begun < 1.0
            assert status.state == "idle"
    finally:
        silent.close()
