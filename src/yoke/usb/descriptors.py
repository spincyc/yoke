"""Descriptor parsing, supported-topology checks and presentation rewrites.

Pure functions (ADR 0021). The proxy fetches the remote device's
descriptors once at import and presents rewritten copies so that standard
GET_DESCRIPTOR requests meet the USB 2.0 §9.2.6.4 deadlines without a
network round trip. Every rewrite is listed in ``doc/design/USB-DATAPATH.md``:

* ``bMaxPacketSize0`` (device and qualifier) becomes the UDC's endpoint-zero
  size, because Raw Gadget packetizes control data at that size.
* Configuration ``bmAttributes`` declares bus power without remote wakeup;
  ``bMaxPower`` declares the computer-side box's own draw.
* The BOS USB 2.0 Extension LPM bit is cleared; link power management is a
  property of the local UDC, not the remote device.

A low-speed device is presented at full speed. Every valid low-speed
control/interrupt descriptor is valid at full speed with an unchanged
polling period, so endpoint descriptors are not rewritten; low-speed bulk or
isochronous endpoints are malformed and rejected.
"""

from __future__ import annotations

import struct
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from yoke.usb import ch9
from yoke.usb.ch9 import Speed

MAX_CONFIGS = 8
MAX_STRING_LANGS = 4
MAX_DESCRIPTOR = 0xFFFF


class DescriptorError(ValueError):
    """Malformed descriptor bytes from the remote device."""


class UnsupportedError(ValueError):
    """A well-formed device this proxy explicitly refuses to present."""


@dataclass(frozen=True, slots=True)
class Endpoint:
    address: int
    attributes: int
    max_packet: int
    interval: int
    raw: bytes

    @property
    def number(self) -> int:
        return self.address & 0x0F

    @property
    def is_in(self) -> bool:
        return bool(self.address & 0x80)

    @property
    def transfer_type(self) -> int:
        return self.attributes & 0x03

    @property
    def packet_size(self) -> int:
        return ch9.endpoint_packet_size(self.max_packet)

    @property
    def transfer_size(self) -> int:
        return ch9.endpoint_transfer_size(self.max_packet)


@dataclass(frozen=True, slots=True)
class AltSetting:
    interface: int
    alternate: int
    interface_class: int
    interface_subclass: int
    interface_protocol: int
    string_index: int
    endpoints: tuple[Endpoint, ...]
    hid_report_lengths: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class Configuration:
    value: int
    attributes: int
    max_power: int
    string_index: int
    interfaces: dict[int, dict[int, AltSetting]]
    raw: bytes

    def alt(self, interface: int, alternate: int) -> AltSetting | None:
        return self.interfaces.get(interface, {}).get(alternate)

    def all_endpoints(self) -> Iterator[Endpoint]:
        for alts in self.interfaces.values():
            for alt in alts.values():
                yield from alt.endpoints


@dataclass(frozen=True, slots=True)
class DeviceDescriptor:
    bcd_usb: int
    device_class: int
    device_subclass: int
    device_protocol: int
    max_packet0: int
    vendor: int
    product: int
    bcd_device: int
    i_manufacturer: int
    i_product: int
    i_serial: int
    num_configurations: int
    raw: bytes


_DEVICE = struct.Struct("<BBHBBBBHHHBBBB")


def parse_device(raw: bytes) -> DeviceDescriptor:
    if len(raw) < 18 or raw[0] < 18 or raw[1] != ch9.DT_DEVICE:
        raise DescriptorError("device descriptor is malformed")
    fields = _DEVICE.unpack(raw[:18])
    if fields[13] < 1 or fields[13] > MAX_CONFIGS:
        raise DescriptorError(f"bNumConfigurations {fields[13]} out of range")
    if fields[6] not in (8, 16, 32, 64, 9):
        raise DescriptorError(f"bMaxPacketSize0 {fields[6]} invalid")
    return DeviceDescriptor(
        bcd_usb=fields[2],
        device_class=fields[3],
        device_subclass=fields[4],
        device_protocol=fields[5],
        max_packet0=fields[6],
        vendor=fields[7],
        product=fields[8],
        bcd_device=fields[9],
        i_manufacturer=fields[10],
        i_product=fields[11],
        i_serial=fields[12],
        num_configurations=fields[13],
        raw=bytes(raw[:18]),
    )


def iter_descriptors(raw: bytes) -> Iterator[tuple[int, bytes]]:
    """Yield (type, bytes) for each TLV descriptor, validating bLength."""
    offset = 0
    while offset < len(raw):
        if len(raw) - offset < 2:
            raise DescriptorError("truncated descriptor header")
        length = raw[offset]
        if length < 2 or offset + length > len(raw):
            raise DescriptorError(f"descriptor bLength {length} at {offset} invalid")
        yield raw[offset + 1], raw[offset : offset + length]
        offset += length


def parse_configuration(raw: bytes) -> Configuration:
    """Parse a full configuration (or other-speed) descriptor set."""
    if (
        len(raw) < 9
        or raw[0] < 9
        or raw[1]
        not in (
            ch9.DT_CONFIG,
            ch9.DT_OTHER_SPEED_CONFIG,
        )
    ):
        raise DescriptorError("configuration descriptor is malformed")
    total = int.from_bytes(raw[2:4], "little")
    if total != len(raw):
        raise DescriptorError(f"wTotalLength {total} != {len(raw)} bytes received")
    num_interfaces = raw[4]
    interfaces: dict[int, dict[int, AltSetting]] = {}
    # (number, alternate, bNumEndpoints, class, subclass, protocol, iInterface)
    header: tuple[int, int, int, int, int, int, int] | None = None
    endpoints: list[Endpoint] = []
    hid: list[int] = []

    def finish() -> None:
        if header is None:
            return
        number, alt, expected = header[0], header[1], header[2]
        if len(endpoints) != expected:
            raise DescriptorError(
                f"interface {number} alt {alt}: {len(endpoints)} endpoints, "
                f"bNumEndpoints {expected}"
            )
        if alt in interfaces.setdefault(number, {}):
            raise DescriptorError(f"duplicate interface {number} alt {alt}")
        interfaces[number][alt] = AltSetting(
            number, alt, *header[3:], tuple(endpoints), tuple(hid)
        )

    for dtype, body in iter_descriptors(raw[raw[0] :]):
        if dtype == ch9.DT_INTERFACE:
            if len(body) < 9:
                raise DescriptorError("short interface descriptor")
            finish()
            endpoints, hid = [], []
            header = (body[2], body[3], body[4], body[5], body[6], body[7], body[8])
        elif dtype == ch9.DT_ENDPOINT:
            if header is None or len(body) < 7:
                raise DescriptorError("endpoint descriptor outside an interface")
            address = body[2]
            if address & 0x70 or address & 0x0F == 0:
                raise DescriptorError(f"endpoint address {address:#04x} invalid")
            endpoint = Endpoint(
                address,
                body[3],
                int.from_bytes(body[4:6], "little"),
                body[6],
                bytes(body),
            )
            if endpoint.packet_size == 0:
                raise DescriptorError(f"endpoint {address:#04x} has wMaxPacketSize 0")
            if any(e.address == address for e in endpoints):
                raise DescriptorError(f"duplicate endpoint {address:#04x}")
            endpoints.append(endpoint)
        elif dtype == ch9.DT_HID and header is not None and len(body) >= 9:
            for start in range(6, 6 + 3 * body[5], 3):
                if start + 3 > len(body):
                    raise DescriptorError("truncated HID descriptor")
                if body[start] == ch9.DT_HID_REPORT:
                    hid.append(int.from_bytes(body[start + 1 : start + 3], "little"))
    finish()
    if len(interfaces) != num_interfaces:
        raise DescriptorError(
            f"bNumInterfaces {num_interfaces} but {len(interfaces)} interfaces found"
        )
    return Configuration(
        value=raw[5],
        attributes=raw[7],
        max_power=raw[8],
        string_index=raw[6],
        interfaces=interfaces,
        raw=bytes(raw),
    )


@dataclass(frozen=True, slots=True)
class UdcBudget:
    """Non-control endpoints the UDC can expose at once (⚠ RK3566 unmeasured).

    ``max_number`` bounds endpoint addresses because Raw Gadget matches a
    descriptor's endpoint number to a fixed-function UDC endpoint (dwc3 names
    them ``epNin``/``epNout``). The runtime refines this from EPS_INFO.
    """

    max_in: int = 7
    max_out: int = 7
    max_number: int = 15

    def __post_init__(self) -> None:
        for value in (self.max_in, self.max_out, self.max_number):
            if type(value) is not int or not 0 <= value <= 15:
                raise ValueError("UDC endpoint budget values must be 0..15")


def check_speed(speed: int) -> None:
    if speed in (Speed.SUPER, Speed.SUPER_PLUS):
        raise UnsupportedError(
            "super-speed devices are unsupported (USB 2 first); attach the "
            "device through a USB 2 cable or hub so it enumerates at high speed"
        )
    if speed not in (Speed.LOW, Speed.FULL, Speed.HIGH):
        raise UnsupportedError(f"device speed {speed} is unsupported")


def check_supported(
    device: DeviceDescriptor,
    configs: list[Configuration],
    speed: int,
    budget: UdcBudget,
) -> None:
    """Raise :class:`UnsupportedError` naming the first unsupported property."""
    check_speed(speed)
    if device.device_class == ch9.CLASS_HUB:
        raise UnsupportedError("hubs are unsupported; export the device behind the hub")
    for config in configs:
        worst_in = worst_out = 0
        for number, alts in config.interfaces.items():
            ins = outs = 0
            for alt in alts.values():
                if alt.interface_class == ch9.CLASS_HUB:
                    raise UnsupportedError("hub interfaces are unsupported")
                alt_in = sum(1 for e in alt.endpoints if e.is_in)
                ins = max(ins, alt_in)
                outs = max(outs, len(alt.endpoints) - alt_in)
                for endpoint in alt.endpoints:
                    kind = endpoint.transfer_type
                    if kind == ch9.XFER_ISOC:
                        raise UnsupportedError(
                            f"isochronous endpoint {endpoint.address:#04x} "
                            f"(interface {number}) is unsupported"
                        )
                    if kind == ch9.XFER_CONTROL:
                        raise UnsupportedError(
                            f"extra control endpoint {endpoint.address:#04x} "
                            "is unsupported"
                        )
                    if speed == Speed.LOW and kind == ch9.XFER_BULK:
                        raise UnsupportedError("low-speed bulk endpoints are invalid")
                    if endpoint.number > budget.max_number:
                        raise UnsupportedError(
                            f"endpoint {endpoint.address:#04x} exceeds the UDC "
                            f"endpoint number limit {budget.max_number}"
                        )
            worst_in += ins
            worst_out += outs
        if worst_in > budget.max_in or worst_out > budget.max_out:
            raise UnsupportedError(
                f"configuration {config.value} needs {worst_in} IN and "
                f"{worst_out} OUT endpoints; UDC budget is {budget.max_in} IN "
                f"and {budget.max_out} OUT"
            )


def presented_speed(remote: int) -> Speed:
    """Low and full speed present at full speed; high speed at high speed."""
    check_speed(remote)
    return Speed.HIGH if remote == Speed.HIGH else Speed.FULL


def rewrite_device(raw: bytes, ep0_size: int) -> bytes:
    out = bytearray(raw)
    out[7] = ep0_size
    return bytes(out)


def rewrite_configuration(raw: bytes, power_ma: int) -> bytes:
    """Declare bus power, no remote wakeup, and the box's own current draw."""
    if not 0 <= power_ma <= 500:
        raise ValueError("USB 2 bus power must be 0..500 mA")
    out = bytearray(raw)
    out[7] = (out[7] | 0x80) & ~0x60 & 0xFF
    out[8] = (power_ma + 1) // 2
    return bytes(out)


def rewrite_bos(raw: bytes) -> bytes:
    """Clear USB 2.0 Extension LPM support in a BOS descriptor set."""
    out = bytearray(raw)
    offset = raw[0] if raw else 0
    for dtype, body in iter_descriptors(bytes(raw[offset:])):
        if dtype == ch9.DT_DEVICE_CAPABILITY and len(body) >= 7 and body[2] == 0x02:
            out[offset + 3] &= ~0x02 & 0xFF
        offset += len(body)
    return bytes(out)


Fetch = Callable[[ch9.Setup], bytes | None]
"""Perform a control IN request on the remote; ``None`` means STALL."""


@dataclass(frozen=True)
class Presentation:
    """Everything needed to answer standard requests without the network."""

    remote_speed: Speed
    speed: Speed
    device: DeviceDescriptor
    device_raw: bytes
    configurations: tuple[Configuration, ...]
    config_raw: tuple[bytes, ...]
    qualifier: bytes | None
    other_speed: bytes | None
    bos: bytes | None
    strings: dict[tuple[int, int], bytes | None] = field(default_factory=dict)
    class_descriptors: dict[tuple[int, int, int], bytes] = field(default_factory=dict)

    def configuration(self, value: int) -> Configuration | None:
        for config in self.configurations:
            if config.value == value:
                return config
        return None

    def descriptor(self, setup: ch9.Setup) -> tuple[bool, bytes | None]:
        """Answer a device-recipient GET_DESCRIPTOR: (known, data-or-STALL)."""
        dtype, index = setup.descriptor_type, setup.descriptor_index
        if dtype == ch9.DT_DEVICE:
            return True, self.device_raw
        if dtype == ch9.DT_CONFIG:
            if index < len(self.config_raw):
                return True, self.config_raw[index]
            return True, None
        if dtype == ch9.DT_DEVICE_QUALIFIER:
            return True, self.qualifier
        if dtype == ch9.DT_OTHER_SPEED_CONFIG:
            return True, self.other_speed if index == 0 else None
        if dtype == ch9.DT_BOS:
            return True, self.bos
        if dtype == ch9.DT_STRING and (index, setup.index) in self.strings:
            return True, self.strings[(index, setup.index)]
        return False, None


def _fetch_full(fetch: Fetch, dtype: int, index: int = 0) -> bytes | None:
    """Read a variable-length descriptor: header first, then wTotalLength."""
    head = fetch(ch9.get_descriptor(dtype, index, 9 if dtype != ch9.DT_BOS else 5))
    if head is None:
        return None
    if len(head) < 4:
        raise DescriptorError(f"descriptor type {dtype} header is truncated")
    total = int.from_bytes(head[2:4], "little")
    if total < len(head) or total > MAX_DESCRIPTOR:
        raise DescriptorError(f"descriptor type {dtype} wTotalLength {total}")
    full = fetch(ch9.get_descriptor(dtype, index, total))
    if full is None or len(full) != total:
        raise DescriptorError(f"descriptor type {dtype} returned a short read")
    return full


def build_presentation(
    fetch: Fetch,
    remote_speed: int,
    *,
    power_ma: int,
    ep0_size: int = 64,
    budget: UdcBudget | None = None,
) -> Presentation:
    """Fetch, validate and rewrite the remote device's descriptors."""
    speed = presented_speed(remote_speed)
    device_raw = fetch(ch9.get_descriptor(ch9.DT_DEVICE, 0, 18))
    if device_raw is None:
        raise DescriptorError("device stalled GET_DESCRIPTOR(DEVICE)")
    device = parse_device(device_raw)
    configs: list[Configuration] = []
    config_raw: list[bytes] = []
    for index in range(device.num_configurations):
        raw = _fetch_full(fetch, ch9.DT_CONFIG, index)
        if raw is None:
            raise DescriptorError(f"configuration {index} stalled")
        config = parse_configuration(raw)
        configs.append(config)
        config_raw.append(rewrite_configuration(raw, power_ma))
    check_supported(device, configs, remote_speed, budget or UdcBudget())
    qualifier = other = bos = None
    if device.bcd_usb >= 0x0200:
        qualifier = fetch(ch9.get_descriptor(ch9.DT_DEVICE_QUALIFIER, 0, 10))
        if qualifier is not None:
            if len(qualifier) < 10 or qualifier[1] != ch9.DT_DEVICE_QUALIFIER:
                raise DescriptorError("device qualifier is malformed")
            qualifier = rewrite_device(qualifier[:10], ep0_size)
            raw_other = _fetch_full(fetch, ch9.DT_OTHER_SPEED_CONFIG, 0)
            if raw_other is not None:
                parse_configuration(raw_other)
                other = rewrite_configuration(raw_other, power_ma)
    if device.bcd_usb >= 0x0201:
        raw_bos = _fetch_full(fetch, ch9.DT_BOS, 0)
        if raw_bos is not None:
            list(iter_descriptors(raw_bos[raw_bos[0] :]))
            bos = rewrite_bos(raw_bos)
    strings: dict[tuple[int, int], bytes | None] = {}
    wanted = {device.i_manufacturer, device.i_product, device.i_serial}
    for config in configs:
        wanted.add(config.string_index)
        for alts in config.interfaces.values():
            wanted.update(alt.string_index for alt in alts.values())
    wanted.discard(0)
    langs_raw = fetch(ch9.get_descriptor(ch9.DT_STRING, 0, 255)) if wanted else None
    if langs_raw is not None:
        strings[(0, 0)] = langs_raw
        langs = [
            int.from_bytes(langs_raw[i : i + 2], "little")
            for i in range(2, min(len(langs_raw), langs_raw[0]) - 1, 2)
        ][:MAX_STRING_LANGS]
        for lang in langs:
            for index in sorted(wanted):
                strings[(index, lang)] = fetch(
                    ch9.get_descriptor(ch9.DT_STRING, index, 255, lang=lang)
                )
    class_descriptors: dict[tuple[int, int, int], bytes] = {}
    for config in configs:
        for number, alts in config.interfaces.items():
            for alt in alts.values():
                for report_index, length in enumerate(alt.hid_report_lengths):
                    key = (number, ch9.DT_HID_REPORT, report_index)
                    if key in class_descriptors or length == 0:
                        continue
                    data = fetch(
                        ch9.get_descriptor(
                            ch9.DT_HID_REPORT,
                            report_index,
                            length,
                            lang=number,
                            recip=ch9.RECIP_INTERFACE,
                        )
                    )
                    if data is not None:
                        class_descriptors[key] = data
    return Presentation(
        remote_speed=Speed(remote_speed),
        speed=speed,
        device=device,
        device_raw=rewrite_device(device.raw, ep0_size),
        configurations=tuple(configs),
        config_raw=tuple(config_raw),
        qualifier=qualifier,
        other_speed=other,
        bos=bos,
        strings=strings,
        class_descriptors=class_descriptors,
    )
