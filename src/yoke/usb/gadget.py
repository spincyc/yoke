"""Backend-neutral USB device (gadget) presentation interface (ADR 0021).

The proxy drives a :class:`GadgetBackend`. :mod:`yoke.usb.rawgadget` is the
first backend; GadgetFS or a native worker can implement the same surface.
Calls block like the kernel interfaces they wrap. A blocked call is
cancelled from another thread with :meth:`GadgetBackend.interrupt`.

Endpoint buffers carry :data:`HEADROOM` reserved bytes before the payload so
a backend may build its request header in place, avoiding a copy between
the network receive and the device write.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Protocol

from yoke.usb.ch9 import Setup, Speed

HEADROOM = 8


class EventType(IntEnum):
    """Gadget events (values match Raw Gadget ``usb_raw_event_type``)."""

    INVALID = 0
    CONNECT = 1
    CONTROL = 2
    SUSPEND = 3
    RESUME = 4
    RESET = 5
    DISCONNECT = 6


@dataclass(frozen=True, slots=True)
class GadgetEvent:
    type: EventType
    setup: Setup | None = None


@dataclass(frozen=True, slots=True)
class UdcEndpoint:
    """One non-control UDC endpoint (Raw Gadget ``usb_raw_ep_info``)."""

    name: str
    number: int | None  # None: any address (configurable endpoint)
    control: bool
    iso: bool
    bulk: bool
    interrupt: bool
    dir_in: bool
    dir_out: bool
    maxpacket_limit: int

    def supports(self, address: int, transfer_type: int, packet_size: int) -> bool:
        number_ok = self.number is None or self.number == address & 0x0F
        dir_ok = self.dir_in if address & 0x80 else self.dir_out
        type_ok = {2: self.bulk, 3: self.interrupt, 1: self.iso}.get(
            transfer_type, False
        )
        return number_ok and dir_ok and type_ok and packet_size <= self.maxpacket_limit


class GadgetError(OSError):
    """A gadget call failed; ``errno`` carries the kernel error."""


class GadgetInterruptedError(GadgetError):
    """A blocking call was cancelled by :meth:`GadgetBackend.interrupt`."""


class GadgetHaltClearedError(GadgetError):
    """A started request was returned with EPIPE: the host cleared the halt.

    dwc3 answers CLEAR_FEATURE(ENDPOINT_HALT) itself and gives back started
    requests with -EPIPE; for the proxy this is the only sign of that request.
    """


class GadgetShutdownError(GadgetError):
    """The transfer was cancelled by bus reset, disconnect or endpoint disable."""


class GadgetBackend(Protocol):
    def start(self, speed: Speed) -> None:
        """Bind to the UDC and connect (pull-up) at most at ``speed``."""

    def fetch_event(self) -> GadgetEvent: ...

    def ep0_write(self, data: bytes, zero: bool) -> int:
        """Send a control IN data stage; ``zero`` terminates a short reply
        that ends on a packet boundary (set it when ``len(data) < wLength``)."""

    def ep0_read(self, length: int) -> bytes:
        """Read OUT data, or with ``length`` 0 acknowledge the status stage."""

    def ep0_stall(self) -> None: ...

    def endpoints(self) -> list[UdcEndpoint]: ...

    def ep_enable(self, descriptor: bytes) -> int:
        """Enable an endpoint from its descriptor; return its handle."""

    def ep_disable(self, handle: int) -> None: ...

    def ep_write(self, handle: int, buf: bytearray, length: int, zero: bool) -> int:
        """Send ``buf[HEADROOM:HEADROOM+length]``; ``zero`` appends a ZLP."""

    def ep_read(self, handle: int, buf: bytearray, length: int) -> int:
        """Receive into ``buf[HEADROOM:]``; return the byte count."""

    def ep_set_halt(self, handle: int) -> None: ...

    def ep_clear_halt(self, handle: int) -> None: ...

    def configure(self) -> None: ...

    def vbus_draw(self, milliamps: int) -> None: ...

    def link_speed(self) -> Speed:
        """The speed the computer enumerated, or UNKNOWN."""

    def interrupt(self, thread_ident: int) -> None:
        """Cancel a blocking call made by ``thread_ident`` (best effort)."""

    def close(self) -> None:
        """Unbind from the UDC; the computer sees a disconnect."""
