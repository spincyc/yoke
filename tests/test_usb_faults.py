"""Regression tests for the 2026-11-04 cold review of the USB data path.

Each test names the finding it pins. The simulated kernel now models the
behaviors the first simulation got wrong: dwc3's gadget name, signals that
are lost unless the thread is blocked, ``-errno`` results without retry,
EPIPE on a host clear-halt of a started request, dwc3 disconnect keeping
requests queued, late ep0 replies blocking, and stub_tx unlink ordering.
"""

from __future__ import annotations

import ctypes
import errno
import os
import signal
import socket
import struct
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from tests.usbsim.classes import CBW, MassStorageHost
from tests.usbsim.device import DeviceModel
from tests.usbsim.devices import CompositeDevice, Loopback, MassStorage
from tests.usbsim.harness import Rig, make_rig
from tests.usbsim.host import StallError
from tests.usbsim.streams import memory_pair
from tests.usbsim.udc import FakeRawGadgetKernel
from tests.usbsim.usbipd import FakeUsbipd
from yoke.drivers.base import DriverError
from yoke.drivers.gadget import UsbGadgetDriver
from yoke.usb import ch9, rawgadget, usbip
from yoke.usb.ch9 import Setup, Speed
from yoke.usb.client import ConnectionLostError, recv_exact
from yoke.usb.control import ControlClient, Route, Target, WorkerStatus
from yoke.usb.gadget import HEADROOM, GadgetError, GadgetInterruptedError
from yoke.usb.policy import ProxyPolicy
from yoke.usb.proxy import Proxy, _BotLink
from yoke.usb.worker import EXIT_CONFIG, BuildTicket, Worker
from yoke.usb.worker import main as worker_main

MEDIA = {"host": "192.168.7.10", "port": 3240, "busids": ["1-1"]}


@contextmanager
def rig_for(device: DeviceModel, **kwargs: Any) -> Iterator[Rig]:
    rig = make_rig(device, **kwargs)
    try:
        rig.host.attach()
        rig.host.enumerate("linux")
        yield rig
    finally:
        rig.close()


def wait_for(predicate: Any, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        time.sleep(0.002)


# --- finding 1: Raw Gadget driver_name is the gadget name ---------------------


def test_udc_device_name_as_driver_name_fails_like_the_kernel() -> None:
    kernel = FakeRawGadgetKernel()  # dwc3: gadget "dwc3-gadget", UDC fcc00000.usb
    backend = rawgadget.RawGadgetBackend(
        kernel.name,
        driver_name=kernel.name,
        opener=kernel.open,
        ioctl=kernel.ioctl,
        closer=kernel.close,
        require_signal_handler=False,
    )
    with pytest.raises(GadgetError, match="wrong gadget name") as failed:
        backend.start(Speed.HIGH)
    assert failed.value.errno == errno.EBUSY and not kernel.bound
    kernel.backend().start(Speed.HIGH)  # dwc3-gadget + fcc00000.usb binds
    assert kernel.bound


def test_gadget_name_is_inferred_from_the_udc_platform_driver(tmp_path: Path) -> None:
    for driver, udc, expected in (
        ("dwc3", "fcc00000.usb", "dwc3-gadget"),
        ("dummy_udc", "dummy_udc.0", "dummy_udc"),
        ("dwc2", "20980000.usb", "20980000.usb"),
    ):
        drivers = tmp_path / "bus" / driver
        drivers.mkdir(parents=True)
        device = tmp_path / "devices" / udc
        device.mkdir(parents=True)
        (device / "driver").symlink_to(drivers)
        link = tmp_path / "udc" / udc
        link.mkdir(parents=True)
        (link / "device").symlink_to(device)
        assert rawgadget.gadget_driver_name(udc, tmp_path / "udc") == expected
    assert rawgadget.gadget_driver_name("missing", tmp_path / "udc") == "missing"


# --- finding 2: cancellation must reach the real syscall ---------------------


def test_production_call_path_returns_eintr_from_a_real_blocked_syscall() -> None:
    """ctypes call-once + pthread_kill(SIGUSR1) unblocks a worker thread.

    CPython's fcntl.ioctl/os.read retry EINTR (PEP 475) when the Python
    handler returns normally, so this path must not go through them.
    """
    previous = signal.signal(signal.SIGUSR1, lambda _s, _f: None)
    read_fd, write_fd = os.pipe()
    libc = ctypes.CDLL(None, use_errno=True)
    libc.read.restype = ctypes.c_ssize_t
    libc.read.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t]
    buffer = ctypes.create_string_buffer(8)
    results: list[int] = []
    thread = threading.Thread(
        target=lambda: results.append(
            rawgadget.errno_call(libc.read, read_fd, buffer, 8)
        )
    )
    try:
        thread.start()
        time.sleep(0.05)
        assert thread.is_alive()  # really blocked in read(2)
        deadline = time.monotonic() + 5
        while thread.is_alive() and time.monotonic() < deadline:
            assert thread.ident is not None
            rawgadget._default_interrupt(thread.ident)
            thread.join(0.02)
        assert results == [-errno.EINTR]
    finally:
        os.write(write_fd, b"x")
        thread.join(1)
        os.close(read_fd)
        os.close(write_fd)
        signal.signal(signal.SIGUSR1, previous)


def test_libc_ioctl_reports_kernel_results() -> None:
    ioctl = rawgadget.LibcIoctl()
    left, right = socket.socketpair()
    try:
        right.sendall(b"abc")
        time.sleep(0.01)
        count = bytearray(4)
        assert ioctl(left.fileno(), 0x541B, count) == 0  # FIONREAD
        assert struct.unpack("i", count)[0] == 3
        assert ioctl(left.fileno(), 0x5401, bytearray(64)) == -errno.ENOTTY
    finally:
        left.close()
        right.close()


def test_dispatch_never_retries_eintr() -> None:
    calls: list[int] = []

    def eintr_once(fd: int, request: int, arg: int | bytearray) -> int:
        calls.append(request)
        return -errno.EINTR

    backend = rawgadget.RawGadgetBackend(
        "udc", driver_name="udc", ioctl=eintr_once, require_signal_handler=False
    )
    backend._fd = 3
    with pytest.raises(GadgetInterruptedError):
        backend.ep_read(1, bytearray(HEADROOM + 8), 8)
    assert calls == [rawgadget.USB_RAW_IOCTL_EP_READ]


def test_lost_interrupts_are_repeated_until_teardown_is_confirmed() -> None:
    device = Loopback()
    with rig_for(device) as rig:
        assert rig.proxy is not None
        kernel = rig.kernel
        original = kernel.interrupt
        dropped = 0

        def lossy(ident: int) -> None:  # the first signals land outside ioctl
            nonlocal dropped
            if dropped < 5:
                dropped += 1
                return
            original(ident)

        kernel.interrupt = lossy  # type: ignore[method-assign]
        rig.proxy.stop()
        assert rig.proxy.state == "stopped" and not kernel.bound


# --- finding 3: host CLEAR_FEATURE(HALT) on a started request -> EPIPE --------


def test_host_clear_halt_with_started_requests_is_survived() -> None:
    device = Loopback()
    with rig_for(device) as rig:
        assert rig.proxy is not None
        out_handle = rig.proxy._handles[0x02]
        wait_for(lambda: rig.kernel._eps[out_handle].request is not None)  # EP_READ
        rig.host.clear_halt(0x02)  # e.g. libusb_clear_halt on an idle pipe
        wait_for(
            lambda: rig.proxy is not None and rig.proxy.counters.host_clear_halts >= 1
        )
        rig.host.transfer_out(0x02, b"after clear")
        assert rig.host.transfer_in(0x81, 512) == b"after clear"
        device.emit(b"queued")
        in_handle = rig.proxy._handles[0x81]
        wait_for(lambda: rig.kernel._eps[in_handle].request is not None)  # EP_WRITE
        rig.host.clear_halt(0x81)
        assert rig.host.transfer_in(0x81, 512) == b"queued"  # re-written
        assert rig.proxy.error is None
        assert "clear_halt 0x02" in rig.usbipd.stats.tweaks
        assert any("clear-halt EPIPE" in line for line in rig.kernel.log)


# --- finding 4: stub_tx ordering of RET_SUBMIT and RET_UNLINK ----------------


@pytest.mark.parametrize("mode", ["late-submit", "lost-completion"])
def test_unlink_race_orderings_keep_the_connection(mode: str) -> None:
    device = Loopback()
    with rig_for(device, unlink_mode=mode) as rig:
        assert rig.proxy is not None
        depth = rig.policy.in_depth_bulk
        wait_for(lambda: len(device.pipes[0x81].urbs) == depth)
        rig.host.reset()  # quiesce: unlink every outstanding IN URB
        wait_for(lambda: rig.usbipd.stats.unlinked >= depth)
        assert rig.proxy.conn.lost is None and not rig.proxy.ended.is_set()
        if mode == "late-submit":
            wait_for(
                lambda: (
                    rig.proxy is not None and rig.proxy.conn.late_ret_submits >= depth
                )
            )
        rig.host.enumerate("windows")
        device.emit(b"still connected")
        assert rig.host.transfer_in(0x81, 512) == b"still connected"


def test_late_ret_submit_after_ret_unlink_zero_delivers_data() -> None:
    from tests.test_usb_wire import ScriptedExporter, connect

    exporter = ScriptedExporter()
    conn = connect(exporter)
    urb = conn.submit(1, usbip.DIR_IN, 64)
    cmd = exporter.read_command()
    canceller = threading.Thread(target=lambda: conn.cancel([urb], timeout=2))
    canceller.start()
    unlink = exporter.read_command()
    exporter.server.sendall(usbip.RetUnlink(unlink.seqnum, 0).pack())
    exporter.server.sendall(usbip.RetSubmit(cmd.seqnum, 0, 4).pack() + b"data")
    canceller.join(2)
    assert urb.wait(1).data == b"data" and conn.lost is None
    assert conn.late_ret_submits == 1
    conn.close()


# --- finding 5: a device that stays halted must not cause a submit storm -----


@pytest.mark.parametrize("bot_sizing", [False, True])
def test_persistent_stall_does_not_spin_and_reset_recovery_releases_it(
    bot_sizing: bool,
) -> None:
    device = MassStorage()
    with rig_for(device, policy=ProxyPolicy(bot_sizing=bot_sizing)) as rig:
        assert rig.proxy is not None
        rig.host.transfer_out(0x02, b"USBX" + bytes(27))  # invalid CBW
        time.sleep(0.2)
        before = rig.usbipd.stats.submits
        time.sleep(1.0)
        assert rig.usbipd.stats.submits - before < 30
        storage = MassStorageHost(rig.host)
        storage.reset_recovery()
        assert storage.inquiry()[:1] == b"\x00"
        assert rig.host.violations == []


# --- finding 7: IN requests without data travel as OUT URBs ------------------


def test_zero_length_in_requests_use_the_out_pipe() -> None:
    device = CompositeDevice()
    with rig_for(device) as rig:
        with pytest.raises(StallError):
            rig.host.control(Setup(0xC0, 0x7E, 0, 0, 0))
        assert not any("BOGUS" in v for v in rig.usbipd.violations)
        assert device.control_log[-1].request == 0x7E


# --- finding 8: CBW-shaped data inside a data-out phase ------------------------


def test_cbw_shaped_payload_in_data_phase_is_not_parsed() -> None:
    device = MassStorage()
    with rig_for(device) as rig:
        assert rig.proxy is not None
        storage = MassStorageHost(rig.host)
        fake = struct.pack("<4sIIBBB16s", b"USBC", 99, 4096, 0x80, 0, 10, bytes(16))
        _, status = storage.command(bytes([0xEA]) + bytes(5), data_out=fake)
        assert status.status == 1
        assert storage.inquiry()[:1] == b"\x00"
        pump = rig.proxy._pumps[0x81]
        wait_for(lambda: not pump.inflight)
        assert pump.bot is not None and not pump.bot.in_sizes


# --- finding 9: the driver adopts only the generation it sent ----------------


class StaleClient(ControlClient):
    def __init__(self) -> None:
        super().__init__("\0unused", timeout=1)
        self.current = WorkerStatus(generation=4, state="idle", updated=time.time())

    def status(self) -> WorkerStatus:
        return self.current

    def route(self, route: Route) -> WorkerStatus:
        # Another writer won the race: same target, different generation.
        return WorkerStatus(
            generation=route.generation + 1,
            state="connecting",
            target=route.target,
            updated=time.time(),
        )


def test_driver_rejects_an_acknowledgment_for_another_generation() -> None:
    driver = UsbGadgetDriver(udc="u", client=StaleClient())
    with pytest.raises(DriverError, match="did not adopt route 5"):
        driver.attach("prod", MEDIA)


# --- finding 10: route changes never wait for a slow import -------------------


def test_apply_aborts_a_session_build_in_progress() -> None:
    started = threading.Event()
    outcome: list[BaseException] = []

    def slow_factory(target: Target, ticket: BuildTicket) -> Proxy:
        client, _server, _link = memory_pair()
        ticket.register(client)
        started.set()
        try:
            recv_exact(client, memoryview(bytearray(8)))  # import never answered
        except (ConnectionLostError, OSError) as exc:
            outcome.append(exc)
            raise ConnectionLostError("aborted") from exc
        raise AssertionError("unreachable")

    worker = Worker(slow_factory, retry_min=10)
    worker.apply(Route(1, Target("10.0.0.2", 3240, "1-1")))
    builder = threading.Thread(target=worker.step)
    builder.start()
    assert started.wait(2)
    begun = time.monotonic()
    status = worker.apply(Route(2, None))
    assert time.monotonic() - begun < 1.0
    assert status.generation == 2 and status.state == "idle"
    builder.join(2)
    assert outcome  # the blocked import was aborted, not left to time out


# --- simulation fidelity ------------------------------------------------------


def test_late_ep0_reply_blocks_until_bus_reset() -> None:
    kernel = FakeRawGadgetKernel()
    backend = kernel.backend()
    backend.start(Speed.HIGH)
    assert backend.fetch_event().type.name == "CONNECT"
    kernel.host_attach(Speed.HIGH)
    result = kernel.host_control(ch9.get_descriptor(1, 0, 18), b"", 0.01)
    assert result.outcome == "timeout"  # the host gave up
    assert backend.fetch_event().setup is not None
    errors: list[BaseException] = []

    def late() -> None:
        try:
            backend.ep0_write(bytes(18), False)
        except GadgetError as exc:
            errors.append(exc)

    replier = threading.Thread(target=late)
    replier.start()
    time.sleep(0.05)
    assert replier.is_alive()  # queued reply never completes
    assert (
        kernel.host_control(ch9.get_descriptor(1, 0, 18), b"", 0.05).outcome == "stall"
    )
    kernel.host_reset()
    replier.join(1)
    assert errors and errors[0].errno == errno.ECONNRESET


def test_dwc3_disconnect_keeps_requests_queued() -> None:
    kernel = FakeRawGadgetKernel()
    backend = kernel.backend()
    backend.start(Speed.HIGH)
    kernel.host_attach(Speed.HIGH)
    handle = backend.ep_enable(bytes([7, 5, 0x02, 2, 0, 2, 0]))
    errors: list[BaseException] = []

    def read() -> None:
        try:
            backend.ep_read(handle, bytearray(HEADROOM + 512), 512)
        except GadgetError as exc:
            errors.append(exc)

    reader = threading.Thread(target=read)
    reader.start()
    wait_for(lambda: kernel._eps[handle].request is not None)
    kernel.host_disconnect()
    time.sleep(0.05)
    assert reader.is_alive()  # only an interrupt (or reset) releases it
    assert reader.ident is not None
    backend.interrupt(reader.ident)
    reader.join(1)
    assert isinstance(errors[0], GadgetInterruptedError)


def raw_import(usbipd: FakeUsbipd) -> Any:
    client, server, _ = memory_pair()
    usbipd.spawn(server)
    client.sendall(usbip.encode_req_import("1-1"))
    recv_exact(client, memoryview(bytearray(8 + usbip.DEVICE_INFO_SIZE)))
    return client


def test_exporter_desynchronizes_on_a_bad_endpoint_like_the_stub() -> None:
    usbipd = FakeUsbipd({"1-1": Loopback()})
    client = raw_import(usbipd)
    devid = (1 << 16) | 2
    bad = usbip.CmdSubmit(1, devid, usbip.DIR_OUT, 7, 0, 48).pack()
    client.sendall(bad + bytes(48))  # payload is parsed as the next header
    wait_for(lambda: usbipd.active_connections == 0)
    assert any("endpoint 0x07" in v for v in usbipd.violations)


def test_exporter_enforces_control_length_and_warns_on_direction() -> None:
    usbipd = FakeUsbipd({"1-1": Loopback()})
    client = raw_import(usbipd)
    devid = (1 << 16) | 2
    setup = Setup(0xC0, 0x01, 0, 0, 0).pack()
    client.sendall(
        usbip.CmdSubmit(1, devid, usbip.DIR_IN, 0, 0x200, 0, setup=setup).pack()
    )
    wrong_len = Setup(0x80, 6, 0x0100, 0, 18).pack()
    client.sendall(
        usbip.CmdSubmit(2, devid, usbip.DIR_IN, 0, 0x200, 64, setup=wrong_len).pack()
    )
    wait_for(lambda: usbipd.active_connections == 0)
    assert any("BOGUS control dir" in v for v in usbipd.violations)
    assert any("BOGUS control len" in v for v in usbipd.violations)


# --- 2026-11-05 lane A review ---------------------------------------------------

WRITE_128K = struct.pack(">BBIBHB", 0x2A, 0, 20, 0, 256, 0)  # two 64 KiB BOT URBs


def test_bot_link_discard_keeps_only_the_csw() -> None:
    link = _BotLink(0)
    link.push_in([65536, 65536, 512])
    link.plan_out([65536, 65536])
    _, epoch = link.pop_out()
    link.discard()
    assert list(link.in_sizes) == [512] and not link.out_sizes
    assert not link.current(epoch) and link.pop_out() == (None, epoch + 1)
    link.in_sizes.clear()
    link.discard()
    assert not link.in_sizes


def test_bot_plan_is_discarded_after_a_data_out_stall() -> None:
    """Port of repro.c test_bot_stale_plan: WRITE with 128 KiB data-out, the
    first 64 KiB URB stalled, clear-halt, CSW, then a READ CBW, which must
    plan IN URBs instead of being read as the stale second data URB."""
    device = MassStorage()
    device.refuse_data_out = 1
    with rig_for(device) as rig:
        assert rig.proxy is not None
        storage = MassStorageHost(rig.host, timeout=2.0)
        pump = rig.proxy._pumps[0x02]
        storage.tag += 1
        cbw = CBW.pack(b"USBC", storage.tag, 131072, 0, 0, 10, WRITE_128K.ljust(16))
        rig.host.transfer_out(0x02, cbw)
        with pytest.raises(StallError):
            rig.host.transfer_out(0x02, bytes(131072))
        assert pump.bot is not None
        wait_for(lambda: pump.bot is not None and not pump.bot.out_sizes)
        rig.host.clear_halt(0x02)
        status = storage._csw()
        assert (status.status, status.residue) == (1, 131072)
        assert storage.read10(0, 8) == bytes(device.disk[: 8 * 512])
        assert rig.proxy.counters.remote_stalls >= 1


def test_bot_reset_discards_a_plan_whose_read_is_in_progress() -> None:
    """Trigger (c): the class reset ends a data-out plan, including the planned
    read the OUT pump already has queued (no clear-halt EPIPE here)."""
    device = MassStorage()
    with rig_for(device, clear_halt_cancels=False) as rig:
        assert rig.proxy is not None
        storage = MassStorageHost(rig.host, timeout=2.0)
        handle = rig.proxy._handles[0x02]
        storage.tag += 1
        cbw = CBW.pack(b"USBC", storage.tag, 131072, 0, 0, 10, WRITE_128K.ljust(16))
        rig.host.transfer_out(0x02, cbw)
        wait_for(
            lambda: (
                (r := rig.kernel._eps[handle].request) is not None
                and r.capacity == 65536
            )
        )  # the planned data-out read is queued
        storage.reset_recovery()  # the host abandons the WRITE
        assert storage.read10(0, 8) == bytes(device.disk[: 8 * 512])
        storage.write10(4, bytes(range(256)) * 4)
        assert bytes(device.disk[4 * 512 : 6 * 512]) == bytes(range(256)) * 4


def test_get_status_reports_a_locally_halted_endpoint_until_cleared() -> None:
    device = MassStorage()
    with rig_for(device, udc_handles_standard=False) as rig:
        assert rig.proxy is not None
        storage = MassStorageHost(rig.host)
        assert rig.host.get_status(ch9.RECIP_ENDPOINT, 0x81) == b"\0\0"
        storage.tag += 1
        bad = bytes([0xFF]) + bytes(5)  # unknown opcode: data-in fails by STALL
        rig.host.transfer_out(
            0x02, CBW.pack(b"USBC", storage.tag, 64, 0x80, 0, 6, bad.ljust(16))
        )
        with pytest.raises(StallError):
            rig.host.transfer_in(0x81, 64)
        assert rig.host.get_status(ch9.RECIP_ENDPOINT, 0x81) == b"\x01\0"
        assert rig.host.get_status(ch9.RECIP_ENDPOINT, 0x02) == b"\0\0"
        rig.host.clear_halt(0x81)  # delegated: the proxy handles it
        assert rig.host.get_status(ch9.RECIP_ENDPOINT, 0x81) == b"\0\0"
        rig.proxy._control.set_halted(0x81, True)
        rig.host.reset()  # a bus reset clears every halt bit
        wait_for(lambda: rig.proxy is not None and rig.proxy.counters.bus_resets >= 3)
        assert not rig.proxy._control.halted


VENDOR_IN = Setup(0xC0, 0x01, 0, 0, 64)


@pytest.mark.parametrize("event", ["reset", "disconnect"])
def test_first_reenumeration_after_a_bus_event_mid_forward_succeeds(
    event: str,
) -> None:
    """Reviewer reproducer (test_review_ep0.py, Python half): the host gives up
    on a forwarded request, then resets or disconnects before the remote
    answers. Raw Gadget keeps that SETUP owed, so the proxy stalls it."""
    device = CompositeDevice()
    with rig_for(device) as rig:
        assert rig.proxy is not None
        rig.usbipd.service_delay = 0.3  # the remote answers slowly
        result = rig.kernel.host_control(VENDOR_IN, b"", 0.02)
        assert result.outcome == "timeout"  # the host gave up; SETUP still owed
        getattr(rig.host, event)()
        time.sleep(0.05)
        rig.usbipd.service_delay = 0.0
        time.sleep(0.8)  # the remote answer arrives and is ignored
        if event == "disconnect":
            rig.host.attach()
        rig.host.enumerate("linux")  # the first attempt
        assert any("abandoned the request" in line for line in rig.logs)
        assert rig.proxy.error is None


def test_teardown_does_not_wait_for_unlinks_a_stub_never_confirms() -> None:
    """Teardown closes the connection (the stub kills every URB on close)
    instead of unlinking each outstanding URB, as the native worker does."""
    device = Loopback()
    with rig_for(device, unlink_mode="ignore") as rig:
        assert rig.proxy is not None
        wait_for(lambda: len(device.pipes[0x81].urbs) == rig.policy.in_depth_bulk)
        begun = time.monotonic()
        rig.proxy.stop()
        assert time.monotonic() - begun < 0.5
        assert rig.proxy.state == "stopped" and rig.proxy.error is None
        assert rig.usbipd.stats.unlinks == 0


def test_missing_native_binary_exits_with_ex_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "yoke-gadget-worker"
    code = worker_main(
        ["--udc", "u", "--implementation", "native", "--native-binary", str(missing)]
    )
    assert code == EXIT_CONFIG == 78
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and str(missing) in err and "Traceback" not in err
