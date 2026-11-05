"""The device-side export core (ADR 0027) over real TCP, and the YX extension.

``yusb-export-sim`` serves a simulated BOT disk through the export core.
Its peers are the Python reference client and proxy (interoperability:
any tier talks plain USB/IP to any other) and the native worker, which
also negotiates the ADR 0028 extension with it. Data is checked against
the disk file the server maps. Simulated software evidence only.
"""

from __future__ import annotations

import random
import socket
import struct
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from tests.usbsim.classes import MassStorageHost
from tests.usbsim.devices import Loopback
from tests.usbsim.harness import Transport, make_rig
from tests.usbsim.native import ExportServer, NativeRig, make_native_rig, wait_for

from yoke.usb import usbip
from yoke.usb.ch9 import Setup
from yoke.usb.client import (
    ImportRejectedError,
    UsbipConnection,
    import_device,
    list_devices,
)
from yoke.usb.descriptors import build_presentation

MIB = 1024 * 1024


@contextmanager
def server(*options: str) -> Iterator[ExportServer]:
    exporter = ExportServer(*options)
    try:
        yield exporter
    finally:
        exporter.stop()


def connect(exporter: ExportServer) -> socket.socket:
    sock = socket.create_connection(exporter.address, timeout=5)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return sock


def test_reference_client_imports_and_builds_the_presentation() -> None:
    with server() as exporter:
        sock = connect(exporter)
        info = import_device(sock, "1-1", timeout=3)
        assert (info.vendor, info.product, info.speed) == (0x1209, 0x5EEF, 3)
        conn = UsbipConnection(sock, info)
        conn.start()
        try:
            pres = build_presentation(
                lambda s: conn.control(s, timeout=3), info.speed, power_ma=500
            )
            alt = pres.configurations[0].alt(0, 0)
            assert alt is not None and alt.interface_class == 8
            assert pres.strings[(2, 0x0409)] is not None
        finally:
            conn.close()


def test_devlist_and_refused_import() -> None:
    with server() as exporter:
        devices = list_devices(connect(exporter), timeout=3)
        assert [d.busid for d in devices] == ["1-1"]
        assert devices[0].interfaces == ((8, 6, 0x50),)
        with pytest.raises(ImportRejectedError):
            import_device(connect(exporter), "9-9", timeout=3)
        sock = connect(exporter)
        import_device(sock, "1-1", timeout=3)
        with pytest.raises(ImportRejectedError):  # one importer at a time
            import_device(connect(exporter), "1-1", timeout=3)
        sock.close()


def test_unlink_of_a_pending_urb_and_of_an_unknown_seqnum() -> None:
    with server() as exporter:
        sock = connect(exporter)
        info = import_device(sock, "1-1", timeout=3)
        conn = UsbipConnection(sock, info)
        conn.start()
        try:
            conn.control(Setup(0, 9, 1, 0, 0), timeout=3)  # SET_CONFIGURATION (tweak)
            urb = conn.submit(1, usbip.DIR_IN, 512)  # no CBW: stays pending
            conn.cancel([urb], timeout=3)
            assert urb.result is not None and urb.result.unlinked
            assert urb.result.status == -usbip.ECONNRESET
            handle = conn.unlink(urb)  # already gone: RET_UNLINK(0)
            assert handle.wait(3).status == 0
            assert conn.lost is None
        finally:
            conn.close()


def test_protocol_violations_close_the_connection() -> None:
    with server() as exporter:
        sock = connect(exporter)
        info = import_device(sock, "1-1", timeout=3)
        sock.sendall(usbip.CmdSubmit(1, info.devid + 1, 1, 0, 0x200, 0).pack())
        sock.settimeout(3)
        assert sock.recv(64) == b""  # closed for the devid mismatch
        sock = connect(exporter)
        info = import_device(sock, "1-1", timeout=3)
        setup = Setup(0x80, 6, 0x0100, 0, 18).pack()
        bad = usbip.CmdSubmit(2, info.devid, 1, 0, 0x200, 64, setup=setup).pack()
        sock.sendall(bad)  # wLength != transfer_buffer_length
        assert sock.recv(64) == b""


def test_reference_proxy_presents_the_export_core() -> None:
    with server() as exporter:
        transport = Transport(kind="tcp", address=exporter.address)
        rig = make_rig(Loopback(), transport=transport)
        try:
            rig.host.attach()
            rig.host.enumerate("windows")
            storage = MassStorageHost(rig.host)
            assert storage.read_capacity() == (8 * MIB // 512 - 1, 512)
            payload = random.Random(1).randbytes(300 * 512)
            storage.write10(64, payload)
            assert exporter.read_disk(64 * 512, len(payload)) == payload
            assert storage.read10(64, 300) == payload
            data, status = storage.command(bytes([0xFF]) + bytes(5), data_in=64)
            assert status.status == 1 and data == b""
            assert rig.host.violations == []
        finally:
            rig.close()


@contextmanager
def native_to(exporter: ExportServer, *options: str) -> Iterator[NativeRig]:
    rig = make_native_rig(Loopback(), options=options, present=False)
    try:
        rig.address = exporter.address
        rig.present()
        rig.host.attach()
        rig.host.enumerate("linux")
        yield rig
    finally:
        rig.close()


def corpus() -> dict[str, bytes]:
    rng = random.Random(7)
    text = b"".join(
        b"%06d yoke usb/ip payload line, mostly the same words again\n" % i
        for i in range(6000)
    )
    return {
        "zeros": bytes(256 * 1024),
        "text": text[: 256 * 1024],
        "random": rng.randbytes(256 * 1024),
    }


@pytest.mark.parametrize(
    "levers", ["none", "fill", "fill,lz4", "fill,lz4,cache"], ids=lambda s: s
)
def test_native_worker_and_export_core_negotiate_yx(levers: str) -> None:
    options = ("--yx", levers, "--yx-cache", "64")
    with server(*options) as exporter, native_to(exporter, *options) as rig:
        storage = MassStorageHost(rig.host)
        lba = 128
        for name, payload in corpus().items():
            blocks = len(payload) // 512
            storage.write10(lba, payload)
            assert exporter.read_disk(lba * 512, len(payload)) == payload, name
            assert storage.read10(lba, blocks) == payload, name
            assert storage.read10(lba, blocks) == payload, name  # re-read: cache hits
            lba += blocks
        wait_for(lambda: rig.counters().get("in_bytes", 0) > 0)
        counters = rig.counters()
        if levers == "none":
            assert counters["yx_raw_in"] == counters["yx_raw_out"] == 0
        else:
            # Zeros and text shrink on the wire in both directions.
            assert 0 < counters["yx_wire_in"] < counters["yx_raw_in"] * 0.8
            assert 0 < counters["yx_wire_out"] < counters["yx_raw_out"] * 0.8
        assert rig.host.violations == []


@pytest.mark.parametrize("yx", ["none", "fill,lz4,cache"])
def test_small_buffer_pool_holds_urbs_back_without_losing_data(yx: str) -> None:
    """Few slots (two for reads, one kept for writes) and a pool for two
    64 KiB reads with their encoded replies, besides the control and write
    reserves: the proxy's pipelined reads and write-behind are held and
    replayed in order; nothing fails."""
    options = ("--yx", yx, "--yx-cache", "16")
    pool = ("--max-urbs", "4", "--max-urb-bytes", "65536", "--pool-bytes", "330000")
    with (
        server(*options, *pool) as exporter,
        native_to(exporter, *options) as rig,
    ):
        storage = MassStorageHost(rig.host)
        lba = 64
        for name, payload in corpus().items():
            blocks = len(payload) // 512
            storage.write10(lba, payload)
            assert exporter.read_disk(lba * 512, len(payload)) == payload, name
            assert storage.read10(lba, blocks) == payload, name
            lba += blocks
        wait_for(lambda: rig.counters().get("in_bytes", 0) > 0)
        counters = rig.counters()
        assert (counters["yx_raw_in"] > 0) == (yx != "none")
        assert rig.host.violations == []


def test_yx_offer_falls_back_to_plain_usbip_with_a_stock_exporter() -> None:
    """The reference usbipd model ignores the offer bytes; nothing changes."""
    rig = make_native_rig(Loopback(), options=("--yx", "fill,lz4"))
    try:
        rig.host.attach()
        rig.host.enumerate("linux")
        rig.host.transfer_out(0x02, bytes(4000))  # ends with a short packet
        assert rig.host.transfer_in(0x81, 8192) == bytes(4000)
        assert rig.counters().get("yx_raw_out", 0) == 0
        assert rig.usbipd.violations == []
    finally:
        rig.close()


def test_import_offer_bytes_are_ignored_by_the_reference_decoder() -> None:
    raw = bytearray(usbip.encode_req_import("1-1"))
    raw[8 + 24 : 8 + 32] = b"YX\x01\x03\x00\x40\x00\x00"
    assert usbip.decode_req_import(bytes(raw[8:])) == "1-1"
    record = bytearray(
        usbip.DeviceInfo("/sys/x", "1-1", 1, 2, 3, 1, 2, 3, 0, 0, 0, 1, 1, 1).pack()
    )
    record[248:256] = b"YX\x01\x03\x00\x40\x00\x00"
    assert usbip.DeviceInfo.unpack(bytes(record)).path == "/sys/x"
    assert struct.unpack(">2sBB", bytes(record[248:252])) == (b"YX", 1, 3)
