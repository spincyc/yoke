"""Host-side class drivers for the simulated computer (BOT/SCSI, HID, CDC)."""

from __future__ import annotations

import struct
from dataclasses import dataclass

from tests.usbsim.host import StallError, UsbHost
from yoke.usb import ch9
from yoke.usb.ch9 import Setup

CBW = struct.Struct("<4sIIBBB16s")
CSW = struct.Struct("<4sIIB")


@dataclass
class CommandStatus:
    tag: int
    residue: int
    status: int


class MassStorageHost:
    """Bulk-Only Transport with reset recovery (USB MSC BOT 1.0 §5.3.4)."""

    def __init__(
        self,
        host: UsbHost,
        *,
        bulk_in: int = 0x81,
        bulk_out: int = 0x02,
        interface: int = 0,
        timeout: float = 5.0,
    ) -> None:
        self.host = host
        self.timeout = timeout
        self.bulk_in = bulk_in
        self.bulk_out = bulk_out
        self.interface = interface
        self.tag = 0

    def max_lun(self) -> int:
        return self.host.control(Setup(0xA1, 0xFE, 0, self.interface, 1))[0]

    def reset_recovery(self) -> None:
        self.host.control(Setup(0x21, 0xFF, 0, self.interface, 0))
        self.host.clear_halt(self.bulk_in)
        self.host.clear_halt(self.bulk_out)

    def _csw(self) -> CommandStatus:
        try:
            raw = self.host.transfer_in(self.bulk_in, 13, self.timeout)
        except StallError:
            self.host.clear_halt(self.bulk_in)
            raw = self.host.transfer_in(self.bulk_in, 13, self.timeout)
        signature, tag, residue, status = CSW.unpack(raw)
        if signature != b"USBS" or tag != self.tag:
            raise AssertionError(f"bad CSW {raw.hex()}")
        return CommandStatus(tag, residue, status)

    def command(
        self, cb: bytes, *, data_in: int = 0, data_out: bytes = b""
    ) -> tuple[bytes, CommandStatus]:
        self.tag += 1
        length = data_in or len(data_out)
        flags = 0x80 if data_in else 0
        cbw = CBW.pack(
            b"USBC", self.tag, length, flags, 0, len(cb), cb.ljust(16, b"\0")
        )
        self.host.transfer_out(self.bulk_out, cbw, timeout=self.timeout)
        data = b""
        if data_in:
            try:
                data = self.host.transfer_in(self.bulk_in, data_in, self.timeout)
            except StallError:
                self.host.clear_halt(self.bulk_in)
        elif data_out:
            try:
                self.host.transfer_out(self.bulk_out, data_out, timeout=self.timeout)
            except StallError:
                self.host.clear_halt(self.bulk_out)
        return data, self._csw()

    def inquiry(self) -> bytes:
        data, status = self.command(bytes([0x12, 0, 0, 0, 36, 0]), data_in=36)
        assert status.status == 0
        return data

    def read_capacity(self) -> tuple[int, int]:
        data, status = self.command(bytes([0x25]) + bytes(9), data_in=8)
        assert status.status == 0
        last, block = struct.unpack(">II", data)
        return last, block

    def read10(self, lba: int, blocks: int, block_size: int = 512) -> bytes:
        cb = struct.pack(">BBIBHB", 0x28, 0, lba, 0, blocks, 0)
        data, status = self.command(cb, data_in=blocks * block_size)
        if status.status:
            raise AssertionError(f"READ(10) failed: {status}")
        return data

    def write10(self, lba: int, data: bytes, block_size: int = 512) -> None:
        cb = struct.pack(">BBIBHB", 0x2A, 0, lba, 0, len(data) // block_size, 0)
        _, status = self.command(cb, data_out=data)
        if status.status:
            raise AssertionError(f"WRITE(10) failed: {status}")


class HidHost:
    def __init__(
        self, host: UsbHost, *, interface: int = 0, endpoint: int = 0x81
    ) -> None:
        self.host = host
        self.interface = interface
        self.endpoint = endpoint

    def set_idle(self) -> bool:
        try:
            self.host.control(Setup(0x21, 0x0A, 0, self.interface, 0))
        except StallError:
            return False
        return True

    def report_descriptor(self, length: int) -> bytes:
        return self.host.control(
            Setup(0x81, ch9.GET_DESCRIPTOR, 0x2200, self.interface, length)
        )

    def set_leds(self, leds: int) -> None:
        self.host.control(Setup(0x21, 0x09, 0x0200, self.interface, 1), bytes([leds]))

    def set_protocol(self, protocol: int) -> None:
        self.host.control(Setup(0x21, 0x0B, protocol, self.interface, 0))

    def get_protocol(self) -> int:
        return self.host.control(Setup(0xA1, 0x03, 0, self.interface, 1))[0]

    def read_report(self, length: int = 8, timeout: float = 2.0) -> bytes:
        return self.host.transfer_in(self.endpoint, length, timeout)


class CdcHost:
    def __init__(self, host: UsbHost, *, interface: int = 0) -> None:
        self.host = host
        self.interface = interface

    def set_line_coding(
        self, baud: int, stop: int = 0, parity: int = 0, bits: int = 8
    ) -> None:
        data = struct.pack("<IBBB", baud, stop, parity, bits)
        self.host.control(Setup(0x21, 0x20, 0, self.interface, 7), data)

    def get_line_coding(self) -> tuple[int, int, int, int]:
        raw = self.host.control(Setup(0xA1, 0x21, 0, self.interface, 7))
        baud, stop, parity, bits = struct.unpack("<IBBB", raw)
        return baud, stop, parity, bits

    def set_control_lines(self, value: int) -> None:
        self.host.control(Setup(0x21, 0x22, value, self.interface, 0))
