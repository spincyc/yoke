"""Descriptor byte builders for simulated devices (USB 2.0 chapter 9 layouts)."""

from __future__ import annotations

import struct


def device(
    *,
    vendor: int,
    product: int,
    bcd_usb: int = 0x0200,
    device_class: int = 0,
    subclass: int = 0,
    protocol: int = 0,
    max_packet0: int = 64,
    bcd_device: int = 0x0100,
    manufacturer: int = 1,
    product_string: int = 2,
    serial: int = 3,
    configurations: int = 1,
) -> bytes:
    return struct.pack(
        "<BBHBBBBHHHBBBB",
        18,
        1,
        bcd_usb,
        device_class,
        subclass,
        protocol,
        max_packet0,
        vendor,
        product,
        bcd_device,
        manufacturer,
        product_string,
        serial,
        configurations,
    )


def qualifier(
    *, bcd_usb: int = 0x0200, device_class: int = 0, max_packet0: int = 64
) -> bytes:
    return struct.pack(
        "<BBHBBBBBB", 10, 6, bcd_usb, device_class, 0, 0, max_packet0, 1, 0
    )


def interface(
    number: int,
    *,
    alternate: int = 0,
    endpoints: int,
    cls: int,
    subclass: int = 0,
    protocol: int = 0,
    string: int = 0,
) -> bytes:
    return bytes([9, 4, number, alternate, endpoints, cls, subclass, protocol, string])


def endpoint(address: int, kind: int, max_packet: int, interval: int = 0) -> bytes:
    """``kind``: 2 bulk, 3 interrupt, 1 isochronous."""
    return struct.pack("<BBBBHB", 7, 5, address, kind, max_packet, interval)


def hid(report_length: int, *, country: int = 0) -> bytes:
    return struct.pack("<BBHBBBH", 9, 0x21, 0x0111, country, 1, 0x22, report_length)


def iad(
    first: int, count: int, cls: int, subclass: int = 0, protocol: int = 0
) -> bytes:
    return bytes([8, 11, first, count, cls, subclass, protocol, 0])


def configuration(
    body: bytes,
    *,
    interfaces: int,
    value: int = 1,
    attributes: int = 0x80,
    max_power_ma: int = 100,
    string: int = 0,
    other_speed: bool = False,
) -> bytes:
    total = 9 + len(body)
    head = struct.pack(
        "<BBHBBBBB",
        9,
        7 if other_speed else 2,
        total,
        interfaces,
        value,
        string,
        attributes,
        max_power_ma // 2,
    )
    return head + body


def string(text: str) -> bytes:
    raw = text.encode("utf-16-le")
    return bytes([2 + len(raw), 3]) + raw


def langids(*ids: int) -> bytes:
    return bytes([2 + 2 * len(ids), 3]) + b"".join(struct.pack("<H", i) for i in ids)


def bos(*capabilities: bytes) -> bytes:
    body = b"".join(capabilities)
    return struct.pack("<BBHB", 5, 15, 5 + len(body), len(capabilities)) + body


def usb2_extension(lpm: bool = True) -> bytes:
    return struct.pack("<BBBI", 7, 16, 2, 0x02 if lpm else 0)
