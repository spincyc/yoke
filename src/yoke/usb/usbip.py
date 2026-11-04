"""USB/IP protocol version 1.1.1 (0x0111) codec. Pure: no sockets.

Normative reference: https://docs.kernel.org/usb/usbip_protocol.html.
All fields are big endian. The 48-byte ``usbip_header`` is shared by
CMD_SUBMIT, RET_SUBMIT, CMD_UNLINK and RET_UNLINK. ISO transfers are not
supported by this implementation (ADR 0021), so iso_packet_descriptor
arrays are never produced and a peer that sends one for a non-ISO URB is
a protocol error handled by the connection layer.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

USBIP_VERSION = 0x0111

OP_REQ_DEVLIST = 0x8005
OP_REP_DEVLIST = 0x0005
OP_REQ_IMPORT = 0x8003
OP_REP_IMPORT = 0x0003

CMD_SUBMIT = 0x00000001
CMD_UNLINK = 0x00000002
RET_SUBMIT = 0x00000003
RET_UNLINK = 0x00000004

DIR_OUT = 0
DIR_IN = 1

# include/uapi/linux/usbip.h transfer flags
URB_SHORT_NOT_OK = 0x0001
URB_ISO_ASAP = 0x0002
URB_NO_TRANSFER_DMA_MAP = 0x0004
URB_ZERO_PACKET = 0x0040
URB_NO_INTERRUPT = 0x0080
URB_FREE_BUFFER = 0x0100
URB_DIR_IN = 0x0200

NON_ISO_PACKETS = 0xFFFFFFFF
"""The documented ``number_of_packets`` for a non-ISO CMD_SUBMIT."""

HEADER_SIZE = 48
OP_HEADER_SIZE = 8
DEVICE_INFO_SIZE = 312
INTERFACE_INFO_SIZE = 4
BUSID_SIZE = 32
PATH_SIZE = 256
MAX_TRANSFER = 16 * 1024 * 1024
"""Bound on accepted transfer_buffer_length/actual_length (memory safety)."""

# Linux errno values as carried in RET_SUBMIT/RET_UNLINK status. The exporter
# is Linux (asm-generic numbering on arm64 and x86), so these are wire values,
# not the local platform's errno module.
EPERM = 1
ENOENT = 2
EINTR = 4
EIO = 5
ENOMEM = 12
EBUSY = 16
ENODEV = 19
EINVAL = 22
EPIPE = 32
ETIME = 62
EPROTO = 71
EOVERFLOW = 75
EILSEQ = 84
ECONNRESET = 104
ESHUTDOWN = 108
ETIMEDOUT = 110
EINPROGRESS = 115
EREMOTEIO = 121

_OP = struct.Struct(">HHI")
_BASIC = struct.Struct(">IIIII")
_SUBMIT = struct.Struct(">IIIIIIiiii8s")
_RET_SUBMIT = struct.Struct(">IIIIIiiiii8x")
_UNLINK = struct.Struct(">IIIIII24x")
_RET_UNLINK = struct.Struct(">IIIIIi24x")
_DEVICE = struct.Struct(">256s32sIIIHHHBBBBBB")
_INTERFACE = struct.Struct(">BBBx")

assert _SUBMIT.size == _RET_SUBMIT.size == _UNLINK.size == HEADER_SIZE
assert _RET_UNLINK.size == HEADER_SIZE and _DEVICE.size == DEVICE_INFO_SIZE


class ProtocolError(ValueError):
    """Malformed or unsupported USB/IP bytes; the connection must close."""


def _cstr(raw: bytes, name: str) -> str:
    text, nul, _ = raw.partition(b"\0")
    if not nul:
        raise ProtocolError(f"{name} is not NUL terminated")
    try:
        return text.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ProtocolError(f"{name} is not ASCII") from exc


def _fixed(text: str, size: int, name: str) -> bytes:
    raw = text.encode("ascii")
    if len(raw) >= size:
        raise ValueError(f"{name} longer than {size - 1} bytes")
    return raw.ljust(size, b"\0")


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """``usbip_usb_device`` as carried by OP_REP_IMPORT and OP_REP_DEVLIST."""

    path: str
    busid: str
    busnum: int
    devnum: int
    speed: int
    vendor: int
    product: int
    bcd_device: int
    device_class: int
    device_subclass: int
    device_protocol: int
    configuration_value: int
    num_configurations: int
    num_interfaces: int
    interfaces: tuple[tuple[int, int, int], ...] = field(default=())

    @property
    def devid(self) -> int:
        """Client-side ``devid``: ``(busnum << 16) | devnum``."""
        return ((self.busnum & 0xFFFF) << 16) | (self.devnum & 0xFFFF)

    def pack(self, *, with_interfaces: bool = False) -> bytes:
        body = _DEVICE.pack(
            _fixed(self.path, PATH_SIZE, "path"),
            _fixed(self.busid, BUSID_SIZE, "busid"),
            self.busnum,
            self.devnum,
            self.speed,
            self.vendor,
            self.product,
            self.bcd_device,
            self.device_class,
            self.device_subclass,
            self.device_protocol,
            self.configuration_value,
            self.num_configurations,
            self.num_interfaces,
        )
        if with_interfaces:
            if len(self.interfaces) != self.num_interfaces:
                raise ValueError("interface list does not match bNumInterfaces")
            body += b"".join(_INTERFACE.pack(*i) for i in self.interfaces)
        return body

    @classmethod
    def unpack(cls, raw: bytes | bytearray | memoryview) -> DeviceInfo:
        if len(raw) != DEVICE_INFO_SIZE:
            raise ProtocolError("device record must be 312 bytes")
        fields = _DEVICE.unpack(raw)
        return cls(_cstr(fields[0], "path"), _cstr(fields[1], "busid"), *fields[2:])


def op_header(code: int, status: int = 0) -> bytes:
    return _OP.pack(USBIP_VERSION, code, status)


def parse_op_header(raw: bytes | bytearray | memoryview, expected: int) -> int:
    """Validate an operation header; return its status field."""
    version, code, status = _OP.unpack(raw)
    if version != USBIP_VERSION:
        raise ProtocolError(f"unsupported USB/IP version {version:#06x}")
    if code != expected:
        raise ProtocolError(f"expected op {expected:#06x}, got {code:#06x}")
    return int(status)


def encode_req_import(busid: str) -> bytes:
    return op_header(OP_REQ_IMPORT) + _fixed(busid, BUSID_SIZE, "busid")


def decode_req_import(body: bytes | bytearray | memoryview) -> str:
    if len(body) != BUSID_SIZE:
        raise ProtocolError("OP_REQ_IMPORT busid must be 32 bytes")
    return _cstr(bytes(body), "busid")


def encode_rep_import(info: DeviceInfo | None) -> bytes:
    if info is None:
        return op_header(OP_REP_IMPORT, 1)
    return op_header(OP_REP_IMPORT, 0) + info.pack()


def encode_req_devlist() -> bytes:
    return op_header(OP_REQ_DEVLIST)


def encode_rep_devlist(devices: list[DeviceInfo]) -> bytes:
    return (
        op_header(OP_REP_DEVLIST)
        + struct.pack(">I", len(devices))
        + b"".join(d.pack(with_interfaces=True) for d in devices)
    )


@dataclass(frozen=True, slots=True)
class CmdSubmit:
    seqnum: int
    devid: int
    direction: int
    ep: int
    transfer_flags: int
    transfer_buffer_length: int
    interval: int = 0
    setup: bytes = b"\0" * 8
    start_frame: int = 0
    number_of_packets: int = NON_ISO_PACKETS

    def pack(self) -> bytes:
        if len(self.setup) != 8:
            raise ValueError("setup must be 8 bytes")
        if not 0 <= self.transfer_buffer_length <= MAX_TRANSFER:
            raise ValueError("transfer_buffer_length out of range")
        packets = self.number_of_packets
        return _SUBMIT.pack(
            CMD_SUBMIT,
            self.seqnum,
            self.devid,
            self.direction,
            self.ep,
            self.transfer_flags,
            self.transfer_buffer_length,
            self.start_frame,
            packets - (1 << 32) if packets >= 1 << 31 else packets,
            self.interval,
            self.setup,
        )


@dataclass(frozen=True, slots=True)
class RetSubmit:
    seqnum: int
    status: int
    actual_length: int
    start_frame: int = 0
    number_of_packets: int = NON_ISO_PACKETS
    error_count: int = 0

    def pack(self) -> bytes:
        packets = self.number_of_packets
        return _RET_SUBMIT.pack(
            RET_SUBMIT,
            self.seqnum,
            0,
            0,
            0,
            self.status,
            self.actual_length,
            self.start_frame,
            packets - (1 << 32) if packets >= 1 << 31 else packets,
            self.error_count,
        )


@dataclass(frozen=True, slots=True)
class CmdUnlink:
    seqnum: int
    devid: int
    unlink_seqnum: int

    def pack(self) -> bytes:
        return _UNLINK.pack(
            CMD_UNLINK, self.seqnum, self.devid, 0, 0, self.unlink_seqnum
        )


@dataclass(frozen=True, slots=True)
class RetUnlink:
    seqnum: int
    status: int

    def pack(self) -> bytes:
        return _RET_UNLINK.pack(RET_UNLINK, self.seqnum, 0, 0, 0, self.status)


Message = CmdSubmit | RetSubmit | CmdUnlink | RetUnlink


def decode_header(raw: bytes | bytearray | memoryview) -> Message:
    """Decode one 48-byte ``usbip_header`` (payload follows separately)."""
    if len(raw) != HEADER_SIZE:
        raise ProtocolError("USB/IP header must be 48 bytes")
    command, seqnum, devid, direction, ep = _BASIC.unpack_from(raw)
    if command == CMD_SUBMIT:
        values = _SUBMIT.unpack(raw)
        if direction not in (DIR_OUT, DIR_IN) or ep > 15:
            raise ProtocolError("CMD_SUBMIT direction/endpoint out of range")
        length = values[6]
        if not 0 <= length <= MAX_TRANSFER:
            raise ProtocolError("CMD_SUBMIT transfer_buffer_length out of range")
        return CmdSubmit(
            seqnum=seqnum,
            devid=devid,
            direction=direction,
            ep=ep,
            transfer_flags=values[5],
            transfer_buffer_length=length,
            start_frame=values[7],
            number_of_packets=values[8] & 0xFFFFFFFF,
            interval=values[9],
            setup=values[10],
        )
    if command == RET_SUBMIT:
        values = _RET_SUBMIT.unpack(raw)
        actual = values[6]
        if not 0 <= actual <= MAX_TRANSFER:
            raise ProtocolError("RET_SUBMIT actual_length out of range")
        return RetSubmit(
            seqnum=seqnum,
            status=values[5],
            actual_length=actual,
            start_frame=values[7],
            number_of_packets=values[8] & 0xFFFFFFFF,
            error_count=values[9],
        )
    if command == CMD_UNLINK:
        values = _UNLINK.unpack(raw)
        return CmdUnlink(seqnum=seqnum, devid=devid, unlink_seqnum=values[5])
    if command == RET_UNLINK:
        values = _RET_UNLINK.unpack(raw)
        return RetUnlink(seqnum=seqnum, status=values[5])
    raise ProtocolError(f"unknown USB/IP command {command:#010x}")


def submit_flags(direction: int, *, zero_packet: bool = False) -> int:
    """Transfer flags a Linux client would send for a non-ISO URB."""
    flags = URB_DIR_IN if direction == DIR_IN else 0
    if zero_packet and direction == DIR_OUT:
        flags |= URB_ZERO_PACKET
    return flags
