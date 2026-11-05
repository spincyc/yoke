#!/usr/bin/env python3
"""Deterministic synthetic usbmon traces (pcap, link type 220).

Each class is a plausible device conversation as Linux usbmon would record
it, so replay.py runs before any hardware capture exists:

- ``disk-zero``: BOT mass storage, an image read of a filesystem whose
  blocks are about 70% zero, 12% metadata and 18% file data;
- ``files-text``: BOT writes then reads of log, source and prose files,
  with an explicitly modelled hot set read three times;
- ``media-jpeg``: BOT reads of JPEG and MP4 files with random scan data;
- ``hid``: a keyboard (8-byte) and a mouse (4-byte) interrupt IN stream
  after enumeration, ended by unlinks;
- ``audio-iso``: a USB Audio Class 1 headset, isochronous OUT and IN, which
  Yoke does not support (ADR 0021); replay reports it as not eligible.

Timestamps come from a synthetic clock: bulk data moves at a 40 MB/s
high-speed ceiling plus a fixed per-command device latency. Output is a
pure function of ``--seed`` and ``--scale``. Stdlib only, Python >= 3.9.
"""

from __future__ import annotations

import argparse
import heapq
import math
import random
import struct
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import pcapfmt
import urbs

MIB = 1 << 20
SECTOR = 512
BLOCK = 4096
URB_MAX = 65536
BUS_BYTES_PER_S = 40e6
EPOCH_NS = 1_762_300_800_000_000_000  # 2025-11-05T00:00:00Z, fixed
EINPROGRESS = -115
EXDEV = -18
URB_DIR_IN = 0x0200
HOST_GAP_NS = 5_000
COMMAND_GAP_NS = 20_000
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DIR = ROOT / ".scratch" / "usbtrace"


def bus_ns(length: int) -> int:
    return int(length / BUS_BYTES_PER_S * 1e9)


@dataclass(frozen=True)
class Urb:
    urb_id: int
    xfer: int  # USB transfer type
    endpoint: int
    devnum: int
    requested: int
    interval: int


class Recorder:
    """usbmon S/C events of one bus on a synthetic clock (``now``, in ns)."""

    def __init__(self, busnum: int = 1, start_ns: int = EPOCH_NS) -> None:
        self.busnum = busnum
        self.now = start_ns
        self._events: list[tuple[int, int, pcapfmt.UsbmonEvent]] = []
        self._free: list[tuple[int, int]] = []  # heap of (completed at, id)
        self._allocated = 0

    def _urb_id(self, ts: int) -> int:
        """Kernel-pointer-like ids, reused once completed (as slabs are)."""
        if self._free and self._free[0][0] < ts:
            return heapq.heappop(self._free)[1]
        self._allocated += 1
        return 0xFFFF_8880_0420_0000 + self._allocated * 0x200

    def _add(
        self,
        ts: int,
        urb: Urb,
        kind: str,
        *,
        status: int,
        length: int,
        data: bytes,
        flag_data: int,
        setup: bytes | None = None,
        isodescs: Sequence[pcapfmt.IsoDesc] = (),
    ) -> None:
        seconds, rest = divmod(ts, 1_000_000_000)
        setup_field = pcapfmt.NO_SETUP if setup is None else setup
        if isodescs:  # the union holds iso error_count and numdesc instead
            setup_field = struct.pack("<ii", 0, len(isodescs))
        event = pcapfmt.UsbmonEvent(
            urb_id=urb.urb_id,
            kind=kind,
            xfer_type=pcapfmt.USB_TO_USBMON[urb.xfer],
            epnum=urb.endpoint,
            devnum=urb.devnum,
            busnum=self.busnum,
            flag_setup=ord("-") if setup is None else pcapfmt.FLAG_PRESENT,
            flag_data=flag_data,
            ts_sec=seconds,
            ts_usec=rest // 1000,
            status=status,
            length=length,
            len_cap=0,  # pack_record computes it
            setup=setup_field,
            interval=urb.interval,
            xfer_flags=URB_DIR_IN if urb.endpoint & 0x80 else 0,
            isodescs=tuple(isodescs),
            data=data,
        )
        self._events.append((ts, len(self._events), event))

    def submit(
        self,
        ts: int,
        *,
        devnum: int,
        xfer: int,
        endpoint: int,
        length: int,
        data: bytes = b"",
        setup: bytes | None = None,
        interval: int = 0,
        isodescs: Sequence[pcapfmt.IsoDesc] = (),
    ) -> Urb:
        urb = Urb(self._urb_id(ts), xfer, endpoint, devnum, length, interval)
        is_in = bool(endpoint & 0x80)
        self._add(
            ts,
            urb,
            "S",
            status=EINPROGRESS,
            length=length,
            data=b"" if is_in else data,
            flag_data=ord("<") if is_in else pcapfmt.FLAG_PRESENT,
            setup=setup,
            isodescs=isodescs,
        )
        return urb

    def complete(
        self,
        urb: Urb,
        ts: int,
        *,
        status: int = 0,
        data: bytes = b"",
        actual: int | None = None,
        isodescs: Sequence[pcapfmt.IsoDesc] = (),
    ) -> None:
        is_in = bool(urb.endpoint & 0x80)
        length = (
            actual if actual is not None else (len(data) if is_in else urb.requested)
        )
        self._add(
            ts,
            urb,
            "C",
            status=status,
            length=length,
            data=data if is_in else b"",
            flag_data=pcapfmt.FLAG_PRESENT if is_in else ord(">"),
            isodescs=isodescs,
        )
        heapq.heappush(self._free, (ts, urb.urb_id))

    def transfer(
        self,
        *,
        devnum: int,
        xfer: int,
        endpoint: int,
        data: bytes = b"",
        requested: int | None = None,
        setup: bytes | None = None,
        device_ns: int = 10_000,
        status: int = 0,
    ) -> None:
        """One sequential URB: submit now, complete after ``device_ns``."""
        is_in = bool(endpoint & 0x80)
        length = requested if requested is not None else len(data)
        urb = self.submit(
            self.now,
            devnum=devnum,
            xfer=xfer,
            endpoint=endpoint,
            length=length,
            data=b"" if is_in else data,
            setup=setup,
        )
        self.now += device_ns
        self.complete(urb, self.now, status=status, data=data if is_in else b"")
        self.now += HOST_GAP_NS

    def write(self, path: Path) -> int:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as stream:
            writer = pcapfmt.PcapWriter(stream, order="<")
            for _ts, _seq, event in sorted(self._events, key=lambda e: (e[0], e[1])):
                writer.write_event(event)
        return path.stat().st_size


# --- control requests -------------------------------------------------------


def setup_packet(
    request_type: int, request: int, value: int, index: int, length: int
) -> bytes:
    return struct.pack("<BBHHH", request_type, request, value, index, length)


def control(
    rec: Recorder,
    devnum: int,
    setup: bytes,
    *,
    data_in: bytes = b"",
    data_out: bytes = b"",
) -> None:
    length = struct.unpack("<H", setup[6:8])[0]
    if setup[0] & 0x80:
        rec.transfer(
            devnum=devnum,
            xfer=pcapfmt.USB_CONTROL,
            endpoint=0x80,
            data=data_in[:length],
            requested=length,
            setup=setup,
            device_ns=60_000 + bus_ns(length),
        )
    else:
        rec.transfer(
            devnum=devnum,
            xfer=pcapfmt.USB_CONTROL,
            endpoint=0x00,
            data=data_out,
            setup=setup,
            device_ns=60_000 + bus_ns(len(data_out)),
        )


def enumerate_device(rec: Recorder, devnum: int, device: bytes, config: bytes) -> None:
    """The descriptor reads and SET_CONFIGURATION Linux performs."""
    control(rec, devnum, setup_packet(0x80, 6, 0x0100, 0, 8), data_in=device)
    control(rec, devnum, setup_packet(0x80, 6, 0x0100, 0, 18), data_in=device)
    control(rec, devnum, setup_packet(0x80, 6, 0x0200, 0, 9), data_in=config)
    control(rec, devnum, setup_packet(0x80, 6, 0x0200, 0, len(config)), data_in=config)
    for index in (0, 2):
        name = "Yoke synthetic".encode("utf-16-le")
        string = bytes([2 + len(name), 3]) + name if index else b"\x04\x03\x09\x04"
        control(
            rec,
            devnum,
            setup_packet(0x80, 6, 0x0300 | index, 0x0409, 255),
            data_in=string,
        )
    control(rec, devnum, setup_packet(0x00, 9, 1, 0, 0))


def device_descriptor(vendor: int, product: int, device_class: int = 0) -> bytes:
    return struct.pack(
        "<BBHBBBBHHHBBBB",
        18,
        1,
        0x0200,
        device_class,
        0,
        0,
        64,
        vendor,
        product,
        0x0100,
        1,
        2,
        3,
        1,
    )


def config_descriptor(interfaces: bytes) -> bytes:
    total = 9 + len(interfaces)
    return struct.pack("<BBHBBBBB", 9, 2, total, 1, 1, 0, 0x80, 50) + interfaces


# --- mass storage -------------------------------------------------------------


class BotDisk:
    """A BOT mass-storage device over ``image``; data URBs are <= 64 KiB."""

    EP_OUT, EP_IN = 0x02, 0x81

    def __init__(self, rec: Recorder, devnum: int, image: bytearray) -> None:
        self.rec = rec
        self.devnum = devnum
        self.image = image
        self.tag = 0
        self.read_latency_ns = 100_000
        self.write_latency_ns = 250_000

    def _bulk(
        self, endpoint: int, data: bytes, device_ns: int, requested: int = 0
    ) -> None:
        self.rec.transfer(
            devnum=self.devnum,
            xfer=pcapfmt.USB_BULK,
            endpoint=endpoint,
            data=data,
            requested=requested or len(data),
            device_ns=device_ns,
        )

    def command(
        self,
        cdb: bytes,
        *,
        data_in: bytes | None = None,
        data_out: bytes | None = None,
        status: int = 0,
    ) -> None:
        self.tag += 1
        payload = data_in if data_in is not None else data_out or b""
        cbw = urbs.pack_cbw(self.tag, len(payload), data_in is not None, 0, cdb)
        self._bulk(self.EP_OUT, cbw, 15_000)
        for offset in range(0, len(payload), URB_MAX):
            chunk = payload[offset : offset + URB_MAX]
            if data_in is not None:
                first = self.read_latency_ns if offset == 0 else 0
                self._bulk(self.EP_IN, chunk, first + bus_ns(len(chunk)))
            else:
                self._bulk(self.EP_OUT, chunk, bus_ns(len(chunk)))
        settle = self.write_latency_ns if data_out else 0
        csw = urbs.pack_csw(self.tag, 0, status)
        self._bulk(self.EP_IN, csw, 15_000 + settle, requested=urbs.CSW_LENGTH)
        self.rec.now += COMMAND_GAP_NS

    def read10(self, lba: int, blocks: int) -> None:
        cdb = struct.pack(">BBIBHB", urbs.READ_10, 0, lba, 0, blocks, 0)
        start = lba * SECTOR
        self.command(cdb, data_in=bytes(self.image[start : start + blocks * SECTOR]))

    def write10(self, lba: int, data: bytes) -> None:
        blocks = len(data) // SECTOR
        cdb = struct.pack(">BBIBHB", urbs.WRITE_10, 0, lba, 0, blocks, 0)
        self.image[lba * SECTOR : lba * SECTOR + len(data)] = data
        self.command(cdb, data_out=data)

    def read_range(self, lba: int, sectors: int, per_command: int = 128) -> None:
        """Sequential READ(10)s of ``per_command`` sectors (last one shorter)."""
        end = lba + sectors
        while lba < end:
            count = min(per_command, end - lba)
            self.read10(lba, count)
            lba += count

    def write_range(self, lba: int, data: bytes, per_command: int = 256) -> None:
        step = per_command * SECTOR
        for offset in range(0, len(data), step):
            self.write10(lba + offset // SECTOR, data[offset : offset + step])

    def attach(self) -> None:
        """Enumeration and the SCSI probe usb-storage and sd perform."""
        interface = struct.pack("<BBBBBBBBB", 9, 4, 0, 0, 2, 8, 6, 0x50, 0)
        endpoints = struct.pack("<BBBBHB", 7, 5, self.EP_IN, 2, 512, 0) + struct.pack(
            "<BBBBHB", 7, 5, self.EP_OUT, 2, 512, 0
        )
        enumerate_device(
            self.rec,
            self.devnum,
            device_descriptor(0x1D6B, 0x0104),
            config_descriptor(interface + endpoints),
        )
        control(
            self.rec, self.devnum, setup_packet(0xA1, 0xFE, 0, 0, 1), data_in=b"\x00"
        )
        inquiry = b"\x00\x80\x06\x02\x1f\x00\x00\x00" + b"Yoke    Synthetic disk  1.00"
        self.command(bytes([0x12, 0, 0, 0, 36, 0]), data_in=inquiry.ljust(36, b" "))
        self.command(bytes(6), status=1)  # TEST UNIT READY: UNIT ATTENTION
        sense = bytes([0x70, 0, 0x06, 0, 0, 0, 0, 10, 0, 0, 0, 0, 0x28, 0, 0, 0, 0, 0])
        self.command(bytes([0x03, 0, 0, 0, 18, 0]), data_in=sense)
        self.command(bytes(6))
        last = len(self.image) // SECTOR - 1
        self.command(bytes([0x25]) + bytes(9), data_in=struct.pack(">II", last, SECTOR))
        self.command(bytes([0x1A, 0, 0x3F, 0, 192, 0]), data_in=b"\x03\x00\x00\x00")


# --- payload generators -----------------------------------------------------

_SYLLABLES = (
    "ka", "to", "ri", "men", "sa", "lo", "ne", "dar", "vi", "po",
    "stra", "quel", "um", "ex", "ion", "er", "al", "in", "or", "ta",
    "ble", "con", "de", "sys", "tem", "ing", "ed", "pre", "port", "ly",
)  # fmt: skip


class TextSource:
    """Prose, log and C-like source text from a seeded Zipf vocabulary."""

    def __init__(self, rng: random.Random, words: int = 1500) -> None:
        self.rng = rng
        vocab = {
            "".join(rng.choices(_SYLLABLES, k=rng.randint(1, 4))) for _ in range(words)
        }
        self.words = sorted(vocab)
        self.cum = list(
            _cumulative(1.0 / (rank + 1) for rank in range(len(self.words)))
        )

    def _pick(self, count: int) -> list[str]:
        return self.rng.choices(self.words, cum_weights=self.cum, k=count)

    def prose(self, size: int) -> bytes:
        words = self._pick(size // 5 + 16)
        for index in range(11, len(words), 12):
            words[index] += ".\n" if index % 96 == 95 else "."
        return " ".join(words).encode()[:size]

    def log(self, size: int) -> bytes:
        rng, total = self.rng, 0
        lines: list[str] = []
        services = self.words[:12]
        levels = ("INFO", "INFO", "INFO", "DEBUG", "WARN", "ERROR")
        stamp = 1_762_300_800_000
        while total < size:
            stamp += rng.randint(0, 900)
            words = " ".join(self._pick(rng.randint(3, 8)))
            line = (
                f"2025-11-05T{stamp // 3_600_000 % 24:02}:{stamp // 60_000 % 60:02}:"
                f"{stamp // 1000 % 60:02}.{stamp % 1000:03}Z yoke-box "
                f"{rng.choice(services)}[{rng.randint(300, 4000)}]: "
                f"{rng.choice(levels)} "
                f"{words} id={rng.getrandbits(32):08x} took={rng.randint(0, 250)}ms\n"
            )
            lines.append(line)
            total += len(line)
        return "".join(lines).encode()[:size]

    def source(self, size: int) -> bytes:
        rng, total = self.rng, 0
        parts: list[str] = []
        while total < size:
            fn, var, typ, field, verb = self._pick(5)
            part = (
                f"static int {fn}_{verb}(struct {typ} *{var}, size_t count)\n{{\n"
                f"    if ({var} == NULL)\n        return -EINVAL;\n"
                f"    for (size_t i = 0; i < count; i++) {{\n"
                f"        {var}->{field}[i] = {fn}_lookup({var}, i)"
                f" + {rng.randint(0, 64)};\n"
                f"    }}\n    return 0;\n}}\n\n"
            )
            parts.append(part)
            total += len(part)
        return "".join(parts).encode()[:size]

    def mixed(self, size: int) -> bytes:
        third = size // 3
        return self.log(third) + self.source(third) + self.prose(size - 2 * third)


def _cumulative(values: Iterable[float]) -> list[float]:
    total, out = 0.0, []
    for value in values:
        total += value
        out.append(total)
    return out


def binary_like(rng: random.Random, size: int) -> bytes:
    """Executable-like bytes: random runs between zero and opcode-pattern runs."""
    out = bytearray()
    patterns = [rng.randbytes(8) for _ in range(16)]
    while len(out) < size:
        roll = rng.random()
        run = rng.randint(32, 512)
        if roll < 0.5:
            out += rng.randbytes(run)
        elif roll < 0.75:
            out += bytes(run)
        else:
            out += rng.choice(patterns) * (run // 8)
    return bytes(out[:size])


def jpeg_file(rng: random.Random, size: int) -> bytes:
    """A JFIF container whose entropy-coded scan is random (byte-stuffed)."""
    header = (
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00H\x00H\x00\x00"
        + b"\xff\xdb\x00\x43\x00"
        + bytes(range(1, 65))
        + b"\xff\xc0\x00\x11\x08\x0b\xb8\x0f\xa0\x03"
        + b"\x01\x22\x00\x02\x11\x01\x03\x11\x01"
        + b"\xff\xc4\x00\x1f\x00"
        + bytes(range(16))
        + bytes(range(12))
        + b"\xff\xda\x00\x0c\x03\x01\x00\x02\x11\x03\x11\x00\x3f\x00"
    )
    scan = rng.randbytes(max(0, size - len(header) - 2)).replace(b"\xff", b"\xff\x00")
    return (header + scan)[: size - 2] + b"\xff\xd9"


def mp4_file(rng: random.Random, size: int) -> bytes:
    ftyp = struct.pack(">I4s4sI8s", 24, b"ftyp", b"isom", 0x200, b"isomavc1")
    moov_body = b"".join(struct.pack(">I", rng.randint(0, 1 << 20)) for _ in range(256))
    moov = struct.pack(">I4s", 8 + len(moov_body), b"moov") + moov_body
    mdat_len = max(8, size - len(ftyp) - len(moov))
    return (
        ftyp
        + moov
        + struct.pack(">I4s", mdat_len, b"mdat")
        + rng.randbytes(mdat_len - 8)
    )


def inode_block(rng: random.Random, first_block: int, used: int) -> bytes:
    """Sixteen 256-byte ext4-like inodes, ``used`` of them populated."""
    out = bytearray(BLOCK)
    for slot in range(used):
        size = rng.randint(1, 1 << 20)
        stamp = 1_760_000_000 + rng.randint(0, 3_000_000)
        inode = (
            struct.pack(
                "<HHIIIIIHHII",
                0x81A4,
                1000,
                size,
                stamp,
                stamp,
                stamp,
                0,
                1000,
                1,
                (size + 511) // 512,
                0x80000,
            )
            + struct.pack("<HHHHI", 0xF30A, 1, 4, 0, 0)
            + struct.pack(
                "<IHHI", 0, (size + BLOCK - 1) // BLOCK, 0, first_block + slot * 64
            )
        )
        out[slot * 256 : slot * 256 + len(inode)] = inode
    return bytes(out)


def zero_heavy_image(rng: random.Random, size: int, text: TextSource) -> bytearray:
    """Groups of 512 blocks: ~12% metadata, ~18% file data, the rest zero."""
    image = bytearray(size)
    blocks = size // BLOCK
    group = min(512, blocks)
    makers: tuple[Callable[[int], bytes], ...] = (
        text.mixed,
        lambda n: binary_like(rng, n),
        rng.randbytes,
    )
    for base in range(0, blocks, group):
        count = min(group, blocks - base)
        meta, data = round(count * 0.12), round(count * 0.18)
        used_blocks = meta + data
        bitmap = b"\xff" * (used_blocks // 8)
        header = (
            struct.pack("<IIII", 0xEF53, base, count, used_blocks) + b"yoke-synthetic"
        )
        _put(image, base, header)
        if meta > 1:
            _put(image, base + 1, bitmap)
        for index in range(2, meta):
            used = 16 if base == 0 else rng.randint(0, 12)
            _put(image, base + index, inode_block(rng, base + meta, used))
        position = base + meta
        while position < base + meta + data:
            run = min(rng.randint(4, 24), base + meta + data - position)
            _put(image, position, rng.choice(makers)(run * BLOCK))
            position += run
    return image


def _put(image: bytearray, block: int, data: bytes) -> None:
    image[block * BLOCK : block * BLOCK + len(data)] = data


# --- classes ------------------------------------------------------------------


def _volume(scale: float) -> int:
    """Disk-class payload: 16 MiB at scale 1, in whole 64 KiB units."""
    return max(URB_MAX, int(16 * MIB * scale) // URB_MAX * URB_MAX)


def disk_zero(rng: random.Random, scale: float) -> Recorder:
    rec = Recorder()
    size = _volume(scale)
    disk = BotDisk(rec, 4, zero_heavy_image(rng, size, TextSource(rng)))
    disk.attach()
    for block in (0, 1, 2, 3, 2, 64 if size > 64 * BLOCK else 4):
        disk.read10(block * 8, 8)  # mount: superblock, bitmaps, inodes, root
    disk.read_range(0, size // SECTOR)  # the image read itself
    return rec


def files_text(rng: random.Random, scale: float) -> Recorder:
    rec = Recorder()
    volume = _volume(scale)
    disk = BotDisk(rec, 5, bytearray(volume + 4 * MIB))
    disk.attach()
    text = TextSource(rng)
    kinds = (text.log, text.source, text.prose)
    files: list[tuple[int, int]] = []
    lba, written = 2048 * 2, 0
    while written < volume // 2:
        length = min(rng.randint(64, 1024) * 1024, volume // 2 - written)
        length = max(SECTOR, length // SECTOR * SECTOR)
        disk.write_range(lba, rng.choice(kinds)(length))
        disk.write10(64 + len(files) % 32 * 8, inode_block(rng, lba // 8, 16))
        files.append((lba, length // SECTOR))
        lba += -(-length // BLOCK) * (BLOCK // SECTOR)
        written += length
    for start, sectors in files:
        disk.read_range(start, sectors)
    hot = files[: min(4, len(files))]
    for _round in range(3):  # the modelled re-reads (CACHE evidence)
        for start, sectors in hot:
            disk.read_range(start, min(sectors, 192))
    return rec


def media_jpeg(rng: random.Random, scale: float) -> Recorder:
    rec = Recorder()
    volume = _volume(scale)
    image = bytearray(volume + 4 * MIB)
    disk = BotDisk(rec, 6, image)
    files: list[tuple[int, int]] = []
    lba, placed = 4096, 0
    while volume - placed >= URB_MAX:
        if rng.random() < 0.15 and volume - placed > 2 * MIB:
            data = mp4_file(rng, min(volume // 3, volume - placed))
        else:
            data = jpeg_file(rng, min(rng.randint(512, 3072) * 1024, volume - placed))
        image[lba * SECTOR : lba * SECTOR + len(data)] = data
        files.append((lba, -(-len(data) // SECTOR)))
        lba += -(-len(data) // BLOCK) * (BLOCK // SECTOR)
        placed += len(data)
    fat = b"".join(struct.pack("<I", n + 3) for n in range(BLOCK // 4))
    image[2048 * SECTOR : 2048 * SECTOR + BLOCK] = fat
    disk.attach()
    for start, sectors in files:
        disk.read10(2048, 8)  # the directory/FAT block a file browser reads
        disk.read_range(start, sectors)
    return rec


def hid(rng: random.Random, scale: float) -> Recorder:
    rec = Recorder()
    keyboard, mouse = 7, 8
    hid_desc = struct.pack("<BBHBBBH", 9, 0x21, 0x0111, 0, 1, 0x22, 63)
    for devnum, protocol, size in ((keyboard, 1, 8), (mouse, 2, 4)):
        interface = struct.pack("<BBBBBBBBB", 9, 4, 0, 0, 1, 3, 1, protocol, 0)
        endpoint = struct.pack("<BBBBHB", 7, 5, 0x81, 3, size, 10 if size == 8 else 8)
        enumerate_device(
            rec,
            devnum,
            device_descriptor(0x1D6B, 0x0200 + protocol),
            config_descriptor(interface + hid_desc + endpoint),
        )
        control(rec, devnum, setup_packet(0x21, 0x0A, 0, 0, 0))  # SET_IDLE
        report = bytes([0x05, 0x01, 0x09, 0x06 if size == 8 else 0x02]) + bytes(59)
        control(rec, devnum, setup_packet(0x81, 6, 0x2200, 0, 63), data_in=report)
    start = rec.now
    end = start + int(20e9 * scale)
    # Keyboard: words typed from prose; press, hold, release, pause.
    events: list[tuple[int, int, bytes]] = []
    letters = TextSource(rng).prose(4096).decode()
    t = start + 500_000_000
    for char in letters:
        if t >= end:
            break
        code = 44 if char == " " else 40 if char == "\n" else 4 + (ord(char) - 97) % 26
        events.append((t, keyboard, bytes([0, 0, code, 0, 0, 0, 0, 0])))
        t += rng.randint(70, 140) * 1_000_000
        events.append((t, keyboard, bytes(8)))
        t += rng.randint(60, 220) * 1_000_000 + (
            rng.randint(800, 2500) * 10**6 if char == "." else 0
        )
    # Mouse: bursts of 125 Hz motion reports.
    t = start + 200_000_000
    while t < end:
        for _ in range(rng.randint(30, 180)):
            dx, dy = rng.randint(-12, 12) & 0xFF, rng.randint(-8, 8) & 0xFF
            events.append((t, mouse, bytes([0, dx, dy, 0])))
            t += 8_000_000
        t += rng.randint(300, 3000) * 1_000_000
    events = [e for e in events if e[0] < end]
    events.sort()
    pending = {
        dev: rec.submit(
            start,
            devnum=dev,
            xfer=pcapfmt.USB_INTERRUPT,
            endpoint=0x81,
            length=8 if dev == keyboard else 4,
            interval=10 if dev == keyboard else 8,
        )
        for dev in (keyboard, mouse)
    }
    for ts, dev, report in events:
        urb = pending[dev]
        rec.complete(urb, ts, data=report)
        pending[dev] = rec.submit(
            ts + 15_000,
            devnum=dev,
            xfer=pcapfmt.USB_INTERRUPT,
            endpoint=0x81,
            length=urb.requested,
            interval=urb.interval,
        )
    for urb in pending.values():  # close(): usb_kill_urb
        rec.complete(urb, end, status=urbs.UNLINK_STATUSES[0])
    rec.now = end
    return rec


def audio_iso(rng: random.Random, scale: float) -> Recorder:
    """UAC1 headset at full speed: 8 x 1 ms packets per URB, two in flight."""
    rec = Recorder()
    devnum, packets = 9, 8
    interface = struct.pack("<BBBBBBBBB", 9, 4, 1, 1, 1, 1, 2, 0, 0)
    out_ep = struct.pack("<BBBBHB", 7, 5, 0x01, 0x09, 192, 1)
    in_ep = struct.pack("<BBBBHB", 7, 5, 0x82, 0x05, 96, 1)
    enumerate_device(
        rec,
        devnum,
        device_descriptor(0x1D6B, 0x0300),
        config_descriptor(interface + out_ep + in_ep),
    )
    for alt_interface in (1, 2):
        control(
            rec, devnum, setup_packet(0x01, 0x0B, 1, alt_interface, 0)
        )  # SET_INTERFACE
    rate = (48000).to_bytes(3, "little")
    control(rec, devnum, setup_packet(0x22, 0x01, 0x0100, 0x01, 3), data_out=rate)
    start = rec.now
    urbs_total = int(10_000 * scale) // packets
    # 0.1 s of a 440 Hz tone holds exactly 44 periods, so the table loops.
    tone = [int(8000 * math.sin(2 * math.pi * 440 * n / 48000)) for n in range(4800)]
    pcm = {
        0x01: b"".join(struct.pack("<hh", v, v) for v in tone),  # stereo out
        0x82: b"".join(
            struct.pack("<h", v + rng.randint(-64, 64)) for v in tone
        ),  # mic
    }
    for index in range(urbs_total):
        t = start + index * packets * 1_000_000
        for endpoint, size in ((0x01, 192), (0x82, 96)):
            table = pcm[endpoint]
            offset = index * size * packets % len(table)
            samples = (table + table)[offset : offset + size * packets]
            descs = [pcapfmt.IsoDesc(EXDEV, k * size, size) for k in range(packets)]
            urb = rec.submit(
                t,
                devnum=devnum,
                xfer=pcapfmt.USB_ISOCHRONOUS,
                endpoint=endpoint,
                length=size * packets,
                data=samples if endpoint == 0x01 else b"",
                interval=1,
                isodescs=descs,
            )
            done = [pcapfmt.IsoDesc(0, d.offset, d.length) for d in descs]
            rec.complete(
                urb,
                t + 2 * packets * 1_000_000,
                data=samples if endpoint == 0x82 else b"",
                actual=size * packets,
                isodescs=done,
            )
    rec.now = start + urbs_total * packets * 1_000_000
    return rec


CLASSES: dict[str, Callable[[random.Random, float], Recorder]] = {
    "disk-zero": disk_zero,
    "files-text": files_text,
    "media-jpeg": media_jpeg,
    "hid": hid,
    "audio-iso": audio_iso,
}


def generate(
    out_dir: Path, *, seed: int = 1, scale: float = 1.0, only: Sequence[str] = ()
) -> list[Path]:
    """Write one pcap per class into ``out_dir``; returns the paths."""
    paths = []
    for index, (name, make) in enumerate(CLASSES.items()):
        if only and name not in only:
            continue
        rec = make(random.Random(seed * 1000 + index), scale)
        path = out_dir / f"{name}.pcap"
        rec.write(path)
        paths.append(path)
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Write deterministic synthetic usbmon traces."
    )
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_DIR)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument(
        "--scale", type=float, default=1.0, help="1.0 = 16 MiB per disk class"
    )
    parser.add_argument("--only", action="append", choices=sorted(CLASSES), default=[])
    args = parser.parse_args(argv)
    for path in generate(
        args.out_dir, seed=args.seed, scale=args.scale, only=args.only
    ):
        print(f"{path} {path.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
