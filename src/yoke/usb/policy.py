"""Pure proxy decisions: control-request routing, IN coalescing, BOT hints.

Nothing here performs I/O, so every routing rule and packet-boundary
calculation is unit-testable (ADR 0021). :mod:`yoke.usb.proxy` executes
the decisions.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field

from yoke.usb import ch9
from yoke.usb.ch9 import Setup
from yoke.usb.descriptors import Presentation, UdcBudget


@dataclass(frozen=True)
class ProxyPolicy:
    """Tunables. Defaults favor exact USB semantics over throughput.

    A remote OUT STALL always reaches the computer on its *next* OUT
    transaction, because the UDC acknowledged the refused packet when a read
    was queued. ``out_window`` > 1 pipelines OUT and delays that STALL by up
    to the window, dropping data the UDC already acknowledged.
    ``power_ma`` is the box's declared draw (⚠ unmeasured; at most 500).
    """

    power_ma: int = 500
    ep0_size: int = 64
    budget: UdcBudget = field(default_factory=UdcBudget)
    in_depth_bulk: int = 16
    in_depth_interrupt: int = 2
    out_window: int = 1
    coalesce_in: bool = True
    bot_sizing: bool = True
    batch_submit: bool = True
    descriptor_cache: bool = True
    max_transfer: int = 65536
    control_timeout: float = 4.5
    import_timeout: float = 5.0
    stop_timeout: float = 2.0
    reset_remote_on_bus_reset: bool = True

    def __post_init__(self) -> None:
        if not 0 <= self.power_ma <= 500:
            raise ValueError("power_ma must be 0..500 (USB 2 bus power)")
        if self.ep0_size not in (8, 16, 32, 64):
            raise ValueError("ep0_size must be 8, 16, 32 or 64")
        for name in ("in_depth_bulk", "in_depth_interrupt", "out_window"):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= 64:
                raise ValueError(f"{name} must be 1..64")
        if self.max_transfer < 512 or self.max_transfer % 512:
            raise ValueError("max_transfer must be a positive multiple of 512")
        for name in ("control_timeout", "import_timeout", "stop_timeout"):
            value = getattr(self, name)
            if not (
                isinstance(value, (int, float)) and math.isfinite(value) and value > 0
            ):
                raise ValueError(f"{name} must be finite and positive")


@dataclass(frozen=True, slots=True)
class Reply:
    data: bytes


@dataclass(frozen=True, slots=True)
class Ack:
    pass


@dataclass(frozen=True, slots=True)
class Stall:
    reason: str


@dataclass(frozen=True, slots=True)
class Forward:
    cache: bool = False


@dataclass(frozen=True, slots=True)
class SetConfiguration:
    value: int


@dataclass(frozen=True, slots=True)
class SetInterface:
    interface: int
    alternate: int


@dataclass(frozen=True, slots=True)
class EndpointHalt:
    address: int
    halt: bool


Decision = (
    Reply | Ack | Stall | Forward | SetConfiguration | SetInterface | EndpointHalt
)


@dataclass
class ControlState:
    """Mutable presentation state the event thread owns."""

    configuration: int = 0
    alternates: dict[int, int] = field(default_factory=dict)
    halted: set[int] = field(default_factory=set)
    descriptor_misses: dict[tuple[int, int], tuple[bytes | None, int]] = field(
        default_factory=dict
    )

    def active_endpoints(self, presentation: Presentation) -> set[int]:
        config = presentation.configuration(self.configuration)
        if config is None:
            return set()
        result: set[int] = set()
        for number, alternate in self.alternates.items():
            setting = config.alt(number, alternate)
            if setting is not None:
                result.update(e.address for e in setting.endpoints)
        return result


def decide(
    setup: Setup, presentation: Presentation, state: ControlState, policy: ProxyPolicy
) -> Decision:
    """Route one control request."""
    if not setup.is_standard:
        return Forward()
    request, recipient = setup.request, setup.recipient
    if request == ch9.GET_DESCRIPTOR and setup.is_in:
        if recipient == ch9.RECIP_INTERFACE:
            key = (setup.index, setup.descriptor_type, setup.descriptor_index)
            cached = presentation.class_descriptors.get(key)
            if cached is not None and policy.descriptor_cache:
                return Reply(cached)
            return Forward()
        if recipient != ch9.RECIP_DEVICE or not policy.descriptor_cache:
            return Forward()
        known, data = presentation.descriptor(setup)
        if known:
            return Reply(data) if data is not None else Stall("descriptor absent")
        miss = state.descriptor_misses.get((setup.value, setup.index))
        if miss is not None:
            data, fetched = miss
            if data is None:
                return Stall("descriptor absent (cached)")
            if len(data) < fetched or setup.length <= fetched:
                return Reply(data)
        return Forward(cache=True)
    if request == ch9.SET_ADDRESS:
        return Ack()  # the UDC owns addressing; never forward
    if request == ch9.SET_CONFIGURATION and recipient == ch9.RECIP_DEVICE:
        if setup.value == 0 or presentation.configuration(setup.value) is not None:
            return SetConfiguration(setup.value)
        return Stall("unknown configuration")
    if request == ch9.GET_CONFIGURATION and setup.is_in:
        return Reply(bytes([state.configuration]))
    config = presentation.configuration(state.configuration)
    if request == ch9.SET_INTERFACE and recipient == ch9.RECIP_INTERFACE:
        if config is not None and config.alt(setup.index, setup.value) is not None:
            return SetInterface(setup.index, setup.value)
        return Stall("unknown interface or alternate setting")
    if request == ch9.GET_INTERFACE and setup.is_in:
        if config is not None and setup.index in config.interfaces:
            return Reply(bytes([state.alternates.get(setup.index, 0)]))
        return Stall("unknown interface")
    if request == ch9.GET_STATUS and setup.is_in:
        if recipient == ch9.RECIP_DEVICE:
            return Reply(b"\0\0")  # bus powered, remote wakeup disabled
        if recipient == ch9.RECIP_INTERFACE:
            if config is not None and setup.index in config.interfaces:
                return Reply(b"\0\0")
            return Stall("unknown interface")
        if recipient == ch9.RECIP_ENDPOINT:
            address = setup.index & 0x8F
            if address & 0x0F == 0:
                return Reply(b"\0\0")
            if address in state.active_endpoints(presentation):
                return Reply(bytes([int(address in state.halted), 0]))
        return Stall("unknown status recipient")
    if request in (ch9.CLEAR_FEATURE, ch9.SET_FEATURE):
        if recipient == ch9.RECIP_ENDPOINT and setup.value == ch9.FEATURE_ENDPOINT_HALT:
            address = setup.index & 0x8F
            if address & 0x0F == 0:
                return Ack()
            if address in state.active_endpoints(presentation):
                return EndpointHalt(address, request == ch9.SET_FEATURE)
        return Stall("feature not supported (remote wakeup is not advertised)")
    if request in (ch9.SYNCH_FRAME, ch9.SET_DESCRIPTOR):
        return Stall("isochronous and SET_DESCRIPTOR are unsupported")
    return Forward()


def in_write_plan(
    results: list[tuple[int, int]], packet_size: int, limit: int
) -> tuple[int, int, bool]:
    """Coalesce completed IN URBs into one device write without changing packets.

    ``results`` holds ``(actual, requested)`` for consecutive completed URBs in
    submission order. Every included URB except the last must be full
    (``actual == requested``, a multiple of the packet size), so the UDC
    re-packetizes the concatenation into exactly the packets the device sent.
    Returns ``(count, total, zero)``; ``zero`` asks the UDC to append the
    zero-length packet that terminated the last URB.
    """
    count = total = 0
    zero = False
    for actual, requested in results:
        if count and total + actual > limit:
            break
        count += 1
        total += actual
        if actual < requested:
            zero = actual % packet_size == 0 and total > 0
            break
    return count, total, zero


_CBW = struct.Struct("<4sIIBBB16s")
CBW_SIZE = 31
CSW_SIZE = 13


def parse_cbw(packet: bytes | bytearray | memoryview) -> tuple[int, bool] | None:
    """Return ``(dCBWDataTransferLength, data_in)`` for a valid BOT CBW."""
    if len(packet) != CBW_SIZE:
        return None
    signature, _tag, length, flags, _lun, cb_length, _cb = _CBW.unpack(bytes(packet))
    if signature != b"USBC" or not 1 <= cb_length <= 16:
        return None
    return length, bool(flags & 0x80)


def chunk_sizes(length: int, limit: int) -> list[int]:
    """Split a known transfer into reads of at most ``limit`` bytes."""
    sizes = [limit] * (length // limit)
    if length % limit:
        sizes.append(length % limit)
    return sizes
