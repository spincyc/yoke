"""Classic pcap and Linux usbmon record codec (stdlib only, Python >= 3.9).

A trace is a classic pcap file whose link type is LINKTYPE_USB_LINUX_MMAPPED
(220, a 64-byte usbmon header per record) or LINKTYPE_USB_LINUX (189, the
older 48-byte header). Each record is one usbmon event: the header followed
by the captured data area (``len_cap`` bytes, possibly truncated by the
capture's snap length). For link type 220 the data area starts with
``ndesc`` 16-byte isochronous descriptors.

The usbmon header is written in the capturing host's byte order, which is
also the byte order of the pcap file header (the libpcap convention), so a
reader decodes both with the order the magic number reveals.
See tools/usbtrace/README.md for the field list.
"""

from __future__ import annotations

import struct
import sys
from collections.abc import Iterator
from dataclasses import dataclass, replace
from typing import BinaryIO

LINKTYPE_USB_LINUX = 189
LINKTYPE_USB_LINUX_MMAPPED = 220
LINKTYPES = (LINKTYPE_USB_LINUX, LINKTYPE_USB_LINUX_MMAPPED)

MAGIC_USEC = 0xA1B2C3D4
MAGIC_NSEC = 0xA1B23C4D
PCAPNG_MAGIC = 0x0A0D0D0A
NATIVE_ORDER = "<" if sys.byteorder == "little" else ">"

# usbmon transfer types (struct usbmon_packet.xfer_type) differ from the USB
# endpoint encoding (bmAttributes & 3) used everywhere else in this tool.
URB_ISOCHRONOUS, URB_INTERRUPT, URB_CONTROL, URB_BULK = 0, 1, 2, 3
USB_CONTROL, USB_ISOCHRONOUS, USB_BULK, USB_INTERRUPT = 0, 1, 2, 3
USBMON_TO_USB = {
    URB_ISOCHRONOUS: USB_ISOCHRONOUS,
    URB_INTERRUPT: USB_INTERRUPT,
    URB_CONTROL: USB_CONTROL,
    URB_BULK: USB_BULK,
}
USB_TO_USBMON = {usb: mon for mon, usb in USBMON_TO_USB.items()}
XFER_NAMES = {
    USB_CONTROL: "control",
    USB_ISOCHRONOUS: "isochronous",
    USB_BULK: "bulk",
    USB_INTERRUPT: "interrupt",
}

# struct usbmon_packet: id, type, xfer_type, epnum, devnum, busnum,
# flag_setup, flag_data, ts_sec, ts_usec, status, length, len_cap, setup[8]
# (48 bytes, link type 189) then interval, start_frame, xfer_flags, ndesc
# (64 bytes, link type 220).
_HEADER_48 = "QBBBBHBBqiiII8s"
_HEADER_64 = _HEADER_48 + "iiII"
HEADER_48 = struct.calcsize("<" + _HEADER_48)
HEADER_64 = struct.calcsize("<" + _HEADER_64)
_ISODESC = "iIII"  # iso_status, iso_off, iso_len, padding
ISODESC_SIZE = struct.calcsize("<" + _ISODESC)
FLAG_PRESENT = 0  # flag_setup/flag_data value when the field carries data
NO_SETUP = b"\x00" * 8


@dataclass(frozen=True)
class IsoDesc:
    status: int
    offset: int
    length: int


@dataclass(frozen=True)
class UsbmonEvent:
    """One usbmon event; ``data`` holds the captured payload bytes only."""

    urb_id: int
    kind: str  # "S" submit, "C" complete, "E" submission error
    xfer_type: int  # usbmon encoding (URB_*)
    epnum: int  # endpoint number, 0x80 set for IN
    devnum: int
    busnum: int
    flag_setup: int
    flag_data: int
    ts_sec: int
    ts_usec: int
    status: int
    length: int  # submitted (S) or actual (C) transfer length
    len_cap: int  # data area length delivered by usbmon
    setup: bytes = NO_SETUP  # setup packet, or iso error_count/numdesc
    interval: int = 0
    start_frame: int = 0
    xfer_flags: int = 0
    ndesc: int = 0
    isodescs: tuple[IsoDesc, ...] = ()
    data: bytes = b""

    @property
    def ts_ns(self) -> int:
        return self.ts_sec * 1_000_000_000 + self.ts_usec * 1000

    @property
    def is_in(self) -> bool:
        return bool(self.epnum & 0x80)

    @property
    def usb_xfer(self) -> int:
        return USBMON_TO_USB[self.xfer_type]


def header_size(linktype: int) -> int:
    if linktype == LINKTYPE_USB_LINUX_MMAPPED:
        return HEADER_64
    if linktype == LINKTYPE_USB_LINUX:
        return HEADER_48
    raise ValueError(f"unsupported link type {linktype} (want 189 or 220)")


def pack_header(event: UsbmonEvent, *, mmapped: bool = True, order: str = "<") -> bytes:
    fields = [
        event.urb_id,
        ord(event.kind),
        event.xfer_type,
        event.epnum,
        event.devnum,
        event.busnum,
        event.flag_setup,
        event.flag_data,
        event.ts_sec,
        event.ts_usec,
        event.status,
        event.length,
        event.len_cap,
        event.setup,
    ]
    if not mmapped:
        return struct.pack(order + _HEADER_48, *fields)
    fields += [event.interval, event.start_frame, event.xfer_flags, event.ndesc]
    return struct.pack(order + _HEADER_64, *fields)


def unpack_header(buf: bytes, *, mmapped: bool = True, order: str = "<") -> UsbmonEvent:
    """Decode a header; ``isodescs`` and ``data`` stay empty."""
    layout = order + (_HEADER_64 if mmapped else _HEADER_48)
    size = struct.calcsize(layout)
    if len(buf) < size:
        raise ValueError(f"usbmon header needs {size} bytes, got {len(buf)}")
    values = struct.unpack_from(layout, buf)
    kind = chr(values[1])
    if kind not in "SCE":
        raise ValueError(f"unknown usbmon event type {kind!r}")
    if values[2] not in USBMON_TO_USB:
        raise ValueError(f"unknown usbmon transfer type {values[2]}")
    tail = values[14:] if mmapped else (0, 0, 0, 0)
    return UsbmonEvent(
        urb_id=values[0],
        kind=kind,
        xfer_type=values[2],
        epnum=values[3],
        devnum=values[4],
        busnum=values[5],
        flag_setup=values[6],
        flag_data=values[7],
        ts_sec=values[8],
        ts_usec=values[9],
        status=values[10],
        length=values[11],
        len_cap=values[12],
        setup=values[13],
        interval=tail[0],
        start_frame=tail[1],
        xfer_flags=tail[2],
        ndesc=tail[3],
    )


def pack_record(event: UsbmonEvent, *, mmapped: bool = True, order: str = "<") -> bytes:
    """Header + iso descriptors + data. ``len_cap`` is recomputed from them."""
    descs = b"".join(
        struct.pack(order + _ISODESC, d.status, d.offset, d.length, 0)
        for d in event.isodescs
    )
    if not mmapped and descs:
        raise ValueError("link type 189 cannot carry isochronous descriptors")
    fixed = replace(
        event,
        len_cap=len(descs) + len(event.data),
        ndesc=len(event.isodescs) if mmapped else 0,
    )
    return pack_header(fixed, mmapped=mmapped, order=order) + descs + event.data


def parse_record(buf: bytes, linktype: int, order: str = "<") -> UsbmonEvent:
    """Decode one pcap record body. A truncated data area is kept as-is."""
    mmapped = linktype == LINKTYPE_USB_LINUX_MMAPPED
    size = header_size(linktype)
    event = unpack_header(buf, mmapped=mmapped, order=order)
    area = buf[size : size + event.len_cap]
    descs: list[IsoDesc] = []
    if mmapped and event.ndesc:
        take = min(event.ndesc, len(area) // ISODESC_SIZE)
        for i in range(take):
            status, offset, length, _pad = struct.unpack_from(
                order + _ISODESC, area, i * ISODESC_SIZE
            )
            descs.append(IsoDesc(status, offset, length))
        area = area[event.ndesc * ISODESC_SIZE :]
    return replace(event, isodescs=tuple(descs), data=bytes(area))


@dataclass(frozen=True)
class PcapRecord:
    ts_ns: int
    data: bytes
    orig_len: int


@dataclass(frozen=True)
class PcapInfo:
    linktype: int
    nanosecond: bool
    order: str
    snaplen: int


class PcapWriter:
    """Classic pcap (version 2.4) writer in the given byte order."""

    def __init__(
        self,
        stream: BinaryIO,
        *,
        linktype: int = LINKTYPE_USB_LINUX_MMAPPED,
        snaplen: int = 0x40000,
        nanosecond: bool = False,
        order: str = "<",
    ) -> None:
        self.stream = stream
        self.order = order
        self.nanosecond = nanosecond
        self.linktype = linktype
        self.bytes_written = 0
        magic = MAGIC_NSEC if nanosecond else MAGIC_USEC
        self._write(
            struct.pack(order + "IHHiIII", magic, 2, 4, 0, 0, snaplen, linktype)
        )

    def _write(self, blob: bytes) -> None:
        self.stream.write(blob)
        self.bytes_written += len(blob)

    def write(self, ts_ns: int, data: bytes, orig_len: int | None = None) -> None:
        seconds, rest = divmod(ts_ns, 1_000_000_000)
        frac = rest if self.nanosecond else rest // 1000
        original = len(data) if orig_len is None else max(orig_len, len(data))
        self._write(
            struct.pack(self.order + "IIII", seconds, frac, len(data), original)
        )
        self._write(data)

    def write_event(self, event: UsbmonEvent) -> None:
        mmapped = self.linktype == LINKTYPE_USB_LINUX_MMAPPED
        self.write(event.ts_ns, pack_record(event, mmapped=mmapped, order=self.order))


class PcapReader:
    """Classic pcap reader for either byte order and timestamp resolution."""

    def __init__(self, stream: BinaryIO) -> None:
        self.stream = stream
        head = stream.read(24)
        if len(head) < 24:
            raise ValueError("not a pcap file (short global header)")
        for order in ("<", ">"):
            (magic,) = struct.unpack(order + "I", head[:4])
            if magic in (MAGIC_USEC, MAGIC_NSEC):
                break
            if magic == PCAPNG_MAGIC:
                raise ValueError(
                    "pcapng is not supported: capture with `dumpcap -P`, or convert "
                    "with `editcap -F pcap in.pcapng out.pcap`"
                )
        else:
            raise ValueError("not a classic pcap file (unknown magic)")
        _major, _minor, _zone, _sigfigs, snaplen, network = struct.unpack(
            order + "HHiIII", head[4:]
        )
        self.info = PcapInfo(
            linktype=network & 0x0FFFFFFF,
            nanosecond=magic == MAGIC_NSEC,
            order=order,
            snaplen=snaplen,
        )

    def __iter__(self) -> Iterator[PcapRecord]:
        scale = 1 if self.info.nanosecond else 1000
        while True:
            head = self.stream.read(16)
            if not head:
                return
            if len(head) < 16:
                raise ValueError("truncated pcap record header")
            seconds, frac, incl, orig = struct.unpack(self.info.order + "IIII", head)
            data = self.stream.read(incl)
            if len(data) < incl:
                raise ValueError("truncated pcap record body")
            yield PcapRecord(seconds * 1_000_000_000 + frac * scale, data, orig)


def read_events(stream: BinaryIO) -> Iterator[UsbmonEvent]:
    """All usbmon events of a pcap stream, in file order."""
    reader = PcapReader(stream)
    header_size(reader.info.linktype)  # rejects other link types early
    for record in reader:
        yield parse_record(record.data, reader.info.linktype, reader.info.order)
