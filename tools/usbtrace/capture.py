#!/usr/bin/env python3
"""Capture Linux usbmon events into pcap (LINKTYPE_USB_LINUX_MMAPPED, 220).

Runs as root with the system python3 (>= 3.9, stdlib only); needs only
pcapfmt.py next to it. ``modprobe usbmon`` first. The kernel interface is
documented in Documentation/usb/usbmon.rst: each MON_IOCX_GETX call moves one
event (a 64-byte struct usbmon_packet plus its data area) out of the
per-reader ring. The ring is enlarged with MON_IOCT_RING_SIZE because usbmon
caps each event's data at a fifth of the ring size. Kernels differ in the
largest ring they accept: current mainline takes up to 64 MiB, older and BSP
kernels only 1200 KiB. A ring size the kernel rejects with EINVAL is retried
once at 1200 KiB (240 KiB per event, still a whole 64 KiB URB).

Everything but UsbmonDevice is pure and tested offline.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import os
import select
import signal
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from types import FrameType
from typing import Any, BinaryIO, Protocol

import pcapfmt

# <asm-generic/ioctl.h> encoding used by x86, arm and arm64 (not alpha, mips,
# powerpc or sparc): dir(2) << 30 | size(14) << 16 | type(8) << 8 | nr(8).
_IOC_NONE, _IOC_WRITE, _IOC_READ = 0, 1, 2
MON_IOC_MAGIC = 0x92


def _ioc(direction: int, nr: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (MON_IOC_MAGIC << 8) | nr


class MonGetArg(ctypes.Structure):
    """struct mon_bin_get {hdr pointer; data pointer; size_t alloc} (GETX)."""

    _fields_ = (
        ("hdr", ctypes.c_void_p),
        ("data", ctypes.c_void_p),
        ("alloc", ctypes.c_size_t),
    )


MON_STATS_SIZE = 8  # struct mon_bin_stats {u32 queued; u32 dropped;}
MON_IOCG_STATS = _ioc(_IOC_READ, 3, MON_STATS_SIZE)
MON_IOCT_RING_SIZE = _ioc(_IOC_NONE, 4, 0)
MON_IOCQ_RING_SIZE = _ioc(_IOC_NONE, 5, 0)
MON_IOCX_GETX = _ioc(_IOC_WRITE, 10, ctypes.sizeof(MonGetArg))

RING_MIN = 8 * 1024  # BUFF_MIN in drivers/usb/mon/mon_bin.c
RING_MAX = 64 * 1024 * 1024  # BUFF_MAX in current mainline
RING_FALLBACK = 1200 * 1024  # BUFF_MAX in older and BSP kernels
USBMON_PATH = "/dev/usbmon{}"
ISODESC_MAX = 128  # descriptors usbmon copies per event
DEFAULT_RING = 32 * 1024 * 1024


def event_alloc(ring_size: int) -> int:
    """Data buffer that never truncates an event: ring/5 data + descriptors."""
    return ring_size // 5 + ISODESC_MAX * pcapfmt.ISODESC_SIZE


@dataclass
class CaptureStats:
    events: int = 0
    records: int = 0
    filtered: int = 0
    truncated: int = 0
    bytes_written: int = 0
    kernel_dropped: int = 0
    stopped_by: str = ""


def event_to_record(
    header: bytes,
    data: bytes,
    *,
    devnum: int | None = None,
    snaplen: int = 0,
    order: str = pcapfmt.NATIVE_ORDER,
) -> tuple[int, bytes, int] | None:
    """One GETX result as (ts_ns, record body, original length), or None.

    ``header`` is the 64-byte usbmon header as the kernel wrote it and
    ``data`` the bytes GETX copied (at most ``len_cap``). As in libpcap the
    header is kept unchanged and only the record is truncated, so a reader
    sees ``len_cap`` larger than the bytes present. ``snaplen`` > 0 limits
    the record body; the original length is 64 + ``len_cap``.
    """
    event = pcapfmt.unpack_header(header, mmapped=True, order=order)
    if devnum is not None and event.devnum != devnum:
        return None
    body = header[: pcapfmt.HEADER_64] + data[: event.len_cap]
    if snaplen > 0:
        body = body[: max(snaplen, pcapfmt.HEADER_64)]
    return event.ts_ns, body, pcapfmt.HEADER_64 + event.len_cap


class EventSource(Protocol):
    def fetch(self, timeout_s: float) -> tuple[bytes, bytes] | None: ...


def capture(
    source: EventSource,
    out: BinaryIO,
    *,
    devnum: int | None = None,
    snaplen: int = 0,
    duration_s: float = 0.0,
    max_bytes: int = 0,
    file_snaplen: int = 0x40000,
    should_stop: Callable[[], bool] = lambda: False,
    clock: Callable[[], float] = time.monotonic,
    poll_s: float = 0.2,
) -> CaptureStats:
    """Copy events from ``source`` to a pcap stream until a limit or stop."""
    order = pcapfmt.NATIVE_ORDER
    writer = pcapfmt.PcapWriter(out, snaplen=file_snaplen, order=order)
    stats = CaptureStats()
    deadline = clock() + duration_s if duration_s > 0 else None
    while True:
        if should_stop():
            stats.stopped_by = "signal"
            break
        if deadline is not None and clock() >= deadline:
            stats.stopped_by = "duration"
            break
        got = source.fetch(poll_s)
        if got is None:
            continue
        stats.events += 1
        record = event_to_record(
            got[0], got[1], devnum=devnum, snaplen=snaplen, order=order
        )
        if record is None:
            stats.filtered += 1
            continue
        ts_ns, body, original = record
        if len(body) < original:
            stats.truncated += 1
        if max_bytes > 0 and writer.bytes_written + 16 + len(body) > max_bytes:
            stats.stopped_by = "max-bytes"
            break
        writer.write(ts_ns, body, original)
        stats.records += 1
    stats.bytes_written = writer.bytes_written
    return stats


class RingSizeError(OSError):
    """usbmon rejected every ring size tried (EINVAL)."""


Ioctl = Callable[..., Any]


def ring_candidates(requested: int) -> list[int]:
    """The requested ring size, then the old-kernel ceiling if it is smaller."""
    return [requested] + ([RING_FALLBACK] if requested > RING_FALLBACK else [])


def set_ring_size(ioctl: Ioctl, fd: int, requested: int) -> int:
    """Set the ring, retrying at RING_FALLBACK on EINVAL; returns the size used.

    Raises RingSizeError when every candidate is rejected with EINVAL; any
    other errno propagates unchanged.
    """
    rejected: list[int] = []
    for size in ring_candidates(requested):
        try:
            ioctl(fd, MON_IOCT_RING_SIZE, size)
        except OSError as exc:
            if exc.errno != errno.EINVAL:
                raise
            rejected.append(size)
            continue
        return int(ioctl(fd, MON_IOCQ_RING_SIZE, 0))
    sizes = ", ".join(f"{n} ({n // 1024} KiB)" for n in rejected)
    raise RingSizeError(
        errno.EINVAL,
        f"usbmon rejected ring size {sizes} with EINVAL; "
        f"try --ring-size {RING_FALLBACK // 1024}K or smaller",
    )


class UsbmonDevice:
    """The thin ioctl layer over /dev/usbmonN (root only).

    Tests pass a fake ``ioctl`` and point USBMON_PATH at a regular file.
    """

    def __init__(
        self, bus: int, ring_size: int = DEFAULT_RING, ioctl: Ioctl | None = None
    ) -> None:
        if ioctl is None:
            import fcntl  # Linux only; imported here so tests run anywhere

            ioctl = fcntl.ioctl
        self._ioctl = ioctl
        self.requested_ring = ring_size
        self.fd = os.open(USBMON_PATH.format(bus), os.O_RDONLY | os.O_NONBLOCK)
        try:
            self.ring_size = set_ring_size(self._ioctl, self.fd, ring_size)
        except OSError:
            os.close(self.fd)
            raise
        self.alloc = event_alloc(self.ring_size)
        self._header = ctypes.create_string_buffer(pcapfmt.HEADER_64)
        self._data = ctypes.create_string_buffer(self.alloc)
        self._arg = MonGetArg(
            ctypes.addressof(self._header), ctypes.addressof(self._data), self.alloc
        )
        self._poll = select.poll()
        self._poll.register(self.fd, select.POLLIN)

    def fetch(self, timeout_s: float) -> tuple[bytes, bytes] | None:
        try:
            if not self._poll.poll(int(timeout_s * 1000)):
                return None
            self._ioctl(self.fd, MON_IOCX_GETX, self._arg)
        except InterruptedError:
            return None
        except BlockingIOError:
            return None
        header = self._header.raw
        len_cap = int.from_bytes(header[36:40], sys.byteorder)
        return header, ctypes.string_at(self._data, min(len_cap, self.alloc))

    def dropped(self) -> int:
        stats = bytearray(MON_STATS_SIZE)
        self._ioctl(self.fd, MON_IOCG_STATS, stats)
        return int.from_bytes(stats[4:8], sys.byteorder)

    def close(self) -> None:
        os.close(self.fd)


def parse_size(text: str) -> int:
    """Byte count with an optional K/M/G (binary) suffix."""
    units = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30}
    text = text.strip().upper().removesuffix("IB").removesuffix("B")
    if text and text[-1] in units:
        return int(float(text[:-1]) * units[text[-1]])
    return int(text)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture usbmon events into pcap (link type 220)."
    )
    parser.add_argument("--bus", type=int, default=0, help="usbmon bus; 0 = all")
    parser.add_argument("--devnum", type=int, help="keep only this device address")
    parser.add_argument("--duration", type=float, default=0.0, help="seconds; 0 = none")
    parser.add_argument(
        "--max-bytes",
        type=parse_size,
        default=0,
        help="stop before the file exceeds this",
    )
    parser.add_argument(
        "--ring-size",
        type=parse_size,
        default=DEFAULT_RING,
        help="usbmon ring (kernel memory)",
    )
    parser.add_argument(
        "--snaplen", type=parse_size, default=0, help="bytes kept per event; 0 = all"
    )
    parser.add_argument("--out", required=True, help="output .pcap file")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not RING_MIN <= args.ring_size <= RING_MAX:
        print(f"--ring-size must be within {RING_MIN}..{RING_MAX}", file=sys.stderr)
        return 2
    stop: list[int] = []

    def on_signal(signum: int, _frame: FrameType | None) -> None:
        stop.append(signum)  # the loop notices within one poll interval

    try:
        device = UsbmonDevice(args.bus, args.ring_size)
    except RingSizeError as exc:
        print(f"cannot size the /dev/usbmon{args.bus} ring: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        hint = ""
        if exc.errno == errno.ENOENT:
            hint = " (run `modprobe usbmon`)"
        elif exc.errno in (errno.EACCES, errno.EPERM):
            hint = " (run as root)"
        print(f"cannot open /dev/usbmon{args.bus}: {exc}{hint}", file=sys.stderr)
        return 1
    previous = {s: signal.signal(s, on_signal) for s in (signal.SIGINT, signal.SIGTERM)}
    if device.ring_size != args.ring_size:
        print(
            f"usbmon ring: requested {args.ring_size}, kernel uses "
            f"{device.ring_size} (larger rings rejected or rounded)",
            file=sys.stderr,
        )
    print(
        f"capturing /dev/usbmon{args.bus} ring={device.ring_size} "
        f"max-event-data={device.ring_size // 5}; Ctrl-C stops",
        file=sys.stderr,
    )
    try:
        with open(args.out, "wb") as out:
            stats = capture(
                device,
                out,
                devnum=args.devnum,
                snaplen=args.snaplen,
                duration_s=args.duration,
                max_bytes=args.max_bytes,
                file_snaplen=pcapfmt.HEADER_64 + device.alloc,
                should_stop=lambda: bool(stop),
            )
        stats.kernel_dropped = device.dropped()
    except OSError as exc:
        print(
            f"capture failed: {exc}; {args.out} holds the events so far",
            file=sys.stderr,
        )
        return 1
    finally:
        device.close()
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    print(
        f"{args.out}: {stats.records} records, {stats.bytes_written} bytes; "
        f"{stats.events} events read, {stats.filtered} filtered, "
        f"{stats.truncated} truncated, {stats.kernel_dropped} dropped by the kernel; "
        f"stopped by {stats.stopped_by}",
        file=sys.stderr,
    )
    return 0 if stats.kernel_dropped == 0 else 3


if __name__ == "__main__":
    sys.exit(main())
