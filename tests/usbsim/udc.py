"""A simulated Raw Gadget kernel and UDC, driven through the real ioctl ABI.

:class:`FakeRawGadgetKernel` supplies ``opener``/``ioctl``/``closer``/
``interrupter``/``read_text`` to :class:`yoke.usb.rawgadget.RawGadgetBackend`,
so the backend's struct packing and error mapping run unchanged. It applies
the checks of ``drivers/usb/gadget/legacy/raw_gadget.c``:

* a CONTROL event marks endpoint zero IN-pending (IN with wLength > 0) or
  OUT-pending; a second SETUP while one is pending is STALLed (EBUSY);
* EP0/endpoint I/O in the wrong direction or with a request already queued
  fails with EBUSY/EINVAL; disabling or halting an endpoint with a queued
  request fails with EINVAL; the event queue holds 16 events;
* blocked calls return EINTR when the calling thread is interrupted.

The UDC half models a dwc3-like controller (⚠ assumption, see ADR 0021):
it answers SET_ADDRESS, device/endpoint GET_STATUS and CLEAR/SET_FEATURE
itself (``udc_handles_standard=False`` delegates them, like a minimal UDC),
completes the status stage of an OUT request with data automatically,
cancels queued transfers with ESHUTDOWN on bus reset and disconnect, and
keeps a halted endpoint's queued requests until the host clears the halt.
"""

from __future__ import annotations

import errno
import os
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from tests.usbsim.packets import BabbleError, Receiver, packetize
from yoke.usb import ch9, rawgadget
from yoke.usb.ch9 import SPEED_NAMES, Setup, Speed
from yoke.usb.gadget import HEADROOM, EventType, UdcEndpoint

EVENT_QUEUE_SIZE = 16
DEVICE_PATH = rawgadget.DEVICE_PATH


def dwc3_like_endpoints(count: int = 7, limit: int = 1024) -> list[UdcEndpoint]:
    """Fixed-number endpoints ep1in..epNin / ep1out..epNout (⚠ RK3566 count)."""
    eps = []
    for number in range(1, count + 1):
        for dir_in in (True, False):
            eps.append(
                UdcEndpoint(
                    name=f"ep{number}{'in' if dir_in else 'out'}",
                    number=number,
                    control=False,
                    iso=True,
                    bulk=True,
                    interrupt=True,
                    dir_in=dir_in,
                    dir_out=not dir_in,
                    maxpacket_limit=limit,
                )
            )
    return eps


@dataclass
class _Request:
    data: bytes = b""
    zero: bool = False
    capacity: int = 0
    packets: deque[bytes] = field(default_factory=deque)
    receiver: Receiver | None = None
    status: int | None = None  # bytes transferred, or -errno
    thread: int = 0
    started: bool = True  # dwc3 defers requests queued on a halted endpoint


@dataclass
class _Ep:
    info: UdcEndpoint
    enabled: bool = False
    address: int = 0
    transfer_type: int = 0
    packet_size: int = 0
    halted: bool = False
    wedged: bool = False
    request: _Request | None = None


@dataclass
class ControlResult:
    outcome: str  # "data", "ack", "stall", "timeout"
    data: bytes = b""
    elapsed: float = 0.0
    zero: bool = False


class FakeRawGadgetKernel:
    def __init__(
        self,
        *,
        name: str = "fcc00000.usb",
        gadget_name: str = "dwc3-gadget",
        max_speed: Speed = Speed.HIGH,
        endpoints: list[UdcEndpoint] | None = None,
        ep0_size: int = 64,
        udc_handles_standard: bool = True,
        sysfs_root: Path | None = None,
        clear_halt_cancels: bool = True,
    ) -> None:
        self.name = name
        self.gadget_name = gadget_name
        self.max_speed = max_speed
        self.ep0_size = ep0_size
        self.udc_handles_standard = udc_handles_standard
        self.sysfs_root = sysfs_root
        self.cond = threading.Condition()
        self._eps = [_Ep(info) for info in (endpoints or dwc3_like_endpoints())]
        self._fd: int | None = None
        self._next_fd = 100
        self.dev_state = "closed"
        self.requested_speed = Speed.UNKNOWN
        self.bound = False
        self.busy_with_other_gadget = False
        self.events: deque[tuple[int, bytes]] = deque()
        self.ep0_in_pending = False
        self.ep0_out_pending = False
        self.ep0_queued = False
        self._ep0_setup: Setup | None = None
        self._ep0_data_out = b""
        self._ep0_result: ControlResult | None = None
        self._ep0_waiting = False
        self.clear_halt_cancels = clear_halt_cancels
        self.interrupted: set[int] = set()
        self._blocked: set[int] = set()
        self._ep0_late = False
        self._bus_epoch = 0
        # Test hook: an EP0_READ with a data stage returns to the gadget this
        # many seconds after the host saw its status stage complete, so the
        # host's next SETUP can be delivered first (as on a loaded board).
        self.ep0_read_delay = 0.0
        self.link_speed = Speed.UNKNOWN
        self.usb_state = "not attached"
        self.address = 0
        self.remote_wakeup = False
        self.vbus_ma = 0
        self.configure_calls = 0
        self.ioctl_calls = 0
        self.log: list[str] = []
        self._init_names: tuple[str, str] = ("", "")
        self._previous_state = "configured"
        self._write_sysfs()

    # --- OS touchpoints for RawGadgetBackend -------------------------------

    def open(self, path: str, flags: int) -> int:
        if path != DEVICE_PATH:
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), path)
        if flags & os.O_NONBLOCK:
            raise OSError(errno.EINVAL, "raw-gadget does not support O_NONBLOCK")
        with self.cond:
            if self._fd is not None:
                raise OSError(errno.EBUSY, "one simulated raw-gadget instance")
            self._fd = self._next_fd
            self._next_fd += 1
            self.dev_state = "opened"
            return self._fd

    def close(self, fd: int) -> None:
        with self.cond:
            if fd != self._fd:
                raise OSError(errno.EBADF, "bad fd")
            self._fd = None
            self.dev_state = "closed"
            self.bound = False
            self._link_down("not attached")
            for ep in self._eps:
                ep.enabled = False
                ep.halted = ep.wedged = False
            self.events.clear()
            self.ep0_in_pending = self.ep0_out_pending = self.ep0_queued = False
            self.cond.notify_all()

    def interrupt(self, thread_ident: int) -> None:
        """A signal: interrupts the thread only while it is blocked here.

        Like a real signal aimed at a thread that is not inside the syscall,
        an interrupt that arrives at any other time is lost, so callers must
        repeat it until the thread leaves.
        """
        with self.cond:
            if thread_ident in self._blocked:
                self.interrupted.add(thread_ident)
                self.cond.notify_all()

    def read_text(self, path: Path) -> str:
        with self.cond:
            if path.name == "current_speed":
                return SPEED_NAMES[self.link_speed] + "\n"
            if path.name == "state":
                return self.usb_state + "\n"
        raise FileNotFoundError(errno.ENOENT, "no such sysfs attribute", str(path))

    def backend(self) -> rawgadget.RawGadgetBackend:
        return rawgadget.RawGadgetBackend(
            self.name,
            driver_name=self.gadget_name,
            opener=self.open,
            ioctl=self.ioctl,
            closer=self.close,
            interrupter=self.interrupt,
            read_text=self.read_text,
            require_signal_handler=False,
        )

    # --- helpers -----------------------------------------------------------

    def _write_sysfs(self) -> None:
        if self.sysfs_root is None:
            return
        directory = self.sysfs_root / self.name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "state").write_text(self.usb_state + "\n")
        (directory / "current_speed").write_text(SPEED_NAMES[self.link_speed] + "\n")

    def _set_state(self, state: str) -> None:
        self.usb_state = state
        self._write_sysfs()

    def _queue_event(self, kind: EventType, data: bytes = b"") -> None:
        if len(self.events) >= EVENT_QUEUE_SIZE:
            self.dev_state = "failed"
            self.log.append("event queue overflow")
            return
        self.events.append((int(kind), data))
        self.cond.notify_all()

    def _wait(self, ready: Callable[[], bool]) -> None:
        """Wait (lock held) until ``ready()`` or interruption (EINTR)."""
        me = threading.get_ident()
        self._blocked.add(me)
        try:
            while not ready():
                if me in self.interrupted:
                    raise OSError(errno.EINTR, "interrupted")
                self.cond.wait()
        finally:
            self._blocked.discard(me)
            self.interrupted.discard(me)

    def _running(self) -> None:
        if self.dev_state != "running":
            raise OSError(errno.EINVAL, "device is not running")

    def _cancel_requests(self, status: int) -> None:
        for ep in self._eps:
            if ep.request is not None and ep.request.status is None:
                ep.request.status = status
        self.cond.notify_all()

    def _link_down(self, state: str, *, cancel: bool = True) -> None:
        if cancel:
            self._cancel_requests(-errno.ESHUTDOWN)
        self._bus_epoch += 1
        self.link_speed = Speed.UNKNOWN
        self.address = 0
        self._abandon_ep0()
        self._set_state(state)

    def _abandon_ep0(self) -> None:
        if self._ep0_waiting and self._ep0_result is None:
            self._ep0_result = ControlResult("timeout")
        self._ep0_waiting = False

    # --- ioctl ----------------------------------------------------------------

    def ioctl(self, fd: int, request: int, arg: int | bytearray) -> int:
        """The injected :data:`~yoke.usb.rawgadget.RawIoctl`: ``-errno``, no raise."""
        with self.cond:
            self.ioctl_calls += 1
            if fd != self._fd:
                return -errno.EBADF
            handler = _HANDLERS.get(request)
            if handler is None:
                return -errno.ENOTTY
            try:
                return handler(self, arg)
            except OSError as exc:
                return -(exc.errno or errno.EIO)

    def _init(self, arg: int | bytearray) -> int:
        assert isinstance(arg, bytearray)
        if len(arg) < rawgadget.SIZEOF_INIT:
            raise OSError(errno.EFAULT, "short usb_raw_init")
        driver, device, speed = rawgadget.unpack_init(arg[: rawgadget.SIZEOF_INIT])
        if self.dev_state != "opened":
            raise OSError(errno.EINVAL, "device is not opened")
        if speed not in (0, 1, 2, 3, 5):
            raise OSError(errno.EINVAL, "bad speed")
        self.requested_speed = Speed(speed or Speed.HIGH)
        self._init_names = (driver, device)
        self.dev_state = "initialized"
        return 0

    def _run(self, arg: int | bytearray) -> int:
        if arg != 0:
            raise OSError(errno.EINVAL, "RUN takes no argument")
        if self.dev_state != "initialized":
            raise OSError(errno.EINVAL, "device is not initialized")
        if self._init_names != (self.gadget_name, self.name):
            # gadget_bind() returns -ENODEV, so match_existing_only makes
            # usb_gadget_register_driver() report -EBUSY.
            self.log.append(f"RUN: no UDC matches {self._init_names}")
            raise OSError(errno.EBUSY, "couldn't find an available UDC")
        if self.busy_with_other_gadget:
            raise OSError(errno.EBUSY, "UDC is bound to another gadget")
        self.dev_state = "running"
        self.bound = True
        self._queue_event(EventType.CONNECT)
        return 0

    def _event_fetch(self, arg: int | bytearray) -> int:
        assert isinstance(arg, bytearray)
        self._running()
        length = int.from_bytes(arg[4:8], "little")
        self._wait(lambda: bool(self.events) or self.dev_state != "running")
        if self.dev_state != "running":
            raise OSError(errno.EINVAL, "device is not running")
        kind, data = self.events.popleft()
        take = data[:length]
        arg[: 8 + len(take)] = rawgadget.pack_event(kind, data)[: 8 + len(take)]
        return 0

    def _ep0_io(self, arg: int | bytearray, is_in: bool) -> int:
        assert isinstance(arg, bytearray)
        _ep, flags, length = rawgadget.unpack_ep_io(arg)
        self._running()
        if self.ep0_queued:
            raise OSError(errno.EBUSY, "ep0 urb already queued")
        if (is_in and not self.ep0_in_pending) or (
            not is_in and not self.ep0_out_pending
        ):
            raise OSError(errno.EBUSY, "ep0 wrong direction")
        setup = self._ep0_setup
        if not self._ep0_waiting or setup is None:
            # The host abandoned this transfer: the reply is queued but no
            # token will ever complete it, so the pending flags stay set (and
            # later SETUPs stall) until a bus reset flushes ep0 or a signal.
            self.ep0_queued = True
            epoch = self._bus_epoch
            try:
                self._wait(
                    lambda: self._bus_epoch != epoch or self.dev_state != "running"
                )
            finally:
                self.ep0_queued = False
                self.ep0_in_pending = self.ep0_out_pending = False
            self.log.append("late ep0 reply flushed")
            raise OSError(errno.ECONNRESET, "ep0 request flushed")
        self.ep0_in_pending = self.ep0_out_pending = False
        if is_in:
            data = bytes(arg[HEADROOM : HEADROOM + length])
            self._ep0_result = ControlResult(
                "data", data, zero=bool(flags & rawgadget.USB_RAW_IO_FLAGS_ZERO)
            )
            self.cond.notify_all()
            return length
        if setup.length:
            data = self._ep0_data_out[:length]
            arg[HEADROOM : HEADROOM + len(data)] = data
            self._ep0_result = ControlResult("ack")
            self.cond.notify_all()
            if self.ep0_read_delay:
                until = time.monotonic() + self.ep0_read_delay
                while (left := until - time.monotonic()) > 0:
                    self.cond.wait(left)
            return len(data)
        self._ep0_result = ControlResult("ack")
        self.cond.notify_all()
        return 0

    def _ep0_write(self, arg: int | bytearray) -> int:
        return self._ep0_io(arg, True)

    def _ep0_read(self, arg: int | bytearray) -> int:
        return self._ep0_io(arg, False)

    def _ep0_stall(self, arg: int | bytearray) -> int:
        if arg != 0:
            raise OSError(errno.EINVAL, "EP0_STALL takes no argument")
        self._running()
        if self.ep0_queued:
            raise OSError(errno.EBUSY, "ep0 urb queued")
        if not (self.ep0_in_pending or self.ep0_out_pending):
            raise OSError(errno.EBUSY, "no request pending")
        self.ep0_in_pending = self.ep0_out_pending = False
        if self._ep0_waiting:
            self._ep0_result = ControlResult("stall")
            self.cond.notify_all()
        return 0

    def _eps_info(self, arg: int | bytearray) -> int:
        assert isinstance(arg, bytearray)
        self._running()
        packed = rawgadget.pack_ep_info([e.info for e in self._eps])
        arg[: len(packed)] = packed
        return len(self._eps)

    def _ep_enable(self, arg: int | bytearray) -> int:
        assert isinstance(arg, bytearray)
        desc = bytes(arg[: rawgadget.SIZEOF_ENDPOINT_DESCRIPTOR])
        max_packet = int.from_bytes(desc[4:6], "little")
        if ch9.endpoint_packet_size(max_packet) == 0:
            raise OSError(errno.EINVAL, "bad maxpacket")
        self._running()
        address, kind = desc[2], desc[3] & 3
        matched = False
        for index, ep in enumerate(self._eps):
            info = ep.info
            if not info.supports(address, kind, ch9.endpoint_packet_size(max_packet)):
                continue
            matched = True
            if ep.enabled:
                continue
            ep.enabled = True
            ep.address = address
            ep.transfer_type = kind
            ep.packet_size = ch9.endpoint_packet_size(max_packet)
            ep.halted = ep.wedged = False
            ep.request = None
            return index
        raise OSError(errno.EBUSY if matched else errno.EINVAL, "no endpoint")

    def _ep(self, handle: int) -> _Ep:
        if not 0 <= handle < len(self._eps):
            raise OSError(errno.EBUSY, "invalid endpoint")
        return self._eps[handle]

    def _ep_disable(self, arg: int | bytearray) -> int:
        assert isinstance(arg, int)
        self._running()
        ep = self._ep(arg)
        if not ep.enabled:
            raise OSError(errno.EINVAL, "endpoint not enabled")
        if ep.request is not None:
            raise OSError(errno.EINVAL, "waiting for urb completion")
        ep.enabled = False
        return 0

    def _halt(self, arg: int | bytearray, *, set_: bool, wedge: bool = False) -> int:
        assert isinstance(arg, int)
        self._running()
        ep = self._ep(arg)
        if not ep.enabled:
            raise OSError(errno.EINVAL, "endpoint not enabled")
        if ep.request is not None:
            raise OSError(errno.EINVAL, "waiting for urb completion")
        if ep.transfer_type == ch9.XFER_ISOC:
            raise OSError(errno.EINVAL, "cannot halt iso")
        if set_:
            ep.halted = True
            ep.wedged = wedge
        else:
            ep.halted = ep.wedged = False
        self.cond.notify_all()
        return 0

    def _ep_io(self, arg: int | bytearray, is_in: bool) -> int:
        assert isinstance(arg, bytearray)
        handle, flags, length = rawgadget.unpack_ep_io(arg)
        self._running()
        if handle >= len(self._eps):
            raise OSError(errno.EINVAL, "invalid endpoint")
        ep = self._eps[handle]
        if not ep.enabled:
            raise OSError(errno.EBUSY, "endpoint not enabled")
        if ep.request is not None:
            raise OSError(errno.EBUSY, "urb already queued")
        if is_in != bool(ep.address & 0x80):
            raise OSError(errno.EINVAL, "wrong direction")
        request = _Request(thread=threading.get_ident(), started=not ep.halted)
        if is_in:
            request.data = bytes(arg[HEADROOM : HEADROOM + length])
            request.zero = bool(flags & rawgadget.USB_RAW_IO_FLAGS_ZERO)
            request.packets = deque(
                packetize(request.data, ep.packet_size, zlp=request.zero)
            )
        else:
            request.capacity = length
            request.receiver = Receiver(length, ep.packet_size)
        ep.request = request
        self.cond.notify_all()
        try:
            self._wait(lambda: request.status is not None)
        except OSError:
            # Interrupted: dequeue (raw_gadget returns -EINTR for -ECONNRESET).
            if request.status is None or request.status < 0:
                ep.request = None
                self.cond.notify_all()
                raise
        ep.request = None
        self.cond.notify_all()
        status = request.status
        assert status is not None
        if status < 0:
            raise OSError(-status, os.strerror(-status))
        if not is_in:
            assert request.receiver is not None
            data = bytes(request.receiver.data)
            arg[HEADROOM : HEADROOM + len(data)] = data
        return status

    def _ep_write(self, arg: int | bytearray) -> int:
        return self._ep_io(arg, True)

    def _ep_read(self, arg: int | bytearray) -> int:
        return self._ep_io(arg, False)

    def _configure(self, arg: int | bytearray) -> int:
        if arg != 0:
            raise OSError(errno.EINVAL, "CONFIGURE takes no argument")
        self._running()
        self.configure_calls += 1
        self._set_state("configured")
        return 0

    def _vbus_draw(self, arg: int | bytearray) -> int:
        assert isinstance(arg, int)
        self._running()
        self.vbus_ma = 2 * arg
        return 0

    # --- host side ---------------------------------------------------------

    def host_attach(self, host_speed: Speed, timeout: float = 5.0) -> Speed:
        """Wait for the gadget's pull-up, then bring the link up."""
        deadline = time.monotonic() + timeout
        with self.cond:
            while not self.bound:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("gadget never connected (no RUN)")
                self.cond.wait(remaining)
            self.link_speed = Speed(
                min(host_speed, self.max_speed, self.requested_speed)
            )
            self._set_state("powered")
            return self.link_speed

    def host_reset(self) -> None:
        with self.cond:
            if not self.bound:
                raise ConnectionError("no gadget on the bus")
            self._cancel_requests(-errno.ESHUTDOWN)
            self._bus_epoch += 1
            for ep in self._eps:
                ep.halted = ep.wedged = False
            self.address = 0
            self.remote_wakeup = False
            self._abandon_ep0()
            self._set_state("default")
            self._queue_event(EventType.RESET)

    def host_disconnect(self) -> None:
        """VBUS loss: dwc3's disconnect interrupt does not give requests back."""
        with self.cond:
            self._link_down("not attached", cancel=False)
            if self.bound:
                self._queue_event(EventType.DISCONNECT)

    def host_suspend(self) -> None:
        with self.cond:
            self._previous_state = self.usb_state
            self._set_state("suspended")
            self._queue_event(EventType.SUSPEND)

    def host_resume(self) -> None:
        with self.cond:
            self._set_state(self._previous_state)
            self._queue_event(EventType.RESUME)

    def _udc_standard(self, setup: Setup) -> ControlResult | None:
        """dwc3-like ep0 handling of requests the gadget never sees."""
        if not self.udc_handles_standard or not setup.is_standard:
            return None
        recipient = setup.recipient
        if setup.request == ch9.SET_ADDRESS and recipient == ch9.RECIP_DEVICE:
            self.address = setup.value
            self._set_state("addressed" if setup.value else "default")
            return ControlResult("ack")
        if setup.request == ch9.GET_STATUS and setup.is_in:
            if recipient == ch9.RECIP_DEVICE:
                return ControlResult("data", bytes([int(self.remote_wakeup) << 1, 0]))
            if recipient == ch9.RECIP_ENDPOINT:
                ep = self._by_address(setup.index & 0x8F)
                if setup.index & 0x0F == 0:
                    return ControlResult("data", b"\0\0")
                if ep is None:
                    return ControlResult("stall")
                return ControlResult("data", bytes([int(ep.halted), 0]))
        if setup.request in (ch9.CLEAR_FEATURE, ch9.SET_FEATURE):
            enable = setup.request == ch9.SET_FEATURE
            if recipient == ch9.RECIP_DEVICE:
                if setup.value == ch9.FEATURE_DEVICE_REMOTE_WAKEUP:
                    self.remote_wakeup = enable
                    return ControlResult("ack")
                return ControlResult("stall")
            if recipient == ch9.RECIP_ENDPOINT and setup.value == 0:
                if setup.index & 0x0F == 0:
                    return ControlResult("ack")
                ep = self._by_address(setup.index & 0x8F)
                if ep is None:
                    return ControlResult("stall")
                if enable:
                    ep.halted = True
                else:
                    request = ep.request
                    if request is not None and request.status is None:
                        if request.started and self.clear_halt_cancels:
                            # __dwc3_gadget_ep_set_halt(value=0): stop the
                            # transfer, give started requests back -EPIPE.
                            request.status = -errno.EPIPE
                            self.log.append(f"clear-halt EPIPE {ep.address:#04x}")
                        else:
                            request.started = True
                    if not ep.wedged:
                        ep.halted = False
                self.cond.notify_all()
                return ControlResult("ack")
        if setup.request == ch9.SET_CONFIGURATION and setup.value == 0:
            self._set_state("addressed")
        return None

    def _by_address(self, address: int) -> _Ep | None:
        for ep in self._eps:
            if ep.enabled and ep.address == address:
                return ep
        return None

    def host_control(self, setup: Setup, data: bytes, timeout: float) -> ControlResult:
        """Run one control transfer from the host; wait at most ``timeout``."""
        start = time.monotonic()
        with self.cond:
            if not self.bound or self.link_speed == Speed.UNKNOWN:
                raise ConnectionError("no link")
            local = self._udc_standard(setup)
            if local is not None:
                if local.outcome == "data":
                    local.data = local.data[: setup.length]
                return local
            if self.ep0_in_pending or self.ep0_out_pending:
                self.log.append(f"SETUP while pending: {setup}")
                return ControlResult("stall", elapsed=time.monotonic() - start)
            if setup.has_in_data:
                self.ep0_in_pending = True
            else:
                self.ep0_out_pending = True
            self._ep0_setup = setup
            self._ep0_data_out = data
            self._ep0_result = None
            self._ep0_waiting = True
            self._queue_event(EventType.CONTROL, setup.pack())
            deadline = start + timeout
            while self._ep0_result is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._ep0_waiting = False
                    return ControlResult("timeout", elapsed=time.monotonic() - start)
                self.cond.wait(remaining)
            result = self._ep0_result
            self._ep0_waiting = False
            result.elapsed = time.monotonic() - start
            if setup.request == ch9.SET_CONFIGURATION and result.outcome == "stall":
                self._set_state("addressed")
            return result

    def _ep_by_address(self, address: int) -> _Ep:
        ep = self._by_address(address)
        if ep is None:
            raise LookupError(f"endpoint {address:#04x} is not enabled")
        return ep

    def host_in(self, address: int, length: int, timeout: float) -> tuple[str, bytes]:
        """Host IN transfer: ("ok"|"stall"|"timeout"|"babble"|"disconnected", data)."""
        deadline = time.monotonic() + timeout
        with self.cond:
            ep = self._ep_by_address(address)
            receiver = Receiver(length, ep.packet_size)
            while not receiver.done:
                if self.link_speed == Speed.UNKNOWN:
                    return "disconnected", bytes(receiver.data)
                if ep.halted:
                    return "stall", bytes(receiver.data)
                request = ep.request
                if request is not None and request.status is None and request.packets:
                    packet = request.packets.popleft()
                    try:
                        receiver.feed(packet)
                    except BabbleError:
                        request.status = -errno.EOVERFLOW
                        self.cond.notify_all()
                        return "babble", bytes(receiver.data)
                    if not request.packets:
                        request.status = len(request.data)
                        self.cond.notify_all()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return "timeout", bytes(receiver.data)
                self.cond.wait(remaining)
                if not ep.enabled:
                    return "disconnected", bytes(receiver.data)
            return "ok", bytes(receiver.data)

    def host_out(
        self, address: int, data: bytes, timeout: float, *, zlp: bool = False
    ) -> str:
        """Host OUT transfer: "ok", "stall", "timeout", "babble" or "disconnected"."""
        deadline = time.monotonic() + timeout
        with self.cond:
            ep = self._ep_by_address(address)
            packets = deque(packetize(data, ep.packet_size, zlp=zlp))
            while packets:
                if self.link_speed == Speed.UNKNOWN:
                    return "disconnected"
                if ep.halted:
                    return "stall"
                request = ep.request
                if request is not None and request.status is None:
                    assert request.receiver is not None
                    try:
                        request.receiver.feed(packets.popleft())
                    except BabbleError:
                        request.status = -errno.EOVERFLOW
                        self.cond.notify_all()
                        return "babble"
                    if request.receiver.done:
                        request.status = len(request.receiver.data)
                        self.cond.notify_all()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return "timeout"
                self.cond.wait(remaining)
                if not ep.enabled:
                    return "disconnected"
            return "ok"


_HANDLERS: dict[int, Callable[[FakeRawGadgetKernel, int | bytearray], int]] = {
    rawgadget.USB_RAW_IOCTL_INIT: FakeRawGadgetKernel._init,
    rawgadget.USB_RAW_IOCTL_RUN: FakeRawGadgetKernel._run,
    rawgadget.USB_RAW_IOCTL_EVENT_FETCH: FakeRawGadgetKernel._event_fetch,
    rawgadget.USB_RAW_IOCTL_EP0_WRITE: FakeRawGadgetKernel._ep0_write,
    rawgadget.USB_RAW_IOCTL_EP0_READ: FakeRawGadgetKernel._ep0_read,
    rawgadget.USB_RAW_IOCTL_EP_ENABLE: FakeRawGadgetKernel._ep_enable,
    rawgadget.USB_RAW_IOCTL_EP_DISABLE: FakeRawGadgetKernel._ep_disable,
    rawgadget.USB_RAW_IOCTL_EP_WRITE: FakeRawGadgetKernel._ep_write,
    rawgadget.USB_RAW_IOCTL_EP_READ: FakeRawGadgetKernel._ep_read,
    rawgadget.USB_RAW_IOCTL_CONFIGURE: FakeRawGadgetKernel._configure,
    rawgadget.USB_RAW_IOCTL_VBUS_DRAW: FakeRawGadgetKernel._vbus_draw,
    rawgadget.USB_RAW_IOCTL_EPS_INFO: FakeRawGadgetKernel._eps_info,
    rawgadget.USB_RAW_IOCTL_EP0_STALL: FakeRawGadgetKernel._ep0_stall,
    rawgadget.USB_RAW_IOCTL_EP_SET_HALT: lambda k, a: k._halt(a, set_=True),
    rawgadget.USB_RAW_IOCTL_EP_CLEAR_HALT: lambda k, a: k._halt(a, set_=False),
    rawgadget.USB_RAW_IOCTL_EP_SET_WEDGE: lambda k, a: k._halt(
        a, set_=True, wedge=True
    ),
}
