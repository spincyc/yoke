"""Raw Gadget ABI encodings and the backend against the simulated kernel.

Golden values were produced on 2026-11-04 by compiling a probe against
``include/uapi/linux/usb/raw_gadget.h`` (linux-api-headers 7.3, identical to
torvalds/linux master apart from install-time guards) with GCC on x86_64.
arm64 uses the same asm-generic ``_IOC`` encoding. ⚠ Assumption: the Radxa
kernel's Raw Gadget matches this uapi; that is bench-unverified.
"""

from __future__ import annotations

import errno
import signal
import threading
import time

import pytest

from tests.usbsim.udc import FakeRawGadgetKernel, dwc3_like_endpoints
from yoke.usb import rawgadget as rg
from yoke.usb.ch9 import Setup, Speed
from yoke.usb.gadget import (
    HEADROOM,
    EventType,
    GadgetError,
    GadgetInterruptedError,
    GadgetShutdownError,
    UdcEndpoint,
)

GOLDEN = {
    "USB_RAW_IOCTL_INIT": 0x41015500,
    "USB_RAW_IOCTL_RUN": 0x00005501,
    "USB_RAW_IOCTL_EVENT_FETCH": 0x80085502,
    "USB_RAW_IOCTL_EP0_WRITE": 0x40085503,
    "USB_RAW_IOCTL_EP0_READ": 0xC0085504,
    "USB_RAW_IOCTL_EP_ENABLE": 0x40095505,
    "USB_RAW_IOCTL_EP_DISABLE": 0x40045506,
    "USB_RAW_IOCTL_EP_WRITE": 0x40085507,
    "USB_RAW_IOCTL_EP_READ": 0xC0085508,
    "USB_RAW_IOCTL_CONFIGURE": 0x00005509,
    "USB_RAW_IOCTL_VBUS_DRAW": 0x4004550A,
    "USB_RAW_IOCTL_EPS_INFO": 0x83C0550B,
    "USB_RAW_IOCTL_EP0_STALL": 0x0000550C,
    "USB_RAW_IOCTL_EP_SET_HALT": 0x4004550D,
    "USB_RAW_IOCTL_EP_CLEAR_HALT": 0x4004550E,
    "USB_RAW_IOCTL_EP_SET_WEDGE": 0x4004550F,
}
# sizeof(): usb_raw_init 257, usb_raw_event 8, usb_raw_ep_io 8,
# usb_endpoint_descriptor 9 (packed), usb_raw_ep_info 32, usb_raw_eps_info 960;
# offsetof: init.speed 256, ep_info.addr 16, caps 20, limits 24;
# caps bitfield: type_control = bit 0, dir_out = bit 5.


@pytest.mark.parametrize(("name", "value"), sorted(GOLDEN.items()))
def test_ioctl_numbers_match_compiled_header(name: str, value: int) -> None:
    assert getattr(rg, name) == value


def test_struct_sizes_and_offsets() -> None:
    assert rg.SIZEOF_INIT == 257 and rg.SIZEOF_EVENT == 8 and rg.SIZEOF_EP_IO == 8
    assert rg.SIZEOF_ENDPOINT_DESCRIPTOR == 9 and rg.SIZEOF_EP_INFO == 32
    assert rg.SIZEOF_EPS_INFO == 960 and HEADROOM == rg.SIZEOF_EP_IO
    init = rg.pack_init("fcc00000.usb", "fcc00000.usb", Speed.FULL)
    assert len(init) == 257 and init[256] == 2
    assert init[:13] == b"fcc00000.usb\0" and init[128:141] == b"fcc00000.usb\0"
    assert rg.unpack_init(bytes(init)) == ("fcc00000.usb", "fcc00000.usb", 2)
    with pytest.raises(ValueError):
        rg.pack_init("x" * 128, "udc", 3)


def test_ep_io_and_event_layout() -> None:
    buf = bytearray(HEADROOM + 4)
    rg.pack_ep_io(buf, 3, 4, zero=True)
    assert buf[:8] == bytes([3, 0, 1, 0, 4, 0, 0, 0])  # native little endian
    assert rg.unpack_ep_io(buf) == (3, 1, 4)
    setup = Setup(0x80, 6, 0x0100, 0, 18).pack()
    raw = rg.pack_event(EventType.CONTROL, setup)
    assert raw[:8] == bytes([2, 0, 0, 0, 8, 0, 0, 0])
    assert rg.unpack_event(raw) == (2, setup)


def test_endpoint_descriptor_argument_is_nine_bytes() -> None:
    desc = bytes([7, 5, 0x81, 2, 0, 2, 0])
    arg = rg.endpoint_descriptor_arg(desc)
    assert arg == bytearray(desc + b"\0\0")
    with pytest.raises(ValueError):
        rg.endpoint_descriptor_arg(bytes([9, 4, 0, 0, 0, 0, 0, 0, 0]))


def test_eps_info_bitfields_round_trip() -> None:
    eps = [
        UdcEndpoint("ep1in", 1, False, False, True, True, True, False, 1024),
        UdcEndpoint("ep-a", None, False, True, False, False, False, True, 64),
    ]
    packed = rg.pack_ep_info(eps)
    assert len(packed) == 960
    assert packed[16:20] == bytes([1, 0, 0, 0])
    assert packed[20:24] == bytes([0b011100, 0, 0, 0])
    assert packed[32 + 16 : 32 + 20] == bytes([0xFF, 0, 0, 0])
    assert packed[32 + 20] == 0b100010
    assert rg.unpack_ep_info(packed, 2) == eps
    with pytest.raises(GadgetError):
        rg.unpack_ep_info(packed, 31)


def started() -> tuple[FakeRawGadgetKernel, rg.RawGadgetBackend]:
    kernel = FakeRawGadgetKernel()
    backend = kernel.backend()
    backend.start(Speed.HIGH)
    return kernel, backend


def test_backend_binds_and_reports_errors_like_the_kernel() -> None:
    kernel, backend = started()
    assert kernel.bound and kernel.requested_speed == Speed.HIGH
    assert backend.fetch_event().type is EventType.CONNECT
    with pytest.raises(GadgetError) as busy:
        backend.ep0_write(b"x", False)  # no control request pending
    assert busy.value.errno == errno.EBUSY
    assert backend.endpoints() == dwc3_like_endpoints()
    handle = backend.ep_enable(bytes([7, 5, 0x81, 2, 0, 2, 0]))
    with pytest.raises(GadgetError) as wrong:
        backend.ep_read(handle, bytearray(HEADROOM + 8), 8)
    assert wrong.value.errno == errno.EINVAL
    with pytest.raises(GadgetError) as missing:
        backend.ep_enable(bytes([7, 5, 0x8F, 2, 0, 2, 0]))  # no ep15 on this UDC
    assert missing.value.errno == errno.EINVAL
    backend.vbus_draw(500)
    assert kernel.vbus_ma == 500
    backend.close()
    assert not kernel.bound


def test_run_fails_when_another_gadget_owns_the_udc() -> None:
    kernel = FakeRawGadgetKernel()
    kernel.busy_with_other_gadget = True
    backend = kernel.backend()
    with pytest.raises(GadgetError, match="RUN"):
        backend.start(Speed.HIGH)
    assert kernel.dev_state == "closed"


def test_blocked_write_is_interrupted_and_queued_endpoint_cannot_halt() -> None:
    kernel, backend = started()
    handle = backend.ep_enable(bytes([7, 5, 0x81, 2, 0, 2, 0]))
    errors: list[BaseException] = []

    def write() -> None:
        try:
            backend.ep_write(handle, bytearray(HEADROOM + 4), 4, False)
        except GadgetError as exc:
            errors.append(exc)

    thread = threading.Thread(target=write)
    thread.start()
    deadline = time.monotonic() + 2
    while kernel._eps[handle].request is None and time.monotonic() < deadline:
        time.sleep(0.001)
    with pytest.raises(GadgetError) as queued:
        backend.ep_set_halt(handle)
    assert queued.value.errno == errno.EINVAL
    assert thread.ident is not None
    backend.interrupt(thread.ident)
    thread.join(2)
    assert isinstance(errors[0], GadgetInterruptedError)
    backend.ep_set_halt(handle)
    backend.ep_disable(handle)


def test_bus_reset_cancels_queued_requests_with_eshutdown() -> None:
    kernel, backend = started()
    kernel.host_attach(Speed.HIGH)
    handle = backend.ep_enable(bytes([7, 5, 0x02, 2, 0, 2, 0]))
    errors: list[BaseException] = []

    def read() -> None:
        try:
            backend.ep_read(handle, bytearray(HEADROOM + 512), 512)
        except GadgetError as exc:
            errors.append(exc)

    thread = threading.Thread(target=read)
    thread.start()
    deadline = time.monotonic() + 2
    while kernel._eps[handle].request is None and time.monotonic() < deadline:
        time.sleep(0.001)
    kernel.host_reset()
    thread.join(2)
    assert isinstance(errors[0], GadgetShutdownError)
    assert backend.link_speed() == Speed.HIGH


def test_start_requires_a_sigusr1_handler() -> None:
    kernel = FakeRawGadgetKernel()
    backend = rg.RawGadgetBackend(
        kernel.name,
        driver_name=kernel.gadget_name,
        opener=kernel.open,
        ioctl=kernel.ioctl,
        closer=kernel.close,
        interrupter=kernel.interrupt,
    )
    previous = signal.getsignal(signal.SIGUSR1)
    signal.signal(signal.SIGUSR1, signal.SIG_DFL)
    try:
        with pytest.raises(GadgetError, match="SIGUSR1"):
            backend.start(Speed.HIGH)
    finally:
        signal.signal(signal.SIGUSR1, previous)


def test_open_failures_are_gadget_errors() -> None:
    backend = rg.RawGadgetBackend(
        "udc",
        driver_name="udc",
        path="/dev/missing-raw-gadget",
        require_signal_handler=False,
    )
    with pytest.raises(GadgetError):
        backend.start(Speed.HIGH)
