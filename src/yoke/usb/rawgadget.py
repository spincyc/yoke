"""Linux Raw Gadget backend (``/dev/raw-gadget``) via ctypes ``ioctl(2)``.

⚠ Bench-unverified. The kernel documents Raw Gadget as a debugging feature
(Documentation/usb/raw-gadget.rst). Whether the Radxa ZERO 3E kernel builds
``CONFIG_USB_RAW_GADGET`` and whether its dwc3 UDC (``fcc00000.usb``) behaves
as assumed here are hardware claims recorded in ``doc/design/USB-DATAPATH.md``.
Every assumption is isolated in this module:

* ABI: ioctl numbers and struct layouts are copied from
  ``include/uapi/linux/usb/raw_gadget.h`` (asm-generic ``_IOC`` encoding,
  as on arm64 and x86) and checked against values compiled from that header
  in ``tests/test_usb_rawgadget.py``. Structs use native little-endian
  layout; ``usb_raw_ep_caps`` bitfields are allocated from bit 0.
* Control: Raw Gadget delays the status stage of a request without data
  until EP0_READ(0); for OUT requests with data the UDC completes the status
  stage itself once EP0_READ returns the data.
* Cancellation: endpoint and event ioctls wait interruptibly, so a signal
  aimed at the blocked thread returns EINTR. The ioctl is issued through
  ctypes (:class:`LibcIoctl`) because ``fcntl.ioctl`` retries EINTR. The
  worker must install a SIGUSR1 handler first; :meth:`RawGadgetBackend.start`
  refuses otherwise, because SIGUSR1's default action would terminate the
  process.
* Naming: ``usb_raw_init.driver_name`` is the controller's gadget name
  (``dwc3-gadget``), not the UDC device name (:data:`GADGET_NAMES`).
* dwc3 completes requests started on an endpoint with ``-EPIPE`` when the
  host clears that endpoint's halt (``__dwc3_gadget_ep_set_halt``); the
  proxy treats that as an observed host CLEAR_FEATURE.
* Halting, disabling or clearing an endpoint fails while a request is queued
  on it; the proxy only does so from the endpoint's own pump thread.
"""

from __future__ import annotations

import ctypes
import errno
import os
import signal
import struct
from collections.abc import Callable
from pathlib import Path

from yoke.usb.ch9 import SETUP_SIZE, Setup, Speed, speed_from_name
from yoke.usb.gadget import (
    HEADROOM,
    EventType,
    GadgetError,
    GadgetEvent,
    GadgetHaltClearedError,
    GadgetInterruptedError,
    GadgetShutdownError,
    UdcEndpoint,
)

DEVICE_PATH = "/dev/raw-gadget"
UDC_NAME_LENGTH_MAX = 128
USB_RAW_EPS_NUM_MAX = 30
USB_RAW_EP_NAME_MAX = 16
USB_RAW_EP_ADDR_ANY = 0xFF
USB_RAW_IO_FLAGS_ZERO = 0x0001

_IOC_NONE, _IOC_WRITE, _IOC_READ = 0, 1, 2


def _ioc(direction: int, nr: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (ord("U") << 8) | nr


SIZEOF_INIT = 2 * UDC_NAME_LENGTH_MAX + 1
SIZEOF_EVENT = 8
SIZEOF_EP_IO = 8
SIZEOF_ENDPOINT_DESCRIPTOR = 9  # packed, includes the audio-only fields
SIZEOF_EP_INFO = 32
SIZEOF_EPS_INFO = USB_RAW_EPS_NUM_MAX * SIZEOF_EP_INFO
SIZEOF_U32 = 4

USB_RAW_IOCTL_INIT = _ioc(_IOC_WRITE, 0, SIZEOF_INIT)
USB_RAW_IOCTL_RUN = _ioc(_IOC_NONE, 1, 0)
USB_RAW_IOCTL_EVENT_FETCH = _ioc(_IOC_READ, 2, SIZEOF_EVENT)
USB_RAW_IOCTL_EP0_WRITE = _ioc(_IOC_WRITE, 3, SIZEOF_EP_IO)
USB_RAW_IOCTL_EP0_READ = _ioc(_IOC_READ | _IOC_WRITE, 4, SIZEOF_EP_IO)
USB_RAW_IOCTL_EP_ENABLE = _ioc(_IOC_WRITE, 5, SIZEOF_ENDPOINT_DESCRIPTOR)
USB_RAW_IOCTL_EP_DISABLE = _ioc(_IOC_WRITE, 6, SIZEOF_U32)
USB_RAW_IOCTL_EP_WRITE = _ioc(_IOC_WRITE, 7, SIZEOF_EP_IO)
USB_RAW_IOCTL_EP_READ = _ioc(_IOC_READ | _IOC_WRITE, 8, SIZEOF_EP_IO)
USB_RAW_IOCTL_CONFIGURE = _ioc(_IOC_NONE, 9, 0)
USB_RAW_IOCTL_VBUS_DRAW = _ioc(_IOC_WRITE, 10, SIZEOF_U32)
USB_RAW_IOCTL_EPS_INFO = _ioc(_IOC_READ, 11, SIZEOF_EPS_INFO)
USB_RAW_IOCTL_EP0_STALL = _ioc(_IOC_NONE, 12, 0)
USB_RAW_IOCTL_EP_SET_HALT = _ioc(_IOC_WRITE, 13, SIZEOF_U32)
USB_RAW_IOCTL_EP_CLEAR_HALT = _ioc(_IOC_WRITE, 14, SIZEOF_U32)
USB_RAW_IOCTL_EP_SET_WEDGE = _ioc(_IOC_WRITE, 15, SIZEOF_U32)

_EVENT = struct.Struct("=II")
_EP_IO = struct.Struct("=HHI")
_EP_INFO = struct.Struct("=16sIIHHI")
assert _EP_IO.size == HEADROOM and _EP_INFO.size == SIZEOF_EP_INFO


def pack_init(driver_name: str, device_name: str, speed: int) -> bytearray:
    """``struct usb_raw_init``: two NUL-terminated 128-byte names and a speed."""
    out = bytearray(SIZEOF_INIT)
    for offset, name in ((0, driver_name), (UDC_NAME_LENGTH_MAX, device_name)):
        raw = name.encode("ascii")
        if not raw or len(raw) >= UDC_NAME_LENGTH_MAX or b"\0" in raw:
            raise ValueError(f"UDC name must be 1..127 ASCII bytes: {name!r}")
        out[offset : offset + len(raw)] = raw
    if not 0 <= speed <= 0xFF:
        raise ValueError("speed out of range")
    out[2 * UDC_NAME_LENGTH_MAX] = speed
    return out


def unpack_init(raw: bytes | bytearray) -> tuple[str, str, int]:
    if len(raw) != SIZEOF_INIT:
        raise ValueError("usb_raw_init must be 257 bytes")

    def name(chunk: bytes | bytearray) -> str:
        text, nul, _ = bytes(chunk).partition(b"\0")
        if not nul:
            raise ValueError("UDC name is not NUL terminated")
        return text.decode("ascii")

    return (
        name(raw[:UDC_NAME_LENGTH_MAX]),
        name(raw[UDC_NAME_LENGTH_MAX : 2 * UDC_NAME_LENGTH_MAX]),
        raw[2 * UDC_NAME_LENGTH_MAX],
    )


def pack_ep_io(buf: bytearray, ep: int, length: int, *, zero: bool = False) -> None:
    """Write ``struct usb_raw_ep_io`` into the buffer's headroom."""
    _EP_IO.pack_into(buf, 0, ep, USB_RAW_IO_FLAGS_ZERO if zero else 0, length)


def unpack_ep_io(buf: bytes | bytearray | memoryview) -> tuple[int, int, int]:
    ep, flags, length = _EP_IO.unpack_from(buf)
    return int(ep), int(flags), int(length)


def pack_event(event_type: int, data: bytes) -> bytes:
    return _EVENT.pack(event_type, len(data)) + data


def unpack_event(buf: bytes | bytearray) -> tuple[int, bytes]:
    event_type, length = _EVENT.unpack_from(buf)
    return int(event_type), bytes(buf[SIZEOF_EVENT : SIZEOF_EVENT + length])


def endpoint_descriptor_arg(descriptor: bytes) -> bytearray:
    """The 9-byte packed ``struct usb_endpoint_descriptor`` EP_ENABLE copies."""
    if len(descriptor) < 7 or descriptor[1] != 5:
        raise ValueError("not an endpoint descriptor")
    out = bytearray(SIZEOF_ENDPOINT_DESCRIPTOR)
    head = descriptor[:SIZEOF_ENDPOINT_DESCRIPTOR]
    out[: len(head)] = head
    return out


def pack_ep_info(endpoints: list[UdcEndpoint]) -> bytearray:
    out = bytearray(SIZEOF_EPS_INFO)
    for index, ep in enumerate(endpoints):
        caps = (
            int(ep.control)
            | int(ep.iso) << 1
            | int(ep.bulk) << 2
            | int(ep.interrupt) << 3
            | int(ep.dir_in) << 4
            | int(ep.dir_out) << 5
        )
        _EP_INFO.pack_into(
            out,
            index * SIZEOF_EP_INFO,
            ep.name.encode("ascii")[: USB_RAW_EP_NAME_MAX - 1],
            USB_RAW_EP_ADDR_ANY if ep.number is None else ep.number,
            caps,
            ep.maxpacket_limit,
            0,
            0,
        )
    return out


def unpack_ep_info(buf: bytes | bytearray, count: int) -> list[UdcEndpoint]:
    if not 0 <= count <= USB_RAW_EPS_NUM_MAX:
        raise GadgetError(errno.EPROTO, f"EPS_INFO count {count} out of range")
    result = []
    for index in range(count):
        name, addr, caps, limit, _streams, _reserved = _EP_INFO.unpack_from(
            buf, index * SIZEOF_EP_INFO
        )
        result.append(
            UdcEndpoint(
                name=name.partition(b"\0")[0].decode("ascii", "replace"),
                number=None if addr == USB_RAW_EP_ADDR_ANY else int(addr),
                control=bool(caps & 1),
                iso=bool(caps & 2),
                bulk=bool(caps & 4),
                interrupt=bool(caps & 8),
                dir_in=bool(caps & 16),
                dir_out=bool(caps & 32),
                maxpacket_limit=int(limit),
            )
        )
    return result


RawIoctl = Callable[[int, int, "int | bytearray"], int]
"""``ioctl(2)`` with kernel conventions: a result >= 0 or ``-errno``.

It must call the kernel exactly once: never raise for a failure and never
retry. CPython's ``fcntl.ioctl`` retries EINTR whenever no Python-level
signal handler raised (PEP 475), and off the main thread one never runs, so
a blocked Raw Gadget call could not be cancelled through it. The dispatch
in :meth:`RawGadgetBackend._call` turns ``-errno`` into exceptions and does
not retry either; the simulated kernel is injected at this same layer.
"""

_LIBC: ctypes.CDLL | None = None


def _libc() -> ctypes.CDLL:
    global _LIBC
    if _LIBC is None:
        _LIBC = ctypes.CDLL(None, use_errno=True)
    return _LIBC


def errno_call(func: Callable[..., int], *args: object) -> int:
    """Call a ctypes libc function once; return its result or ``-errno``.

    ctypes releases the GIL for the call, and a signal handler installed
    without SA_RESTART (Python's default) makes a blocked call return EINTR.
    """
    ctypes.set_errno(0)
    result = int(func(*args))
    return -(ctypes.get_errno() or errno.EIO) if result < 0 else result


class LibcIoctl:
    """The production :data:`RawIoctl`: libc ``ioctl`` through ctypes."""

    def __init__(self) -> None:
        function = _libc().ioctl
        function.restype = ctypes.c_int
        function.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p]
        self._ioctl = function

    def __call__(self, fd: int, request: int, arg: int | bytearray) -> int:
        if isinstance(arg, int):
            return errno_call(self._ioctl, fd, request, arg)
        view = (ctypes.c_char * len(arg)).from_buffer(arg)
        try:
            return errno_call(self._ioctl, fd, request, ctypes.addressof(view))
        finally:
            del view  # release the buffer export before the caller resizes


def _default_interrupt(thread_ident: int) -> None:
    signal.pthread_kill(thread_ident, signal.SIGUSR1)


def _error(code: int, what: str) -> GadgetError:
    code = code or errno.EIO
    message = f"raw-gadget {what}: {os.strerror(code)}"
    if code == errno.EINTR:
        return GadgetInterruptedError(code, message)
    if code == errno.EPIPE:
        return GadgetHaltClearedError(code, message)
    if code in (errno.ESHUTDOWN, errno.ECONNRESET, errno.ENODEV):
        return GadgetShutdownError(code, message)
    return GadgetError(code, message)


GADGET_NAMES = {
    "dwc3": "dwc3-gadget",
    "dummy_udc": "dummy_udc",
    "musb-hdrc": "musb-hdrc",
}
"""UDC platform driver -> ``usb_gadget.name`` that Raw Gadget compares
``usb_raw_init.driver_name`` against (``raw_gadget.c`` gadget_bind). dwc2 and
unknown drivers use the UDC device name. ⚠ Only dummy_udc is checked against
a running kernel (tests/usbsim/kernel_vm); dwc3's name is from its source."""


def gadget_driver_name(
    device_name: str, udc_sysfs: Path = Path("/sys/class/udc")
) -> str:
    """Infer the gadget name from the UDC's bound platform driver."""
    try:
        driver = (udc_sysfs / device_name / "device" / "driver").resolve(strict=True)
    except OSError:
        return device_name
    return GADGET_NAMES.get(driver.name, device_name)


class RawGadgetBackend:
    """One Raw Gadget instance bound to one UDC.

    ``device_name`` is the UDC's name in ``/sys/class/udc`` (``fcc00000.usb``
    ⚠ on RK3566). ``driver_name`` is the controller's *gadget* name, which
    differs: dwc3 calls every gadget ``dwc3-gadget`` and dummy_hcd
    ``dummy_udc``. When omitted it is inferred with :func:`gadget_driver_name`.
    A wrong name makes RUN fail with EBUSY. Every OS touchpoint is injectable,
    so the simulated kernel runs this exact code, including struct packing
    and the no-retry dispatch.
    """

    def __init__(
        self,
        device_name: str,
        *,
        driver_name: str | None = None,
        path: str = DEVICE_PATH,
        opener: Callable[[str, int], int] = os.open,
        ioctl: RawIoctl | None = None,
        closer: Callable[[int], None] = os.close,
        interrupter: Callable[[int], None] = _default_interrupt,
        read_text: Callable[[Path], str] = Path.read_text,
        udc_sysfs: Path = Path("/sys/class/udc"),
        require_signal_handler: bool = True,
    ) -> None:
        if driver_name is None:
            driver_name = gadget_driver_name(device_name, udc_sysfs)
        pack_init(driver_name, device_name, 0)  # validate names early
        self.driver_name = driver_name
        self._device = device_name
        self._path = path
        self._open = opener
        self._ioctl: RawIoctl = ioctl if ioctl is not None else LibcIoctl()
        self._close = closer
        self._interrupt = interrupter
        self._read_text = read_text
        self._speed_file = udc_sysfs / device_name / "current_speed"
        self._require_signal = require_signal_handler
        self._fd: int | None = None

    def _call(self, request: int, arg: int | bytearray, what: str) -> int:
        fd = self._fd
        if fd is None:
            raise GadgetError(errno.EBADF, f"raw-gadget {what}: not started")
        result = self._ioctl(fd, request, arg)
        if result < 0:
            raise _error(-result, what)
        return result

    def start(self, speed: Speed) -> None:
        if self._fd is not None:
            raise GadgetError(errno.EBUSY, "raw-gadget already started")
        if self._require_signal and not callable(signal.getsignal(signal.SIGUSR1)):
            raise GadgetError(
                errno.EINVAL,
                "install a SIGUSR1 handler before starting Raw Gadget; it "
                "interrupts blocked endpoint ioctls",
            )
        try:
            self._fd = self._open(self._path, os.O_RDWR)
        except OSError as exc:
            raise _error(exc.errno or errno.EIO, f"open {self._path}") from exc
        try:
            self._call(
                USB_RAW_IOCTL_INIT,
                pack_init(self.driver_name, self._device, int(speed)),
                "INIT",
            )
            self._call(
                USB_RAW_IOCTL_RUN,
                0,
                f"RUN on {self._device} as {self.driver_name} "
                "(wrong gadget name, or another gadget bound?)",
            )
        except GadgetError:
            self.close()
            raise

    def fetch_event(self) -> GadgetEvent:
        buf = bytearray(SIZEOF_EVENT + SETUP_SIZE)
        _EVENT.pack_into(buf, 0, 0, SETUP_SIZE)
        self._call(USB_RAW_IOCTL_EVENT_FETCH, buf, "EVENT_FETCH")
        event_type, data = unpack_event(buf)
        try:
            kind = EventType(event_type)
        except ValueError:
            return GadgetEvent(EventType.INVALID)
        if kind is EventType.CONTROL:
            if len(data) != SETUP_SIZE:
                raise GadgetError(errno.EPROTO, "control event without setup")
            return GadgetEvent(kind, Setup.unpack(data))
        return GadgetEvent(kind)

    def ep0_write(self, data: bytes, zero: bool) -> int:
        buf = bytearray(HEADROOM + len(data))
        pack_ep_io(buf, 0, len(data), zero=zero)
        buf[HEADROOM:] = data
        return self._call(USB_RAW_IOCTL_EP0_WRITE, buf, "EP0_WRITE")

    def ep0_read(self, length: int) -> bytes:
        buf = bytearray(HEADROOM + length)
        pack_ep_io(buf, 0, length)
        count = self._call(USB_RAW_IOCTL_EP0_READ, buf, "EP0_READ")
        return bytes(buf[HEADROOM : HEADROOM + count])

    def ep0_stall(self) -> None:
        self._call(USB_RAW_IOCTL_EP0_STALL, 0, "EP0_STALL")

    def endpoints(self) -> list[UdcEndpoint]:
        buf = bytearray(SIZEOF_EPS_INFO)
        count = self._call(USB_RAW_IOCTL_EPS_INFO, buf, "EPS_INFO")
        return unpack_ep_info(buf, count)

    def ep_enable(self, descriptor: bytes) -> int:
        return self._call(
            USB_RAW_IOCTL_EP_ENABLE, endpoint_descriptor_arg(descriptor), "EP_ENABLE"
        )

    def ep_disable(self, handle: int) -> None:
        self._call(USB_RAW_IOCTL_EP_DISABLE, handle, "EP_DISABLE")

    def ep_write(self, handle: int, buf: bytearray, length: int, zero: bool) -> int:
        pack_ep_io(buf, handle, length, zero=zero)
        return self._call(USB_RAW_IOCTL_EP_WRITE, buf, "EP_WRITE")

    def ep_read(self, handle: int, buf: bytearray, length: int) -> int:
        pack_ep_io(buf, handle, length)
        return self._call(USB_RAW_IOCTL_EP_READ, buf, "EP_READ")

    def ep_set_halt(self, handle: int) -> None:
        self._call(USB_RAW_IOCTL_EP_SET_HALT, handle, "EP_SET_HALT")

    def ep_clear_halt(self, handle: int) -> None:
        self._call(USB_RAW_IOCTL_EP_CLEAR_HALT, handle, "EP_CLEAR_HALT")

    def configure(self) -> None:
        self._call(USB_RAW_IOCTL_CONFIGURE, 0, "CONFIGURE")

    def vbus_draw(self, milliamps: int) -> None:
        self._call(USB_RAW_IOCTL_VBUS_DRAW, (milliamps + 1) // 2, "VBUS_DRAW")

    def link_speed(self) -> Speed:
        try:
            return speed_from_name(self._read_text(self._speed_file))
        except OSError:
            return Speed.UNKNOWN

    def interrupt(self, thread_ident: int) -> None:
        self._interrupt(thread_ident)

    def close(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            self._close(fd)
