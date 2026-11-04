"""USB/IP 1.1.1 codec and client multiplexer, checked against the protocol doc.

Offsets come from https://docs.kernel.org/usb/usbip_protocol.html tables;
captured bytes come from that document's HID example. No network is used:
in-memory streams stand in for TCP.
"""

from __future__ import annotations

import dataclasses
import struct
import threading

import pytest

from tests.usbsim.streams import memory_pair
from yoke.usb import usbip
from yoke.usb.ch9 import Setup, get_descriptor
from yoke.usb.client import (
    ConnectionLostError,
    ImportRejectedError,
    UsbipConnection,
    import_device,
    list_devices,
    recv_exact,
)
from yoke.usb.usbip import CmdSubmit, CmdUnlink, DeviceInfo, ProtocolError, RetSubmit

INFO = DeviceInfo(
    path="/sys/devices/platform/fcd00000.usb/usb3/3-1",
    busid="3-1",
    busnum=3,
    devnum=2,
    speed=3,
    vendor=0x1209,
    product=0x0001,
    bcd_device=0x0100,
    device_class=0,
    device_subclass=0,
    device_protocol=0,
    configuration_value=1,
    num_configurations=1,
    num_interfaces=1,
    interfaces=((0xFF, 0, 0),),
)


def words(text: str) -> bytes:
    return bytes.fromhex(text.replace(" ", ""))


# From the protocol document's EXAMPLE section (HID interrupt traffic).
CMD_INTR_IN = words(
    "00000001 00000d05 0001000f 00000001 00000001 00000200 00000040 ffffffff "
    "00000000 00000004 00000000 00000000"
)
RET_INTR_IN_HEADER = words(
    "00000003 00000d05 00000000 00000000 00000000 00000000 00000040 ffffffff "
    "00000000 00000000 00000000 00000000"
)


def test_documented_capture_decodes() -> None:
    cmd = usbip.decode_header(CMD_INTR_IN)
    assert isinstance(cmd, CmdSubmit)
    assert (cmd.seqnum, cmd.devid, cmd.direction, cmd.ep) == (0xD05, 0x1000F, 1, 1)
    assert cmd.transfer_flags == usbip.URB_DIR_IN
    assert cmd.transfer_buffer_length == 64 and cmd.interval == 4
    ret = usbip.decode_header(RET_INTR_IN_HEADER)
    assert isinstance(ret, RetSubmit)
    assert (ret.seqnum, ret.status, ret.actual_length) == (0xD05, 0, 64)


def test_cmd_submit_layout_matches_table() -> None:
    raw = CmdSubmit(
        seqnum=7,
        devid=0x30002,
        direction=usbip.DIR_IN,
        ep=1,
        transfer_flags=usbip.URB_DIR_IN,
        transfer_buffer_length=512,
        interval=8,
        setup=bytes(range(8)),
    ).pack()
    assert len(raw) == usbip.HEADER_SIZE == 48
    fields = struct.unpack(">IIIIIIIIII8s", raw)
    assert fields[:5] == (usbip.CMD_SUBMIT, 7, 0x30002, 1, 1)
    assert fields[5:10] == (0x200, 512, 0, 0xFFFFFFFF, 8)  # documented non-ISO
    assert fields[10] == bytes(range(8))
    assert usbip.decode_header(raw) == CmdSubmit(
        7, 0x30002, 1, 1, 0x200, 512, 8, bytes(range(8))
    )


def test_ret_and_unlink_layouts() -> None:
    ret = RetSubmit(9, -usbip.EPIPE, 3).pack()
    assert struct.unpack_from(">IIIII", ret) == (usbip.RET_SUBMIT, 9, 0, 0, 0)
    assert struct.unpack_from(">ii", ret, 0x14) == (-32, 3)
    assert ret[0x28:0x30] == b"\0" * 8
    unlink = CmdUnlink(10, 0x30002, 9).pack()
    assert struct.unpack_from(">IIII", unlink) == (usbip.CMD_UNLINK, 10, 0x30002, 0)
    assert (
        struct.unpack_from(">I", unlink, 0x14) == (9,) and unlink[0x18:] == b"\0" * 24
    )
    ret_unlink = usbip.RetUnlink(10, -usbip.ECONNRESET).pack()
    assert struct.unpack_from(">i", ret_unlink, 0x14) == (-104,)


def test_import_messages_match_table_offsets() -> None:
    request = usbip.encode_req_import("3-1")
    assert request[:8] == bytes.fromhex("0111 8003 00000000".replace(" ", ""))
    assert request[8:] == b"3-1".ljust(32, b"\0")
    reply = usbip.encode_rep_import(INFO)
    assert reply[:8] == bytes.fromhex("0111000300000000")
    assert reply[0x08 : 0x08 + 4] == b"/sys"
    assert reply[0x108 : 0x108 + 4] == b"3-1\0"
    assert struct.unpack_from(">III", reply, 0x128) == (3, 2, 3)
    assert struct.unpack_from(">HHH", reply, 0x134) == (0x1209, 1, 0x100)
    assert reply[0x13D:0x140] == bytes([1, 1, 1])
    assert len(reply) == 0x140
    assert DeviceInfo.unpack(reply[8:]) == dataclasses.replace(INFO, interfaces=())
    assert usbip.encode_rep_import(None) == bytes.fromhex("0111000300000001")
    assert INFO.devid == 0x30002


def test_devlist_includes_interfaces() -> None:
    reply = usbip.encode_rep_devlist([INFO, INFO])
    assert struct.unpack_from(">I", reply, 8) == (2,)
    assert reply[0x0C + 0x138 : 0x0C + 0x13C] == bytes([0xFF, 0, 0, 0])
    assert len(reply) == 0x0C + 2 * (0x138 + 4)


@pytest.mark.parametrize(
    "raw",
    [
        b"\0" * 47,
        struct.pack(">IIIII", 9, 1, 0, 0, 0) + b"\0" * 28,
        CmdSubmit(1, 1, 1, 16 & 0xF, 0, 1).pack()[:16]
        + struct.pack(">I", 16)
        + b"\0" * 28,
        struct.pack(">IIIIIii", usbip.RET_SUBMIT, 1, 0, 0, 0, 0, -1) + b"\0" * 20,
        struct.pack(">IIIIIIi", usbip.CMD_SUBMIT, 1, 0, 0, 0, 0, 1 << 30) + b"\0" * 20,
    ],
)
def test_malformed_headers_are_protocol_errors(raw: bytes) -> None:
    with pytest.raises(ProtocolError):
        usbip.decode_header(raw)


def test_version_and_code_are_checked() -> None:
    with pytest.raises(ProtocolError, match="version"):
        usbip.parse_op_header(bytes.fromhex("0106000300000000"), usbip.OP_REP_IMPORT)
    with pytest.raises(ProtocolError, match="expected op"):
        usbip.parse_op_header(bytes.fromhex("0111000500000000"), usbip.OP_REP_IMPORT)
    with pytest.raises(ValueError, match="busid"):
        usbip.encode_req_import("x" * 32)


class ScriptedExporter:
    """Minimal server side for client tests (no device model)."""

    def __init__(self) -> None:
        self.client, self.server, self.link = memory_pair()

    def accept_import(self, info: DeviceInfo = INFO) -> None:
        body = bytearray(40)
        recv_exact(self.server, memoryview(body))
        assert usbip.decode_req_import(body[8:]) == info.busid
        self.server.sendall(usbip.encode_rep_import(info))

    def read_command(self) -> usbip.Message:
        header = bytearray(48)
        recv_exact(self.server, memoryview(header))
        message = usbip.decode_header(header)
        if isinstance(message, CmdSubmit) and message.direction == usbip.DIR_OUT:
            payload = bytearray(message.transfer_buffer_length)
            recv_exact(self.server, memoryview(payload))
        return message


def connect(exporter: ScriptedExporter) -> UsbipConnection:
    thread = threading.Thread(target=exporter.accept_import)
    thread.start()
    info = import_device(exporter.client, "3-1", timeout=2)
    thread.join()
    conn = UsbipConnection(exporter.client, info)
    conn.start()
    return conn


def test_import_rejection_and_busid_mismatch() -> None:
    exporter = ScriptedExporter()
    exporter.server.sendall(usbip.encode_rep_import(None))
    with pytest.raises(ImportRejectedError):
        import_device(exporter.client, "3-1", timeout=2)
    exporter = ScriptedExporter()
    other = dataclasses.replace(INFO, busid="9-9")
    exporter.server.sendall(usbip.encode_rep_import(other))
    with pytest.raises(ProtocolError, match="busid"):
        import_device(exporter.client, "3-1", timeout=2)


def test_devlist_round_trip() -> None:
    client, server, _ = memory_pair()
    server.sendall(usbip.encode_rep_devlist([INFO]))
    assert list_devices(client, timeout=2) == [INFO]


def test_submit_carries_devid_and_control_round_trip() -> None:
    exporter = ScriptedExporter()
    conn = connect(exporter)
    setup = get_descriptor(1, 0, 18)
    result: list[bytes | None] = []
    thread = threading.Thread(
        target=lambda: result.append(conn.control(setup, timeout=2))
    )
    thread.start()
    cmd = exporter.read_command()
    assert isinstance(cmd, CmdSubmit)
    assert cmd.devid == INFO.devid and cmd.ep == 0 and cmd.direction == usbip.DIR_IN
    assert Setup.unpack(cmd.setup) == setup and cmd.transfer_buffer_length == 18
    exporter.server.sendall(RetSubmit(cmd.seqnum, 0, 4).pack() + b"\x12\x01\x00\x02")
    thread.join(2)
    assert result == [b"\x12\x01\x00\x02"]
    conn.close()


def test_unlink_before_and_after_completion() -> None:
    exporter = ScriptedExporter()
    conn = connect(exporter)
    first = conn.submit(1, usbip.DIR_IN, 512)
    second = conn.submit(1, usbip.DIR_IN, 512)
    cmds = [exporter.read_command(), exporter.read_command()]
    # The first completes before its unlink arrives: RET_UNLINK status 0.
    exporter.server.sendall(RetSubmit(cmds[0].seqnum, 0, 1).pack() + b"x")
    assert first.wait(2).data == b"x"
    handle = conn.unlink(first)
    unlink = exporter.read_command()
    assert isinstance(unlink, CmdUnlink) and unlink.unlink_seqnum == cmds[0].seqnum
    exporter.server.sendall(usbip.RetUnlink(unlink.seqnum, 0).pack())
    assert handle.wait(2).status == 0
    # The second is cancelled: no RET_SUBMIT follows RET_UNLINK(-ECONNRESET).
    handle = conn.unlink(second)
    unlink = exporter.read_command()
    exporter.server.sendall(usbip.RetUnlink(unlink.seqnum, -usbip.ECONNRESET).pack())
    assert handle.wait(2).status == -usbip.ECONNRESET
    assert second.wait(2).status == -usbip.ECONNRESET
    conn.close()


def test_connection_loss_fails_pending_and_unknown_reply_closes() -> None:
    exporter = ScriptedExporter()
    lost: list[BaseException] = []
    thread = threading.Thread(target=exporter.accept_import)
    thread.start()
    info = import_device(exporter.client, "3-1", timeout=2)
    thread.join()
    conn = UsbipConnection(exporter.client, info, on_lost=lost.append)
    conn.start()
    urb = conn.submit(1, usbip.DIR_IN, 64)
    exporter.read_command()
    exporter.server.sendall(RetSubmit(999, 0, 0).pack())
    with pytest.raises(ConnectionLostError):
        urb.wait(2)
    assert lost and "unknown seqnum" in str(lost[0])
    with pytest.raises(ConnectionLostError):
        conn.submit(1, usbip.DIR_IN, 64)


def test_reply_longer_than_request_is_rejected() -> None:
    exporter = ScriptedExporter()
    conn = connect(exporter)
    urb = conn.submit(1, usbip.DIR_IN, 4)
    cmd = exporter.read_command()
    exporter.server.sendall(RetSubmit(cmd.seqnum, 0, 8).pack() + b"12345678")
    with pytest.raises(ConnectionLostError, match="exceeds"):
        urb.wait(2)


def test_tcp_reset_fails_pending_urbs() -> None:
    exporter = ScriptedExporter()
    conn = connect(exporter)
    urb = conn.submit(1, usbip.DIR_IN, 64)
    exporter.read_command()
    exporter.link.reset()
    with pytest.raises(ConnectionLostError):
        urb.wait(2)
    assert conn.lost is not None


def test_batched_submissions_use_one_write() -> None:
    from yoke.usb.client import Submission

    exporter = ScriptedExporter()
    conn = connect(exporter)
    before = exporter.client.sends
    urbs = conn.submit_many([Submission(1, usbip.DIR_IN, 512) for _ in range(4)])
    assert exporter.client.sends == before + 1
    seqnums = [exporter.read_command().seqnum for _ in range(4)]
    assert seqnums == [u.seqnum for u in urbs] == sorted(seqnums)
    conn.close()
