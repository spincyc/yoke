"""tools/usbtrace: usbmon pcap codec, capture loop, pairing, synthetic traces
and the replay model of ADR 0028's levers.

Everything here is offline: capture.py's ioctl layer runs against a fake
ioctl and a regular file (no /dev/usbmon, no root), and replay numbers are
analytic model outputs, not hardware.
Encoder-dependent checks skip when native/yusb/src/yx.c or the vendored lz4
are absent.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import io
import struct
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "usbtrace"))

import capture  # noqa: E402
import pcapfmt  # noqa: E402
import replay  # noqa: E402
import synth  # noqa: E402
import urbs  # noqa: E402
import yxenc  # noqa: E402

SMALL = 1 / 16  # 1 MiB per disk class


def event(
    kind: str,
    urb_id: int,
    *,
    epnum: int = 0x81,
    xfer_type: int = pcapfmt.URB_BULK,
    busnum: int = 1,
    devnum: int = 2,
    ts_usec: int = 0,
    status: int = 0,
    length: int = 0,
    data: bytes = b"",
    setup: bytes = pcapfmt.NO_SETUP,
    flag_setup: int = ord("-"),
) -> pcapfmt.UsbmonEvent:
    return pcapfmt.UsbmonEvent(
        urb_id=urb_id,
        kind=kind,
        xfer_type=xfer_type,
        epnum=epnum,
        devnum=devnum,
        busnum=busnum,
        flag_setup=flag_setup,
        flag_data=pcapfmt.FLAG_PRESENT if data else ord("<"),
        ts_sec=1_700_000_000,
        ts_usec=ts_usec,
        status=status,
        length=length,
        len_cap=len(data),
        setup=setup,
        data=data,
    )


# --- pcap and usbmon codec ----------------------------------------------------


def test_usbmon_header_offsets_match_struct_usbmon_packet() -> None:
    sample = pcapfmt.UsbmonEvent(
        urb_id=0x1122334455667788,
        kind="C",
        xfer_type=pcapfmt.URB_BULK,
        epnum=0x81,
        devnum=5,
        busnum=3,
        flag_setup=ord("-"),
        flag_data=0,
        ts_sec=1_700_000_001,
        ts_usec=123_456,
        status=-errno.EPIPE,
        length=512,
        len_cap=500,
        setup=b"ABCDEFGH",
        interval=8,
        start_frame=-1,
        xfer_flags=0x200,
        ndesc=0,
    )
    raw = pcapfmt.pack_header(sample)
    assert len(raw) == 64 == pcapfmt.HEADER_64
    assert raw[0:8] == (0x1122334455667788).to_bytes(8, "little")
    assert raw[8:12] == b"C\x03\x81\x05"
    assert struct.unpack_from("<H", raw, 12) == (3,)
    assert struct.unpack_from("<qiiII", raw, 16) == (
        1_700_000_001,
        123_456,
        -32,
        512,
        500,
    )
    assert raw[40:48] == b"ABCDEFGH"
    assert struct.unpack_from("<iiII", raw, 48) == (8, -1, 0x200, 0)
    assert pcapfmt.unpack_header(raw) == sample
    short = pcapfmt.pack_header(sample, mmapped=False, order=">")
    assert len(short) == 48 == pcapfmt.HEADER_48
    decoded = pcapfmt.unpack_header(short, mmapped=False, order=">")
    assert (
        decoded.len_cap == 500
        and decoded.interval == 0
        and decoded.setup == b"ABCDEFGH"
    )


@pytest.mark.parametrize("linktype", pcapfmt.LINKTYPES)
@pytest.mark.parametrize("order", ["<", ">"])
@pytest.mark.parametrize("nanosecond", [False, True])
def test_pcap_round_trip(linktype: int, order: str, nanosecond: bool) -> None:
    events = [
        event("S", 7, epnum=0x02, ts_usec=10, length=31, data=bytes(range(31))),
        event("C", 7, epnum=0x02, ts_usec=20, length=31),
        event("C", 8, ts_usec=999_999, length=4, data=b"\x01\x02\x03\x04", status=-71),
    ]
    stream = io.BytesIO()
    writer = pcapfmt.PcapWriter(
        stream, linktype=linktype, nanosecond=nanosecond, order=order
    )
    for item in events:
        writer.write_event(item)
    stream.seek(0)
    reader = pcapfmt.PcapReader(stream)
    assert reader.info == pcapfmt.PcapInfo(linktype, nanosecond, order, 0x40000)
    stream.seek(0)
    assert list(pcapfmt.read_events(stream)) == events
    stream.seek(24)
    assert [r.ts_ns % 1_000_000_000 for r in reader] == [10_000, 20_000, 999_999_000]


def test_isochronous_descriptors_precede_data() -> None:
    descs = tuple(pcapfmt.IsoDesc(-18, k * 96, 96) for k in range(3))
    iso = pcapfmt.UsbmonEvent(
        urb_id=1,
        kind="S",
        xfer_type=pcapfmt.URB_ISOCHRONOUS,
        epnum=0x01,
        devnum=9,
        busnum=1,
        flag_setup=ord("-"),
        flag_data=0,
        ts_sec=0,
        ts_usec=0,
        status=-115,
        length=288,
        len_cap=0,
        setup=struct.pack("<ii", 0, 3),
        isodescs=descs,
        data=b"\x55" * 288,
    )
    body = pcapfmt.pack_record(iso)
    assert len(body) == 64 + 3 * 16 + 288
    assert struct.unpack_from("<I", body, 36) == (3 * 16 + 288,)  # len_cap counts descs
    assert struct.unpack_from("<I", body, 60) == (3,)
    parsed = pcapfmt.parse_record(body, pcapfmt.LINKTYPE_USB_LINUX_MMAPPED)
    assert parsed.isodescs == descs and parsed.data == iso.data
    with pytest.raises(ValueError, match="189"):
        pcapfmt.pack_record(iso, mmapped=False)


def test_truncated_record_keeps_the_captured_prefix() -> None:
    full = pcapfmt.pack_record(event("C", 3, length=100, data=bytes(100)))
    parsed = pcapfmt.parse_record(full[:104], pcapfmt.LINKTYPE_USB_LINUX_MMAPPED)
    assert parsed.len_cap == 100 and parsed.length == 100 and len(parsed.data) == 40


@pytest.mark.parametrize(
    ("head", "message"),
    [
        (struct.pack("<I", 0x0A0D0D0A) + bytes(20), "pcapng"),
        (bytes(24), "magic"),
        (b"\xd4\xc3\xb2\xa1", "short"),
    ],
)
def test_reader_rejects_other_formats(head: bytes, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        pcapfmt.PcapReader(io.BytesIO(head))


def test_reader_rejects_other_linktypes() -> None:
    stream = io.BytesIO()
    pcapfmt.PcapWriter(stream, linktype=1)
    stream.seek(0)
    with pytest.raises(ValueError, match="link type 1"):
        list(pcapfmt.read_events(stream))


# --- capture (offline) ----------------------------------------------------------


def test_ioctl_numbers_follow_the_generic_ioc_encoding() -> None:
    assert capture.MON_IOCT_RING_SIZE == 0x9204
    assert capture.MON_IOCQ_RING_SIZE == 0x9205
    assert capture.MON_IOCG_STATS == 0x80089203
    getx = 0x4000920A | 3 * ctypes.sizeof(ctypes.c_void_p) << 16
    assert getx == capture.MON_IOCX_GETX
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        assert capture.MON_IOCX_GETX == 0x4018920A
    assert capture.event_alloc(32 << 20) == (32 << 20) // 5 + 128 * 16


def test_event_to_record_filters_and_truncates_like_libpcap() -> None:
    header = pcapfmt.pack_header(
        event("C", 9, devnum=4, length=1000, data=b""), order="<"
    )
    header = header[:36] + struct.pack("<I", 1000) + header[40:]  # len_cap 1000
    data = bytes(range(256)) * 4
    assert capture.event_to_record(header, data, devnum=5, order="<") is None
    record = capture.event_to_record(
        header, data[:1000], devnum=4, snaplen=200, order="<"
    )
    assert record is not None
    ts_ns, body, original = record
    assert ts_ns == 1_700_000_000 * 10**9
    assert len(body) == 200 and original == 64 + 1000
    assert body[:64] == header  # the header (len_cap) is left as the kernel wrote it


class FakeSource:
    def __init__(self, items: list[tuple[bytes, bytes] | None]) -> None:
        self.items = items
        self.fetches = 0

    def fetch(self, timeout_s: float) -> tuple[bytes, bytes] | None:
        self.fetches += 1
        return self.items.pop(0) if self.items else None


def _raw(item: pcapfmt.UsbmonEvent) -> tuple[bytes, bytes]:
    return pcapfmt.pack_header(item, order=pcapfmt.NATIVE_ORDER), item.data


def test_capture_loop_writes_a_readable_trace() -> None:
    keep = [event("S", 1, epnum=0x02, length=3, data=b"abc"), event("C", 1, epnum=0x02)]
    other = event("S", 2, devnum=9)
    source = FakeSource([_raw(keep[0]), None, _raw(other), _raw(keep[1])])
    out = io.BytesIO()
    stats = capture.capture(
        source, out, devnum=2, should_stop=lambda: source.fetches >= 6, poll_s=0
    )
    assert (stats.events, stats.records, stats.filtered, stats.stopped_by) == (
        3,
        2,
        1,
        "signal",
    )
    out.seek(0)
    assert list(pcapfmt.read_events(out)) == keep


def test_capture_loop_stops_on_duration_and_max_bytes() -> None:
    ticks = iter(range(100))
    items: list[tuple[bytes, bytes] | None] = [_raw(event("S", n)) for n in range(50)]
    stats = capture.capture(
        FakeSource(items), io.BytesIO(), duration_s=5, clock=lambda: float(next(ticks))
    )
    assert stats.stopped_by == "duration" and stats.records == 4
    items = [_raw(event("S", n)) for n in range(50)]
    out = io.BytesIO()
    stats = capture.capture(FakeSource(items), out, max_bytes=24 + 3 * 80)
    assert stats.stopped_by == "max-bytes" and stats.records == 3
    assert len(out.getvalue()) == stats.bytes_written == 24 + 3 * 80


def _fake_ioctl(
    limit: int, calls: list[tuple[int, int]], *, rejects: int = errno.EINVAL
) -> Callable[..., Any]:
    """A usbmon ioctl stub whose kernel accepts rings up to ``limit`` bytes."""
    state = {"ring": 300 * 1024}

    def ioctl(fd: int, request: int, arg: Any = 0, *_rest: Any) -> int:
        if request == capture.MON_IOCT_RING_SIZE:
            calls.append((request, arg))
            if not capture.RING_MIN <= arg <= limit:
                raise OSError(rejects, "Invalid argument")
            state["ring"] = arg
            return 0
        if request == capture.MON_IOCQ_RING_SIZE:
            return state["ring"]
        if request == capture.MON_IOCX_GETX:
            raise BlockingIOError(errno.EAGAIN, "no event")
        if request == capture.MON_IOCG_STATS:
            return 0
        raise AssertionError(f"unexpected ioctl {request:#x}")

    return ioctl


def test_ring_ceiling_is_the_mainline_64_mib() -> None:
    assert capture.RING_MAX == 64 << 20
    assert capture.RING_FALLBACK == 1200 * 1024
    assert capture.RING_MIN <= capture.DEFAULT_RING <= capture.RING_MAX
    assert capture.ring_candidates(32 << 20) == [32 << 20, 1200 * 1024]
    assert capture.ring_candidates(1 << 20) == [1 << 20]


def test_ring_size_retries_at_1200_kib_on_einval() -> None:
    calls: list[tuple[int, int]] = []
    ioctl = _fake_ioctl(capture.RING_FALLBACK, calls)  # an older or BSP kernel
    assert capture.set_ring_size(ioctl, 3, 32 << 20) == 1200 * 1024
    assert [arg for _req, arg in calls] == [32 << 20, 1200 * 1024]
    calls.clear()
    ioctl = _fake_ioctl(64 << 20, calls)  # current mainline: no retry
    assert capture.set_ring_size(ioctl, 3, 32 << 20) == 32 << 20
    assert len(calls) == 1


def test_ring_size_failure_is_a_clear_error() -> None:
    calls: list[tuple[int, int]] = []
    with pytest.raises(capture.RingSizeError, match="rejected ring size") as info:
        capture.set_ring_size(_fake_ioctl(64 * 1024, calls), 3, 32 << 20)
    assert info.value.errno == errno.EINVAL
    assert "33554432 (32768 KiB), 1228800 (1200 KiB)" in str(info.value)
    with pytest.raises(PermissionError):  # other errors are not retried
        capture.set_ring_size(_fake_ioctl(0, [], rejects=errno.EPERM), 3, 32 << 20)


def _run_capture_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit: int,
    *extra: str,
) -> tuple[int, str, list[tuple[int, int]]]:
    import fcntl

    calls: list[tuple[int, int]] = []
    (tmp_path / "usbmon3").write_bytes(b"")  # poll() reports a regular file ready
    monkeypatch.setattr(capture, "USBMON_PATH", str(tmp_path / "usbmon{}"))
    monkeypatch.setattr(fcntl, "ioctl", _fake_ioctl(limit, calls))
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    out = tmp_path / "trace.pcap"
    code = capture.main(["--bus", "3", "--duration", "0.02", "--out", str(out), *extra])
    return code, err.getvalue(), calls


def test_capture_main_falls_back_and_prints_the_ring_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, err, calls = _run_capture_main(tmp_path, monkeypatch, capture.RING_FALLBACK)
    assert code == 0, err
    assert [arg for _req, arg in calls] == [32 << 20, 1200 * 1024]
    assert "requested 33554432, kernel uses 1228800" in err
    assert "ring=1228800 max-event-data=245760" in err
    with (tmp_path / "trace.pcap").open("rb") as stream:
        assert pcapfmt.PcapReader(stream).info.snaplen == 64 + capture.event_alloc(
            1228800
        )


def test_capture_main_reports_rejected_rings_and_bad_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, err, _calls = _run_capture_main(tmp_path, monkeypatch, 64 * 1024)
    assert code == 1
    assert "cannot size the /dev/usbmon3 ring" in err and "EINVAL" in err
    code, err, calls = _run_capture_main(
        tmp_path, monkeypatch, 64 << 20, "--ring-size", "65M"
    )
    assert code == 2 and not calls and "--ring-size must be within" in err


def test_parse_size() -> None:
    assert capture.parse_size("32M") == 32 << 20
    assert capture.parse_size("2GiB") == 2 << 30
    assert capture.parse_size("1.5k") == 1536
    assert capture.parse_size("4096") == 4096


# --- pairing and BOT ------------------------------------------------------------


def test_pairing_by_bus_and_urb_id_with_errors_and_unlinks() -> None:
    events = [
        event("C", 0xDEAD, length=8, data=bytes(8)),  # submitted before the capture
        event("S", 1, epnum=0x02, ts_usec=1, length=4, data=b"OUT!"),
        event("S", 1, busnum=2, ts_usec=2, length=64),  # same id, other bus
        event("C", 1, epnum=0x02, ts_usec=3, length=4),
        event("C", 1, busnum=2, ts_usec=4, length=16, data=b"I" * 16),
        event("S", 1, ts_usec=5, length=64),  # id reused after completion
        event("E", 1, ts_usec=6, status=-errno.ENODEV),
        event("S", 2, ts_usec=7, length=64),
        event("C", 2, ts_usec=8, status=-errno.ENOENT),
        event("S", 3, ts_usec=9, length=512),
        event(
            "C", 3, ts_usec=10, status=-errno.ECONNRESET, length=100, data=b"p" * 100
        ),
        event("S", 4, ts_usec=11, length=64),
        event("S", 4, ts_usec=12, length=64),  # the first lost its completion
        event("C", 5, ts_usec=13, status=-errno.EPIPE),
        event("S", 6, ts_usec=14, length=8),  # still pending at the end
    ]
    result = urbs.pair_events(events)
    assert (result.orphan_completions, result.unmatched_submits) == (2, 3)
    out, bus2, failed, killed, reset = result.transfers
    assert (out.data, out.payload_length, out.outcome, out.is_in) == (
        b"OUT!",
        4,
        "ok",
        False,
    )
    assert (bus2.busnum, bus2.data, bus2.actual, bus2.requested) == (
        2,
        b"I" * 16,
        16,
        64,
    )
    assert (failed.outcome, failed.actual, failed.status) == ("submit-error", 0, -19)
    assert (killed.outcome, reset.outcome) == ("unlinked", "unlinked")
    assert (reset.payload_length, reset.data) == (100, b"p" * 100)
    assert urbs.outcome_of("C", -errno.EPIPE) == "error"
    assert urbs.outcome_of("C", -errno.ESHUTDOWN) == "gone"


def test_control_setup_is_kept_only_when_captured() -> None:
    setup = synth.setup_packet(0x80, 6, 0x0100, 0, 18)
    control = pcapfmt.URB_CONTROL
    events = [
        event(
            "S",
            1,
            epnum=0x80,
            xfer_type=control,
            length=18,
            setup=setup,
            flag_setup=pcapfmt.FLAG_PRESENT,
        ),
        event("C", 1, epnum=0x80, xfer_type=control, length=18, data=b"d" * 18),
        event("S", 2, epnum=0x80, xfer_type=control, length=18),
        event("C", 2, epnum=0x80, xfer_type=control, length=18),
    ]
    first, second = urbs.pair_events(events).transfers
    assert (first.setup, first.xfer, first.data) == (
        setup,
        pcapfmt.USB_CONTROL,
        b"d" * 18,
    )
    assert second.setup == b""


def test_cbw_and_csw_codec() -> None:
    cdb = struct.pack(">BBIBHB", urbs.READ_10, 0, 0x12345, 0, 128, 0)
    raw = urbs.pack_cbw(7, 65536, True, 1, cdb)
    parsed = urbs.parse_cbw(raw)
    assert parsed is not None
    assert parsed == urbs.Cbw(7, 65536, True, 1, cdb)
    assert parsed.rw10() == (0x12345, 128)
    assert urbs.parse_cbw(raw[:30]) is None
    assert urbs.parse_cbw(b"XXXX" + raw[4:]) is None
    assert urbs.parse_csw(urbs.pack_csw(7, 512, 1)) == (7, 512, 1)
    assert urbs.parse_csw(b"USBC" + bytes(9)) is None


def _bot_trace(tmp_path: Path, script: str) -> replay.Trace:
    """A one-device BOT trace: r = 64 KiB READ(10) continuing, w = 128 KiB WRITE."""
    rec = synth.Recorder()
    disk = synth.BotDisk(rec, 3, bytearray(8 << 20))
    lba = 0
    for step in script:
        if step == "r":
            disk.read10(lba, 128)
            lba += 128
        elif step == "s":
            disk.read10(lba, 8)  # a shorter read: no hit, its own run
            lba += 8
        else:
            disk.write10(4096, bytes(range(256)) * 512)
    path = tmp_path / "bot.pcap"
    rec.write(path)
    return replay.load_trace(path)


def test_bot_commands_group_cbw_data_and_csw(tmp_path: Path) -> None:
    trace = _bot_trace(tmp_path, "rw")
    read, write = trace.commands
    assert (read.data_in, read.data_out, read.cbw.rw10(), read.good) == (
        65536,
        0,
        (0, 128),
        True,
    )
    assert (write.data_out, write.data_out_lengths, write.cbw.opcode) == (
        131072,
        [65536, 65536],
        urbs.WRITE_10,
    )
    assert len(read.members) == 3 and len(write.members) == 4


def test_round_trip_accounting_per_lever(tmp_path: Path) -> None:
    trace = _bot_trace(tmp_path, "rrrsw")
    model = replay.Model(max_urb=65536)
    rtt = 0.001
    plain = replay.stalls(trace, model, replay.LEVERS[0], rtt)
    behind = replay.stalls(trace, model, replay.LEVERS[1], rtt)
    ahead = replay.stalls(trace, model, replay.LEVERS[2], rtt)
    assert plain.round_trips == 5 + 2  # five commands, two data-out URBs
    assert behind.round_trips == 5
    assert plain.seconds == pytest.approx(7 * rtt)
    assert behind.seconds == pytest.approx(5 * rtt)
    assert (ahead.hits, ahead.misses) == (2, 2)  # r r r hit twice; s and w miss
    read_s = replay.command_time_s(trace.commands[2], model)
    short_s = replay.command_time_s(trace.commands[3], model)
    expected = 7 * rtt - 2 * rtt + max(0.0, read_s - rtt) + max(0.0, short_s - rtt)
    assert ahead.seconds == pytest.approx(expected)
    assert ahead.wasted_device_s == pytest.approx(read_s + short_s)


def test_max_urb_defaults_to_the_native_worker_and_splits_bulk(
    tmp_path: Path,
) -> None:
    assert replay.Model().max_urb == replay.NATIVE_MAX_URB == 32768
    trace = _bot_trace(tmp_path, "w")  # one 128 KiB WRITE(10): two 64 KiB URBs
    data_out = next(t for t in trace.transfers if t.payload_length == 65536)
    assert [n for n, _d in replay.chunks(data_out, 32768)] == [32768, 32768]
    assert [n for n, _d in replay.chunks(data_out, 65536)] == [65536]
    small, large = replay.Model(), replay.Model(max_urb=65536)
    plain = replay.LEVERS[0]
    assert replay.stalls(trace, small, plain, 0.001).round_trips == 1 + 4
    assert replay.stalls(trace, large, plain, 0.001).round_trips == 1 + 2
    narrow = replay.wire_bytes(trace, small, plain, None)
    wide = replay.wire_bytes(trace, large, plain, None)
    assert narrow.to_device + narrow.to_host > wide.to_device + wide.to_host
    path = tmp_path / "bot.pcap"
    out = tmp_path / "r.json"
    assert replay.main([str(path), "--no-encoder", "--json-out", str(out)]) == 0
    assert '"max_urb": 32768' in out.read_text()
    assert replay.main([str(path), "--no-encoder", "--max-urb", "65536"]) == 0
    with pytest.raises(SystemExit):
        replay.main([str(path), "--max-urb", "100"])


def _truncated_trace(path: Path) -> Path:
    """Three 64 KiB bulk IN reads of zeros: whole, cut by usbmon, cut by snaplen.

    usbmon truncation leaves len_cap below length (a ring too small for the
    event); snaplen truncation leaves the pcap record shorter than its
    original length with len_cap intact.
    """
    stream = io.BytesIO()
    writer = pcapfmt.PcapWriter(stream)
    full = 65536
    writer.write_event(event("S", 1, ts_usec=1, length=full))
    writer.write_event(event("C", 1, ts_usec=2, length=full, data=bytes(full)))
    writer.write_event(event("S", 2, ts_usec=3, length=full))
    writer.write_event(event("C", 2, ts_usec=4, length=full, data=bytes(16384)))
    writer.write_event(event("S", 3, ts_usec=5, length=full))
    done = event("C", 3, ts_usec=6, length=full, data=bytes(full))
    body = pcapfmt.pack_record(done)
    writer.write(done.ts_ns, body[: 64 + 8192], len(body))
    path.write_bytes(stream.getvalue())
    return path


def test_replay_flags_truncated_captures(tmp_path: Path) -> None:
    path = _truncated_trace(tmp_path / "cut.pcap")
    result = replay.run([path], replay.Model(), use_encoder=False)
    (trace,) = result["traces"]
    captured = 65536 + 16384 + 8192
    assert trace["captured_bytes"] == captured
    assert trace["uncaptured_bytes"] == 3 * 65536 - captured
    assert trace["captured_fraction"] == pytest.approx(captured / (3 * 65536), abs=1e-4)
    assert trace["captured_fraction"] < 1
    assert trace["truncated"] is True and trace["truncated_transfers"] == 2
    assert "plain rows are exact" in trace["truncation_note"]
    text = replay.markdown(result)
    assert "0.46 TRUNCATED" in text and "Truncated captures:" in text
    plain = _rows(result, "cut")["plain"]
    assert plain["payload_bytes"] == 3 * 65536  # bytes on the wire, not captured
    whole = Path(_bot_trace(tmp_path, "r").path)
    clean = replay.run([whole], replay.Model(), use_encoder=False)
    assert clean["traces"][0]["captured_fraction"] == 1.0
    assert clean["traces"][0]["truncated"] is False
    assert "TRUNCATED" not in replay.markdown(clean)


def test_rows_are_labelled_by_the_tiers_that_can_use_them(tmp_path: Path) -> None:
    _bot_trace(tmp_path, "rrw")
    result = replay.run([tmp_path / "bot.pcap"], replay.Model(), use_encoder=False)
    assert result["estimate"] == "analytic upper bound"
    assert result["label"].startswith("analytic upper bound")
    assert result["tiers"]["2"].endswith("plain USB/IP only")
    assert result["model"]["parameters"]["link_tiers"] == {
        "100M": ["1", "1.5"],
        "1G": ["2", "native"],
    }
    rows = _rows(result, "bot")
    # BOT reads run far above full speed, so tier 1 (capped near 1 MB/s) drops.
    assert rows["plain"]["throughput_mbps"]["100M"]["rtt_0.2ms"] > 1.0
    for name in ("plain", "write-behind"):
        assert rows[name]["exporter"] == "any"
        assert rows[name]["tiers"] == ["1.5", "2", "native"]
        assert rows[name]["usable_on"] == {"100M": ["1.5"], "1G": ["2", "native"]}
    for name in ("read-ahead", "FILL", "FILL+LZ4", "FILL+LZ4+CACHE", "all"):
        assert "2" not in rows[name]["tiers"], name
        if "unavailable" not in rows[name]:
            assert rows[name]["tiers"] == ["1.5", "native"], name
            assert rows[name]["usable_on"] == {"100M": ["1.5"], "1G": ["native"]}
    assert rows["read-ahead"]["exporter"] == "yusb"
    text = replay.markdown(result)
    assert "analytic upper bound" in text.lower()
    line = {r.split(" | ")[1]: r for r in text.splitlines() if r.startswith("| bot |")}
    assert "| 1.5, 2, native |" in line["plain"]
    assert "| 1.5, native |" in line["read-ahead"]
    assert "capped near 1 MB/s by full-speed USB" in text
    plain_cells = line["plain"].strip("| ").split(" | ")
    ahead_cells = line["read-ahead"].strip("| ").split(" | ")
    assert not any(c.endswith("*") for c in plain_cells[-4:])
    assert [c.endswith("*") for c in ahead_cells[-4:]] == [False, False, True, True]
    assert "encoder unavailable" in line["FILL"]


def test_message_framing() -> None:
    model = replay.Model()
    assert model.message(48) == (48 + 90, 0)
    frames = -(-65584 // 1448)
    assert frames == 46
    assert model.message(65584) == (65584 + frames * 90, 23 * 90)


# --- synthetic traces -----------------------------------------------------------


@pytest.fixture(scope="module")
def traces(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    out = tmp_path_factory.mktemp("usbtrace")
    return {p.stem: p for p in synth.generate(out, seed=3, scale=SMALL)}


def _digest(paths: list[Path]) -> list[str]:
    return [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths]


def test_synthetic_generation_is_deterministic(
    tmp_path: Path, traces: dict[str, Path]
) -> None:
    again = synth.generate(tmp_path / "a", seed=3, scale=SMALL)
    assert _digest(again) == _digest(list(traces.values()))
    other = synth.generate(tmp_path / "b", seed=4, scale=SMALL, only=["disk-zero"])
    assert _digest(other) != _digest([traces["disk-zero"]])


def test_synthetic_traces_pair_cleanly(traces: dict[str, Path]) -> None:
    assert sorted(traces) == sorted(synth.CLASSES)
    for name, path in traces.items():
        trace = replay.load_trace(path)
        assert trace.pairing.unmatched_submits == 0, name
        assert trace.pairing.orphan_completions == 0, name
        for command in trace.commands:
            assert command.csw_status is not None, name
    disk = replay.load_trace(traces["disk-zero"])
    blocks = [
        t.data[i : i + 4096]
        for t in disk.transfers
        if t.payload_length == 65536
        for i in range(0, 65536, 4096)
    ]
    zero = sum(1 for b in blocks if not any(b)) / len(blocks)
    assert 0.6 < zero < 0.8
    hid = replay.load_trace(traces["hid"])
    assert {t.xfer for t in hid.transfers} == {
        pcapfmt.USB_CONTROL,
        pcapfmt.USB_INTERRUPT,
    }
    assert sum(t.outcome == "unlinked" for t in hid.transfers) == 2
    audio = replay.load_trace(traces["audio-iso"])
    assert audio.unsupported and all(
        t.xfer == pcapfmt.USB_ISOCHRONOUS for t in audio.unsupported
    )


# --- replay ---------------------------------------------------------------------


def _rows(result: dict[str, Any], name: str) -> dict[str, dict[str, Any]]:
    trace = next(t for t in result["traces"] if t["name"] == name)
    return {row["config"]: row for row in trace["results"]}


def _speeds(row: dict[str, Any]) -> Iterator[tuple[str, str, float]]:
    for link, by_rtt in row["throughput_mbps"].items():
        for rtt, value in by_rtt.items():
            yield link, rtt, value


def test_replay_latency_levers_without_encoder(traces: dict[str, Path]) -> None:
    result = replay.run(sorted(traces.values()), replay.Model(), use_encoder=False)
    assert result["label"] == replay.LABEL
    assert replay.LABEL == (
        "analytic upper bound; replay model, software-only; not hardware"
    )
    assert result["encoder"]["available"] is False
    for trace in result["traces"]:
        if not trace["supported"]:
            continue
        rows = _rows(result, trace["name"])
        assert rows["FILL"]["unavailable"].startswith("YX encoder unavailable")
        plain = {(link, rtt): v for link, rtt, v in _speeds(rows["plain"])}
        for link, rtt, value in _speeds(rows["write-behind"]):
            assert value >= plain[link, rtt], (trace["name"], link, rtt)
        if trace["name"] in ("disk-zero", "media-jpeg"):  # long sequential reads
            assert rows["read-ahead"]["read_ahead"]["hits"] > 0
            for link, rtt, value in _speeds(rows["read-ahead"]):
                assert value >= plain[link, rtt], (trace["name"], link, rtt)
    files = _rows(result, "files-text")
    assert files["write-behind"]["round_trips"] < files["plain"]["round_trips"]
    audio = next(t for t in result["traces"] if t["name"] == "audio-iso")
    assert audio["supported"] is False and "ADR 0021" in audio["note"]
    hid = _rows(result, "hid")["plain"]
    assert hid["eligible_fraction"] == 0.0
    text = replay.markdown(result)
    assert "not eligible / unsupported" in text and replay.LABEL in text


@pytest.fixture(scope="module")
def yx_library() -> yxenc.Library:
    missing = [
        p
        for p in (ROOT / "native/yusb/src/yx.c", ROOT / "native/third_party/lz4/lz4.c")
        if not p.exists()
    ]
    if missing:
        pytest.skip(f"native YX sources absent: {missing}")
    try:
        yxenc.compiler()
    except yxenc.EncoderUnavailableError:
        pytest.skip("host C compiler unavailable")
    return yxenc.load()


def test_shim_round_trips_through_the_real_encoder(yx_library: yxenc.Library) -> None:
    levers = yxenc.FILL | yxenc.LZ4 | yxenc.CACHE_LEVER
    with (
        yxenc.Encoder(yx_library, levers, 16) as encoder,
        yxenc.Decoder(yx_library, levers, 16) as decoder,
    ):
        assert isinstance(encoder, yxenc.Encoder) and isinstance(decoder, yxenc.Decoder)
        for data in (
            bytes(65536),
            b"log line 42\n" * 4000,
            bytes(range(256)) * 2,
            b"x" * 513,
        ):
            wire = encoder.encode(data)
            assert decoder.decode(wire, len(data)) == data
        assert encoder.encode(bytes(4096)) == b"\x01\x00\x00\x01\x00"  # one FILL unit
        stats = encoder.stats()
    assert stats["blocks_fill"] >= 17 and stats["raw_bytes"] > 0
    with pytest.raises(ValueError, match="error -2"):  # YUSB_ERR_RANGE
        yxenc.Encoder(yx_library, yxenc.CACHE_LEVER, 4097)  # > YUSB_YX_MAX_CACHE_BLOCKS


def test_replay_levers_with_the_real_encoder(
    traces: dict[str, Path], yx_library: yxenc.Library
) -> None:
    result = replay.run(sorted(traces.values()), replay.Model())
    assert result["encoder"]["available"] is True
    disk = _rows(result, "disk-zero")
    assert disk["FILL"]["ratio"] > 2.5 > 1 > disk["plain"]["ratio"]
    assert disk["FILL"]["yx_stats"]["blocks_fill"] > 0
    jpeg = _rows(result, "media-jpeg")["FILL+LZ4"]
    assert 0.99 < jpeg["gain_vs_plain"] <= 1.0
    assert 0.85 < jpeg["ratio"] < 0.95
    assert jpeg["yx_stats"]["lz4_bypass"] > 0
    assert (
        jpeg["yx_stats"]["lz4_tries"] < jpeg["yx_stats"]["blocks_raw"] / 4
    )  # skipping
    text = _rows(result, "files-text")
    assert text["FILL+LZ4"]["ratio"] > 1.4 * text["FILL"]["ratio"]
    assert text["FILL+LZ4+CACHE"]["yx_stats"]["blocks_ref"] > 0
    for row in _rows(result, "hid").values():
        assert row["encoded_bytes"] == 0 and row["gain_vs_plain"] == 1.0
    for name in ("disk-zero", "files-text", "media-jpeg"):
        rows = _rows(result, name)
        plain = {(link, rtt): v for link, rtt, v in _speeds(rows["plain"])}
        for link, rtt, value in _speeds(rows["FILL+LZ4"]):
            assert value >= 0.99 * plain[link, rtt], (name, link, rtt)
        assert rows["FILL+LZ4"]["encode_ms_per_mib_a55_inference"] == pytest.approx(
            rows["FILL+LZ4"]["encode_ms_per_mib_host"] * 6, rel=0.01
        )


def test_replay_cli_markdown(
    traces: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    code = replay.main(
        [str(traces["disk-zero"]), "--no-encoder", "--markdown", "--rtt-ms", "0.5"]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "| disk-zero | write-behind |" in out and "100M 0.5ms" in out


def test_uncaptured_bytes_are_scaled_not_counted_raw(
    tmp_path: Path, yx_library: yxenc.Library
) -> None:
    path = _truncated_trace(tmp_path / "cut.pcap")
    result = replay.run([path], replay.Model())
    row = _rows(result, "cut")["FILL"]
    missing = 3 * 65536 - (65536 + 16384 + 8192)
    assert row["uncaptured_bytes"] == missing
    assert row["uncaptured_unscaled_bytes"] == 0
    # Zeros encode to FILL units, so the scaled estimate is tiny; counted as
    # RAW blocks it would have been at least ``missing`` bytes.
    assert 0 < row["uncaptured_wire_estimate"] < missing // 100
    assert row["encoded_bytes"] == 65536 + 16384 + 8192


def test_trace_without_paired_payload_is_not_called_isochronous(
    tmp_path: Path,
) -> None:
    stream = io.BytesIO()
    pcapfmt.PcapWriter(stream).write_event(event("C", 1, length=8, data=bytes(8)))
    path = tmp_path / "orphan.pcap"
    path.write_bytes(stream.getvalue())
    result = replay.run([path], replay.Model(), use_encoder=False)
    assert result["traces"][0]["supported"] is False
    text = replay.markdown(result)
    assert "no paired payload" in text and "isochronous" not in text


def test_tier_1_stays_on_rows_within_full_speed(tmp_path: Path) -> None:
    """HID report rates sit far below the ~1 MB/s full-speed cap."""
    stream = io.BytesIO()
    writer = pcapfmt.PcapWriter(stream)
    for n in range(4):
        writer.write_event(
            event("S", n, xfer_type=pcapfmt.URB_INTERRUPT, ts_usec=8000 * n, length=8)
        )
        writer.write_event(
            event(
                "C",
                n,
                xfer_type=pcapfmt.URB_INTERRUPT,
                ts_usec=8000 * n + 10,
                length=8,
                data=bytes(8),
            )
        )
    path = tmp_path / "keys.pcap"
    path.write_bytes(stream.getvalue())
    result = replay.run([path], replay.Model(), use_encoder=False)
    rows = _rows(result, "keys")
    assert rows["plain"]["tiers"] == ["1", "1.5", "2", "native"]
    assert rows["plain"]["usable_on"]["100M"] == ["1", "1.5"]
