"""Simulated USB device models behind the simulated exporter.

Every class here is a software model built on :class:`DeviceModel`. The
behavior is written from the USB 2.0, HID 1.11, CDC 1.20 (PSTN), Mass
Storage Bulk-Only Transport 1.0 and SPC/SBC specifications; none of it was
measured on hardware. Results obtained with these models are simulated
software evidence, never hardware or interoperability evidence.

Vendor ID 0x1209 is the pid.codes open-source VID. The product IDs are
local test values that identify each model, not allocated products.

Conventions shared by the models:

* Attributes are set before ``super().__init__`` because the base applies
  configuration 1, which runs :meth:`DeviceModel.on_configuration`.
* Public methods take the model lock, so completion callbacks run after
  they return.
* IN halts go through :meth:`_SimDevice.halt_in`, which applies the halt
  when an URB finds the endpoint's queue empty. That URB fails with
  ``-EPIPE`` after any partial data, which is what a Linux host observes,
  even when the host submits it after the device decided to stall.
"""

from __future__ import annotations

import struct
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import ClassVar

from tests.usbsim import build
from tests.usbsim.device import Complete, DeviceModel, InPipe, Urb, Verdict
from yoke.usb import ch9
from yoke.usb.ch9 import Setup, Speed

VENDOR_ID = 0x1209
MANUFACTURER = "Yoke simulation"

BULK = ch9.XFER_BULK
INTERRUPT = ch9.XFER_INT
ISO_ADAPTIVE = ch9.XFER_ISOC | 0x08

# bmRequestType values: direction | type | recipient.
CLASS_IN_INTERFACE = 0xA1
CLASS_OUT_INTERFACE = 0x21
VENDOR_IN_DEVICE = 0xC0
VENDOR_OUT_DEVICE = 0x40


# --- descriptor helpers ----------------------------------------------------


@dataclass(frozen=True)
class _Spec:
    """Descriptor inputs for one model.

    ``other_speed`` is the configuration as it would be at full speed; when
    present the model also answers GET_DESCRIPTOR(DEVICE_QUALIFIER). When
    absent, a bcdUSB 0x0200 device is full-speed only and both requests
    STALL.
    """

    speed: Speed
    product_id: int
    product: str
    configuration: bytes
    other_speed: bytes | None = None
    bcd_usb: int = 0x0200
    device_class: tuple[int, int, int] = (0, 0, 0)
    max_packet0: int = 64
    bos: bytes | None = None
    strings: dict[int, str] = field(default_factory=dict)
    class_descriptors: dict[tuple[int, int, int], bytes] = field(default_factory=dict)


def _interface(
    number: int,
    cls: int,
    endpoints: list[bytes],
    *,
    subclass: int = 0,
    protocol: int = 0,
    alternate: int = 0,
    string: int = 0,
    extra: bytes = b"",
) -> bytes:
    """An interface descriptor, its class descriptors, then its endpoints."""
    head = build.interface(
        number,
        alternate=alternate,
        endpoints=len(endpoints),
        cls=cls,
        subclass=subclass,
        protocol=protocol,
        string=string,
    )
    return head + extra + b"".join(endpoints)


def _config(
    *interfaces: bytes,
    count: int,
    other_speed: bool = False,
    attributes: int = 0x80,
) -> bytes:
    return build.configuration(
        b"".join(interfaces),
        interfaces=count,
        other_speed=other_speed,
        attributes=attributes,
    )


def _bulk_pair(in_address: int, out_address: int, size: int) -> list[bytes]:
    return [
        build.endpoint(in_address, BULK, size),
        build.endpoint(out_address, BULK, size),
    ]


def _pattern(length: int) -> bytes:
    return bytes(i & 0xFF for i in range(length))


# --- shared base -------------------------------------------------------------


class _SimDevice(DeviceModel):
    """Common descriptor assembly and IN-halt handling for the models."""

    def __init__(self, spec: _Spec) -> None:
        self._halt_pending: dict[int, InPipe] = {}
        cls, subclass, protocol = spec.device_class
        strings = {
            1: MANUFACTURER,
            2: spec.product,
            3: f"{VENDOR_ID:04X}{spec.product_id:04X}0001",
        }
        strings.update(spec.strings)
        qualifier = None
        if spec.other_speed is not None:
            qualifier = build.qualifier(
                bcd_usb=spec.bcd_usb,
                device_class=cls,
                max_packet0=spec.max_packet0,
            )
        super().__init__(
            speed=spec.speed,
            device=build.device(
                vendor=VENDOR_ID,
                product=spec.product_id,
                bcd_usb=spec.bcd_usb,
                device_class=cls,
                subclass=subclass,
                protocol=protocol,
                max_packet0=spec.max_packet0,
            ),
            configurations=[spec.configuration],
            strings=strings,
            qualifier=qualifier,
            other_speed=spec.other_speed,
            bos=spec.bos,
            class_descriptors=dict(spec.class_descriptors),
        )

    def _queue(self, address: int, data: bytes, *, zlp: bool = False) -> bool:
        """Queue IN data (lock held); False when the endpoint is not live."""
        pipe = self.pipes.get(address)
        if self.unplugged or not isinstance(pipe, InPipe):
            return False
        pipe.send(data, zlp=zlp)
        return True

    def halt_in(self, address: int) -> None:
        """Halt IN ``address`` once its queued packets are read (lock held).

        The base :meth:`InPipe.stall` halts at once: packets still queued
        would follow the stall, and an URB submitted to the halted pipe
        waits for CLEAR_FEATURE instead of failing. Deferring the halt until
        an URB finds the queue empty gives that URB ``-EPIPE`` after any
        partial data. CLEAR_FEATURE(ENDPOINT_HALT) cancels a pending halt.
        """
        pipe = self.pipes.get(address)
        if isinstance(pipe, InPipe):
            self._halt_pending[address] = pipe
            self._apply_halt(address)

    def _apply_halt(self, address: int) -> None:
        pipe = self._halt_pending.get(address)
        if pipe is None:
            return
        if self.pipes.get(address) is not pipe:
            # Pipes were rebuilt by a configuration, interface or reset.
            del self._halt_pending[address]
            return
        if pipe.urbs and not pipe.packets and not pipe.halted:
            del self._halt_pending[address]
            pipe.stall()

    def submit_in(self, address: int, length: int, complete: Complete) -> Urb:
        with self.locked():
            urb = super().submit_in(address, length, complete)
            self._apply_halt(address)
            return urb

    def clear_halt(self, address: int) -> bool:
        with self.locked():
            self._halt_pending.pop(address, None)
            return super().clear_halt(address)


# --- HID -------------------------------------------------------------------

KEYBOARD_REPORT_DESCRIPTOR = bytes.fromhex(
    "05 01 09 06 a1 01"  # Generic Desktop / Keyboard / Collection (Application)
    " 05 07 19 e0 29 e7 15 00 25 01 75 01 95 08 81 02"  # 8 modifier bits
    " 95 01 75 08 81 01"  # reserved byte: Input (Constant)
    " 95 05 75 01 05 08 19 01 29 05 91 02"  # 5 LED bits: Output (Variable)
    " 95 01 75 03 91 01"  # 3 LED padding bits: Output (Constant)
    " 95 06 75 08 15 00 25 65 05 07 19 00 29 65 81 00"  # 6 key codes: Array
    " c0"
)
"""HID 1.11 Appendix B.1 boot keyboard report descriptor (63 bytes)."""

MOUSE_REPORT_DESCRIPTOR = bytes.fromhex(
    "05 01 09 02 a1 01 09 01 a1 00"  # Generic Desktop / Mouse / Pointer
    " 05 09 19 01 29 03 15 00 25 01 95 03 75 01 81 02"  # 3 buttons
    " 95 01 75 05 81 01"  # 5 padding bits: Input (Constant)
    " 05 01 09 30 09 31 09 38 15 81 25 7f 75 08 95 03 81 06"  # X, Y, wheel
    " c0 c0"
)
"""Boot-compatible mouse (52 bytes): 3 buttons, signed 8-bit X, Y, wheel."""

REPORT_PROTOCOL = 1
BOOT_PROTOCOL = 0
ERROR_ROLL_OVER = 0x01

HID_GET_REPORT = 0x01
HID_GET_IDLE = 0x02
HID_GET_PROTOCOL = 0x03
HID_SET_REPORT = 0x09
HID_SET_IDLE = 0x0A
HID_SET_PROTOCOL = 0x0B
REPORT_INPUT = 1
REPORT_OUTPUT = 2


def _hid_interface(
    number: int,
    *,
    protocol: int,
    report: bytes,
    packet: int,
    interval: int,
    string: int = 0,
) -> bytes:
    return _interface(
        number,
        ch9.CLASS_HID,
        [build.endpoint(0x81, INTERRUPT, packet, interval)],
        subclass=1,
        protocol=protocol,
        string=string,
        extra=build.hid(len(report)),
    )


def _hid_descriptors(
    interface: int, report: bytes
) -> dict[tuple[int, int, int], bytes]:
    return {
        (interface, ch9.DT_HID_REPORT, 0): report,
        (interface, ch9.DT_HID, 0): build.hid(len(report)),
    }


def keyboard_report(usages: tuple[int, ...], modifiers: int = 0) -> bytes:
    """An 8-byte boot keyboard input report; >6 keys report ErrorRollOver."""
    if not 0 <= modifiers <= 0xFF:
        raise ValueError(f"modifier byte out of range: {modifiers}")
    for usage in usages:
        if not 0 <= usage <= 0xFF:
            raise ValueError(f"key usage out of range: {usage}")
    keys = list(usages) if len(usages) <= 6 else [ERROR_ROLL_OVER] * 6
    return bytes([modifiers, 0, *keys, *[0] * (6 - len(keys))])


class _HidDevice(_SimDevice):
    """HID class requests for one boot interface whose input is on 0x81.

    The idle rate is stored and reported but not acted on: the models have
    no clock, so no report is repeated on idle expiry.
    """

    REPORT_ENDPOINT: ClassVar[int] = 0x81

    def __init__(
        self, spec: _Spec, *, interface: int, report_size: int, idle: int
    ) -> None:
        self.hid_interface = interface
        self.report = bytes(report_size)
        self.default_idle = idle
        self.idle = idle
        self.protocol = REPORT_PROTOCOL
        super().__init__(spec)

    def output_report(self) -> bytes | None:
        """GET_REPORT(Output) answer; None when there is no output report."""
        return None

    def set_output_report(self, data: bytes) -> bool:
        """Apply SET_REPORT(Output); False STALLs the request."""
        return False

    def class_request(self, setup: Setup, data: bytes) -> bytes | None:
        if setup.recipient != ch9.RECIP_INTERFACE or setup.index != self.hid_interface:
            return None
        request = (setup.request_type, setup.request)
        if request == (CLASS_IN_INTERFACE, HID_GET_REPORT):
            if setup.value >> 8 == REPORT_INPUT:
                return self.report
            if setup.value >> 8 == REPORT_OUTPUT:
                return self.output_report()
            return None
        if request == (CLASS_OUT_INTERFACE, HID_SET_REPORT):
            if setup.value == REPORT_OUTPUT << 8 and self.set_output_report(data):
                return b""
            return None
        if request == (CLASS_OUT_INTERFACE, HID_SET_IDLE):
            self.idle = setup.value >> 8
            return b""
        if request == (CLASS_IN_INTERFACE, HID_GET_IDLE):
            return bytes([self.idle])
        if request == (CLASS_OUT_INTERFACE, HID_SET_PROTOCOL):
            if setup.value not in (BOOT_PROTOCOL, REPORT_PROTOCOL):
                return None
            self.protocol = setup.value
            return b""
        if request == (CLASS_IN_INTERFACE, HID_GET_PROTOCOL):
            return bytes([self.protocol])
        return None

    def on_reset(self) -> None:
        # HID 1.11 7.2.6: devices default to the report protocol.
        self.protocol = REPORT_PROTOCOL
        self.idle = self.default_idle


class _Keyboard(_HidDevice):
    """Boot keyboard behavior: 8-byte input reports and a 5-bit LED report."""

    def __init__(self, spec: _Spec, *, interface: int) -> None:
        self.leds = 0
        # HID 1.11 7.2.4: 500 ms (125 x 4 ms) is the keyboard default idle.
        super().__init__(spec, interface=interface, report_size=8, idle=125)

    def output_report(self) -> bytes | None:
        return bytes([self.leds])

    def set_output_report(self, data: bytes) -> bool:
        if len(data) != 1:
            return False
        self.leds = data[0] & 0x1F
        return True

    def press(self, *usages: int, modifiers: int = 0) -> bool:
        """Queue a report holding ``usages``; False if the device is not live."""
        report = keyboard_report(usages, modifiers)
        with self.locked():
            self.report = report
            return self._queue(self.REPORT_ENDPOINT, report)

    def release(self) -> bool:
        """Queue an all-zero report; False if the device is not live."""
        with self.locked():
            self.report = bytes(8)
            return self._queue(self.REPORT_ENDPOINT, self.report)


class HidKeyboard(_Keyboard):
    """Model of a low-speed USB 1.1 boot keyboard; not hardware evidence.

    Interrupt IN 0x81 (8 bytes, bInterval 10) carries 8-byte reports.
    SET_REPORT(Output) updates :attr:`leds`.
    """

    def __init__(self) -> None:
        report = KEYBOARD_REPORT_DESCRIPTOR
        super().__init__(
            _Spec(
                speed=Speed.LOW,
                product_id=0x0001,
                product="Simulated boot keyboard",
                bcd_usb=0x0110,
                max_packet0=8,
                configuration=_config(
                    _hid_interface(0, protocol=1, report=report, packet=8, interval=10),
                    count=1,
                    attributes=0xA0,
                ),
                class_descriptors=_hid_descriptors(0, report),
            ),
            interface=0,
        )


class HidMouse(_HidDevice):
    """Model of a full-speed-only boot mouse with a wheel; not hardware evidence.

    Interrupt IN 0x81 (4 bytes, bInterval 10) carries ``buttons, X, Y,
    wheel`` reports in both protocols; a boot host reads the first three.
    """

    def __init__(self) -> None:
        report = MOUSE_REPORT_DESCRIPTOR
        super().__init__(
            _Spec(
                speed=Speed.FULL,
                product_id=0x0002,
                product="Simulated boot mouse",
                configuration=_config(
                    _hid_interface(0, protocol=2, report=report, packet=4, interval=10),
                    count=1,
                    attributes=0xA0,
                ),
                class_descriptors=_hid_descriptors(0, report),
            ),
            interface=0,
            report_size=4,
            idle=0,
        )

    def move(self, dx: int, dy: int, buttons: int = 0, wheel: int = 0) -> bool:
        """Queue one movement report; False if the device is not live."""
        for name, value in (("dx", dx), ("dy", dy), ("wheel", wheel)):
            if not -127 <= value <= 127:
                raise ValueError(f"{name} out of range: {value}")
        if not 0 <= buttons <= 0x07:
            raise ValueError(f"buttons out of range: {buttons}")
        report = struct.pack("<Bbbb", buttons, dx, dy, wheel)
        with self.locked():
            self.report = report
            return self._queue(self.REPORT_ENDPOINT, report)


# --- Mass storage (Bulk-Only Transport, SCSI transparent) ------------------

BLOCK_SIZE = 512
CBW_SIGNATURE = b"USBC"
CSW_SIGNATURE = b"USBS"
CBW_SIZE = 31
CSW_SIZE = 13

STATUS_GOOD = 0
STATUS_FAILED = 1
STATUS_PHASE_ERROR = 2

TEST_UNIT_READY = 0x00
REQUEST_SENSE = 0x03
INQUIRY = 0x12
MODE_SENSE_6 = 0x1A
PREVENT_ALLOW_MEDIUM_REMOVAL = 0x1E
READ_CAPACITY_10 = 0x25
READ_10 = 0x28
WRITE_10 = 0x2A

Sense = tuple[int, int, int]
"""(sense key, additional sense code, additional sense code qualifier)."""

NO_SENSE: Sense = (0x00, 0x00, 0x00)
INVALID_COMMAND_OPCODE: Sense = (0x05, 0x20, 0x00)
LBA_OUT_OF_RANGE: Sense = (0x05, 0x21, 0x00)
INVALID_FIELD_IN_CDB: Sense = (0x05, 0x24, 0x00)
LUN_NOT_SUPPORTED: Sense = (0x05, 0x25, 0x00)
WRITE_ERROR: Sense = (0x03, 0x0C, 0x00)

GET_MAX_LUN = 0xFE
BULK_ONLY_RESET = 0xFF

INQUIRY_DATA = (
    bytes([0x00, 0x80, 0x02, 0x02, 31, 0, 0, 0])  # disk, removable, SCSI-2
    + b"YOKE    "
    + b"SIMULATED DISK  "
    + b"1.0 "
)


class _Phase(Enum):
    COMMAND = "command"
    DATA_OUT = "data-out"
    STATUS_AFTER_HALT = "status-after-halt"


@dataclass(frozen=True)
class _Outcome:
    """A SCSI command's result before BOT reconciles it with the CBW."""

    status: int
    data: bytes = b""
    write: tuple[int, int] | None = None
    """(byte offset, byte count) the command wants to receive."""


@dataclass
class _Command:
    """The command in flight between its CBW and its CSW."""

    tag: int
    expected: int
    status: int
    write: tuple[int, int] | None = None
    received: int = 0
    data: bytearray = field(default_factory=bytearray)

    def csw(self, residue: int) -> bytes:
        return CSW_SIGNATURE + struct.pack("<IIB", self.tag, residue, self.status)


class MassStorage(_SimDevice):
    """Model of a high-speed BOT/SCSI flash drive; not hardware evidence.

    Bulk IN 0x81 and bulk OUT 0x02 (512 bytes; 64 in the other-speed
    configuration). One LUN of ``blocks`` 512-byte blocks is backed by
    :attr:`disk`. Failures follow BOT 1.0 section 6.7: a host expecting
    data IN sees bulk IN stall and reads the CSW after CLEAR_FEATURE; host
    data OUT is accepted and discarded. An invalid CBW stalls both pipes
    until a Bulk-Only Mass Storage Reset (section 6.6.1), and the halt
    survives CLEAR_FEATURE until then.

    :attr:`bot_resets` counts Bulk-Only Mass Storage Resets; the inherited
    :attr:`resets` keeps counting port resets.

    :attr:`refuse_data_out` makes the next that many data-out phases fail
    (BOT 1.0 section 6.7.3, a device that cannot take the host's data): the
    first data packet is refused by STALL, the CSW reports a failed command
    with nothing processed, and the halt clears with CLEAR_FEATURE.
    """

    BULK_IN: ClassVar[int] = 0x81
    BULK_OUT: ClassVar[int] = 0x02

    def __init__(self, blocks: int = 2048) -> None:
        if not 1 <= blocks <= 1 << 32:
            raise ValueError(f"block count out of range: {blocks}")
        self.blocks = blocks
        self.disk = bytearray(blocks * BLOCK_SIZE)
        self.commands: list[int] = []
        self.bot_resets = 0
        self.sense: Sense = NO_SENSE
        self.prevent_removal = False
        self._phase = _Phase.COMMAND
        self._command: _Command | None = None
        self._pending_csw = b""
        self._invalid = False
        self.refuse_data_out = 0
        super().__init__(
            _Spec(
                speed=Speed.HIGH,
                product_id=0x0003,
                product="Simulated mass storage",
                configuration=self._configuration(512),
                other_speed=self._configuration(64, other_speed=True),
            )
        )

    @staticmethod
    def _configuration(size: int, *, other_speed: bool = False) -> bytes:
        endpoints = _bulk_pair(MassStorage.BULK_IN, MassStorage.BULK_OUT, size)
        body = _interface(
            0, ch9.CLASS_MASS_STORAGE, endpoints, subclass=0x06, protocol=0x50
        )
        return _config(body, count=1, other_speed=other_speed)

    # --- control and lifecycle ---------------------------------------------

    def class_request(self, setup: Setup, data: bytes) -> bytes | None:
        if (
            setup.recipient != ch9.RECIP_INTERFACE
            or setup.index != 0
            or setup.value != 0
        ):
            return None
        request = (setup.request_type, setup.request)
        if request == (CLASS_IN_INTERFACE, GET_MAX_LUN):
            return b"\x00"
        if request == (CLASS_OUT_INTERFACE, BULK_ONLY_RESET) and setup.length == 0:
            # BOT 5.3.4: the reset keeps endpoint halts and data toggles.
            self.bot_resets += 1
            self._restart()
            pipe = self.pipes.get(self.BULK_IN)
            if isinstance(pipe, InPipe):
                pipe.packets.clear()
            self.defer(lambda: self.resume_out(self.BULK_OUT))
            return b""
        return None

    def _restart(self) -> None:
        self._phase = _Phase.COMMAND
        self._command = None
        self._pending_csw = b""
        self._invalid = False

    def on_configuration(self, value: int) -> None:
        self._restart()

    def on_reset(self) -> None:
        self._restart()
        self.sense = NO_SENSE

    def on_clear_halt(self, address: int) -> None:
        if address != self.BULK_IN:
            return
        if self._invalid:
            self.halt_in(self.BULK_IN)
        elif self._phase is _Phase.STATUS_AFTER_HALT:
            csw, self._pending_csw = self._pending_csw, b""
            self._phase = _Phase.COMMAND
            self._queue(self.BULK_IN, csw)
            self.defer(lambda: self.resume_out(self.BULK_OUT))

    # --- Bulk-Only Transport -----------------------------------------------

    def on_out(self, address: int, packet: bytes) -> Verdict:
        if self._invalid:
            return Verdict.STALL
        if self._phase is _Phase.STATUS_AFTER_HALT:
            return Verdict.NAK  # no new CBW until the CSW has been sent
        if self._phase is _Phase.DATA_OUT:
            if self.refuse_data_out:
                self.refuse_data_out -= 1
                command = self._command
                assert command is not None
                command.status = STATUS_FAILED
                self.sense = WRITE_ERROR
                self._send_csw(command, command.expected - command.received)
                return Verdict.STALL
            self._receive(packet, self.pipes[address].packet_size)
        else:
            self._accept_cbw(packet)
        return Verdict.ACCEPT

    def _accept_cbw(self, packet: bytes) -> None:
        if len(packet) != CBW_SIZE or packet[:4] != CBW_SIGNATURE:
            self._invalid = True
            self.halt_in(self.BULK_IN)
            return
        command = _Command(
            tag=int.from_bytes(packet[4:8], "little"),
            expected=int.from_bytes(packet[8:12], "little"),
            status=STATUS_GOOD,
        )
        data_in = bool(packet[12] & 0x80)
        cb = bytes(packet[15:31])
        self.commands.append(cb[0])
        outcome = self._scsi(cb, lun=packet[13], cb_length=packet[14])
        command.status = outcome.status
        self._dispatch(command, outcome, data_in)

    def _dispatch(self, command: _Command, outcome: _Outcome, data_in: bool) -> None:
        """Reconcile host and device intent (BOT 1.0 section 6.7 cases)."""
        expected = command.expected
        wants_out = outcome.write is not None and outcome.write[1] > 0
        if expected == 0:
            if outcome.data or wants_out:
                command.status = STATUS_PHASE_ERROR
            self._send_csw(command, 0)
        elif data_in:
            sent = b"" if wants_out else outcome.data[:expected]
            if wants_out or len(outcome.data) > expected:
                command.status = STATUS_PHASE_ERROR
            if sent:
                # A short final packet (or a ZLP) ends the host's transfer.
                self._queue(self.BULK_IN, sent, zlp=len(sent) < expected)
                self._send_csw(command, expected - len(sent))
            else:
                self._pending_csw = command.csw(expected)
                self._phase = _Phase.STATUS_AFTER_HALT
                self.halt_in(self.BULK_IN)
        else:
            too_much = outcome.write is not None and outcome.write[1] > expected
            if outcome.data or too_much:
                command.status = STATUS_PHASE_ERROR
            else:
                command.write = outcome.write
            self._command = command
            self._phase = _Phase.DATA_OUT

    def _receive(self, packet: bytes, packet_size: int) -> None:
        command = self._command
        assert command is not None
        command.received += len(packet)
        if command.write is not None:
            command.data += packet
        if command.received < command.expected and len(packet) == packet_size:
            return
        processed = 0
        if command.write is not None:
            offset, size = command.write
            if len(command.data) >= size:
                self.disk[offset : offset + size] = command.data[:size]
                processed = size
            else:
                command.status = STATUS_PHASE_ERROR
        self._send_csw(command, command.expected - processed)

    def _send_csw(self, command: _Command, residue: int) -> None:
        self._phase = _Phase.COMMAND
        self._command = None
        self._queue(self.BULK_IN, command.csw(residue))

    # --- SCSI --------------------------------------------------------------

    def _fail(self, sense: Sense) -> _Outcome:
        self.sense = sense
        return _Outcome(STATUS_FAILED)

    def _scsi(self, cb: bytes, *, lun: int, cb_length: int) -> _Outcome:
        opcode = cb[0]
        if opcode != REQUEST_SENSE:
            self.sense = NO_SENSE
        if lun != 0:
            return self._fail(LUN_NOT_SUPPORTED)
        if not 1 <= cb_length <= 16:
            return self._fail(INVALID_FIELD_IN_CDB)
        if opcode == TEST_UNIT_READY:
            return _Outcome(STATUS_GOOD)
        if opcode == REQUEST_SENSE:
            key, asc, ascq = self.sense
            self.sense = NO_SENSE
            sense = bytearray(18)
            sense[0], sense[2], sense[7] = 0x70, key, 10  # current, fixed format
            sense[12], sense[13] = asc, ascq
            return _Outcome(STATUS_GOOD, bytes(sense[: cb[4]]))
        if opcode == INQUIRY:
            if cb[1] & 0x01:  # EVPD: no vital product data pages
                return self._fail(INVALID_FIELD_IN_CDB)
            return _Outcome(STATUS_GOOD, INQUIRY_DATA[: int.from_bytes(cb[3:5], "big")])
        if opcode == MODE_SENSE_6:
            # Header only: no pages, medium type 0, not write protected.
            return _Outcome(STATUS_GOOD, bytes([3, 0, 0, 0])[: cb[4]])
        if opcode == PREVENT_ALLOW_MEDIUM_REMOVAL:
            self.prevent_removal = bool(cb[4] & 0x01)
            return _Outcome(STATUS_GOOD)
        if opcode == READ_CAPACITY_10:
            return _Outcome(
                STATUS_GOOD, struct.pack(">II", self.blocks - 1, BLOCK_SIZE)
            )
        if opcode in (READ_10, WRITE_10):
            lba = int.from_bytes(cb[2:6], "big")
            count = int.from_bytes(cb[7:9], "big")
            if lba + count > self.blocks:
                return self._fail(LBA_OUT_OF_RANGE)
            offset, size = lba * BLOCK_SIZE, count * BLOCK_SIZE
            if opcode == READ_10:
                return _Outcome(STATUS_GOOD, bytes(self.disk[offset : offset + size]))
            return _Outcome(STATUS_GOOD, write=(offset, size))
        return self._fail(INVALID_COMMAND_OPCODE)


# --- CDC ACM ---------------------------------------------------------------

SET_LINE_CODING = 0x20
GET_LINE_CODING = 0x21
SET_CONTROL_LINE_STATE = 0x22
SERIAL_STATE = 0x20
DEFAULT_LINE_CODING = struct.pack("<IBBB", 115200, 0, 0, 8)
"""115200 baud, 1 stop bit, no parity, 8 data bits."""

CDC_FUNCTIONAL = (
    bytes([5, 0x24, 0x00, 0x10, 0x01])  # Header, bcdCDC 1.10
    + bytes([5, 0x24, 0x01, 0x00, 1])  # Call Management, data interface 1
    + bytes([4, 0x24, 0x02, 0x02])  # ACM: line coding and serial state
    + bytes([5, 0x24, 0x06, 0, 1])  # Union: control 0, subordinate 1
)


class CdcAcm(_SimDevice):
    """Model of a full-speed-only CDC ACM serial loopback; not hardware evidence.

    Interface 0 (communications) has notification endpoint 0x83; interface 1
    (data) has bulk IN 0x81 and bulk OUT 0x02. Every OUT packet, a ZLP
    included, is echoed as the identical IN packet. DTR is wired to DCD and
    DSR: each DTR change queues a SERIAL_STATE notification reporting it.
    """

    NOTIFY: ClassVar[int] = 0x83
    DATA_IN: ClassVar[int] = 0x81
    DATA_OUT: ClassVar[int] = 0x02

    def __init__(self) -> None:
        self.line_coding = DEFAULT_LINE_CODING
        self.control_lines = 0
        self.received = bytearray()
        control = _interface(
            0,
            0x02,
            [build.endpoint(self.NOTIFY, INTERRUPT, 10, 16)],
            subclass=0x02,
            protocol=0x01,
            extra=CDC_FUNCTIONAL,
        )
        data = _interface(
            1, ch9.CLASS_CDC_DATA, _bulk_pair(self.DATA_IN, self.DATA_OUT, 64)
        )
        super().__init__(
            _Spec(
                speed=Speed.FULL,
                product_id=0x0004,
                product="Simulated CDC ACM serial port",
                device_class=(ch9.CLASS_MISC, 0x02, 0x01),  # IAD device
                configuration=_config(
                    build.iad(0, 2, 0x02, 0x02, 0x01), control, data, count=2
                ),
            )
        )

    def class_request(self, setup: Setup, data: bytes) -> bytes | None:
        if setup.recipient != ch9.RECIP_INTERFACE or setup.index != 0:
            return None
        request = (setup.request_type, setup.request)
        if request == (CLASS_OUT_INTERFACE, SET_LINE_CODING):
            if len(data) != 7:
                return None
            rate = int.from_bytes(data[:4], "little")
            stop, parity, bits = data[4], data[5], data[6]
            if rate == 0 or stop > 2 or parity > 4 or bits not in (5, 6, 7, 8, 16):
                return None
            self.line_coding = bytes(data)
            return b""
        if request == (CLASS_IN_INTERFACE, GET_LINE_CODING):
            return self.line_coding
        if request == (CLASS_OUT_INTERFACE, SET_CONTROL_LINE_STATE):
            previous, self.control_lines = self.control_lines, setup.value
            if (previous ^ setup.value) & 0x01:
                state = 0x03 if setup.value & 0x01 else 0x00  # DCD | DSR
                self._queue(self.NOTIFY, serial_state(0, state))
            return b""
        return None

    def on_reset(self) -> None:
        self.line_coding = DEFAULT_LINE_CODING
        self.control_lines = 0

    def on_out(self, address: int, packet: bytes) -> Verdict:
        self.received += packet
        self._queue(self.DATA_IN, packet)
        return Verdict.ACCEPT


def serial_state(interface: int, state: int) -> bytes:
    """A 10-byte CDC PSTN SERIAL_STATE notification."""
    return struct.pack(
        "<BBHHHH", CLASS_IN_INTERFACE, SERIAL_STATE, 0, interface, 2, state
    )


# --- vendor loopbacks --------------------------------------------------------


class _TransferEcho:
    """Collect one OUT transfer, ended by a short packet or ZLP."""

    def __init__(self) -> None:
        self.pending = bytearray()

    def feed(self, packet: bytes, packet_size: int) -> bytes | None:
        self.pending += packet
        if len(packet) == packet_size:
            return None
        transfer = bytes(self.pending)
        self.pending.clear()
        return transfer


VERSION = b"YOKE-SIM-1.0"
GET_VERSION = 0x01
SET_SCRATCH = 0x02
GET_SCRATCH = 0x03
PING = 0x04
SCRATCH_SIZE = 64


class CompositeDevice(_Keyboard):
    """Model of a high-speed keyboard plus vendor loopback; not hardware evidence.

    bcdUSB 0x0201 with a BOS advertising LPM. Interface 0 is a boot keyboard
    on interrupt IN 0x81; interface 1 is a vendor bulk loopback (IN 0x82,
    OUT 0x03) that echoes each OUT transfer, ending it with a ZLP when its
    length is a non-zero multiple of 512.
    """

    ECHO_IN: ClassVar[int] = 0x82
    ECHO_OUT: ClassVar[int] = 0x03

    def __init__(self) -> None:
        self.pings = 0
        self.scratch = b""
        self.received = bytearray()
        self._echo = _TransferEcho()
        super().__init__(
            _Spec(
                speed=Speed.HIGH,
                product_id=0x0005,
                product="Simulated composite device",
                bcd_usb=0x0201,
                configuration=self._configuration(full_speed=False),
                other_speed=self._configuration(full_speed=True),
                bos=build.bos(build.usb2_extension(lpm=True)),
                strings={4: "Keyboard interface", 5: "Vendor loopback interface"},
                class_descriptors=_hid_descriptors(0, KEYBOARD_REPORT_DESCRIPTOR),
            ),
            interface=0,
        )

    @staticmethod
    def _configuration(*, full_speed: bool) -> bytes:
        keyboard = _hid_interface(
            0,
            protocol=1,
            report=KEYBOARD_REPORT_DESCRIPTOR,
            packet=8,
            interval=1 if full_speed else 4,  # 1 ms either way
            string=4,
        )
        size = 64 if full_speed else 512
        vendor = _interface(
            1,
            ch9.CLASS_VENDOR,
            _bulk_pair(CompositeDevice.ECHO_IN, CompositeDevice.ECHO_OUT, size),
            string=5,
        )
        return _config(keyboard, vendor, count=2, other_speed=full_speed)

    def vendor_request(self, setup: Setup, data: bytes) -> bytes | None:
        request = (setup.request_type, setup.request)
        if request == (VENDOR_IN_DEVICE, GET_VERSION):
            return VERSION
        if request == (VENDOR_OUT_DEVICE, SET_SCRATCH):
            if len(data) > SCRATCH_SIZE:
                return None
            self.scratch = bytes(data)
            return b""
        if request == (VENDOR_IN_DEVICE, GET_SCRATCH):
            return self.scratch
        if request == (VENDOR_OUT_DEVICE, PING) and setup.length == 0:
            self.pings += 1
            return b""
        return None

    def on_configuration(self, value: int) -> None:
        self._echo = _TransferEcho()

    def on_reset(self) -> None:
        super().on_reset()
        self._echo = _TransferEcho()

    def on_out(self, address: int, packet: bytes) -> Verdict:
        self.received += packet
        transfer = self._echo.feed(packet, self.pipes[address].packet_size)
        if transfer is not None:
            # packetize() adds the ZLP only for a non-zero multiple.
            self._queue(self.ECHO_IN, transfer, zlp=True)
        return Verdict.ACCEPT


class Loopback(_SimDevice):
    """Model of a vendor bulk loopback for benchmarks; not hardware evidence.

    Bulk IN 0x81 and bulk OUT 0x02. Each OUT transfer (ended by a short
    packet or ZLP) is echoed on IN, with a ZLP when its length is a non-zero
    multiple of the packet size. ``packet_size`` must be valid for bulk at
    the chosen speed: 8, 16, 32 or 64 at full speed and 512 at high speed
    (USB 2.0 5.8.3). Full-speed instances are full-speed only.
    """

    BULK_IN: ClassVar[int] = 0x81
    BULK_OUT: ClassVar[int] = 0x02
    product_id: ClassVar[int] = 0x0006
    product_name: ClassVar[str] = "Simulated bulk loopback"

    def __init__(
        self, speed: Speed = Speed.HIGH, packet_size: int | None = None
    ) -> None:
        if speed not in (Speed.FULL, Speed.HIGH):
            raise ValueError(f"bulk loopback needs full or high speed, not {speed!r}")
        high = speed == Speed.HIGH
        size = packet_size if packet_size is not None else 512 if high else 64
        if size not in ((512,) if high else (8, 16, 32, 64)):
            raise ValueError(f"bulk wMaxPacketSize {size} is invalid at {speed.name}")
        self.packet_size = size
        self.received = bytearray()
        self._echo = _TransferEcho()
        super().__init__(
            _Spec(
                speed=speed,
                product_id=self.product_id,
                product=self.product_name,
                configuration=self._configuration(size),
                other_speed=self._configuration(64, other_speed=True) if high else None,
            )
        )

    @classmethod
    def _configuration(cls, size: int, *, other_speed: bool = False) -> bytes:
        body = _interface(
            0, ch9.CLASS_VENDOR, _bulk_pair(cls.BULK_IN, cls.BULK_OUT, size)
        )
        return _config(body, count=1, other_speed=other_speed)

    def emit(self, data: bytes, zlp: bool = False) -> bool:
        """Queue IN data directly; False if the device is not live."""
        with self.locked():
            return self._queue(self.BULK_IN, data, zlp=zlp)

    def on_configuration(self, value: int) -> None:
        self._echo = _TransferEcho()

    def on_reset(self) -> None:
        self._echo = _TransferEcho()

    def on_out(self, address: int, packet: bytes) -> Verdict:
        self.received += packet
        transfer = self._echo.feed(packet, self.pipes[address].packet_size)
        if transfer is not None:
            self._queue(self.BULK_IN, transfer, zlp=True)
        return Verdict.ACCEPT


class FullSpeedOnly(Loopback):
    """Model of a full-speed-only bulk loopback; not hardware evidence.

    bcdUSB 0x0200 without a device qualifier, so GET_DESCRIPTOR
    (DEVICE_QUALIFIER) STALLs as USB 2.0 9.6.2 requires.
    """

    product_id: ClassVar[int] = 0x0007
    product_name: ClassVar[str] = "Simulated full-speed-only loopback"

    def __init__(self) -> None:
        super().__init__(Speed.FULL)


# --- fault injection ---------------------------------------------------------

ALWAYS_STALL = 0x10
ARM_STALL = 0x11
GET_CLEAR_HALTS = 0x12
RECOVERED = b"RECOVERED"


class StallingDevice(_SimDevice):
    """Model of a high-speed device with injectable stalls; not hardware evidence.

    Bulk IN 0x81, bulk OUT 0x02 (512) and interrupt IN 0x83 (16, bInterval
    4). Vendor request 0x11 (wValue endpoint, wIndex byte count) arms a
    stall: an IN endpoint first queues wIndex bytes rounded down to whole
    packets, then halts once they are read; an OUT endpoint STALLs its next
    packet. Request 0x10 always STALLs; 0x12 returns the CLEAR_FEATURE count.
    Clearing 0x81 queues :data:`RECOVERED` as a short packet.
    """

    BULK_IN: ClassVar[int] = 0x81
    BULK_OUT: ClassVar[int] = 0x02
    INTERRUPT_IN: ClassVar[int] = 0x83

    def __init__(self) -> None:
        self.received = bytearray()
        self.clear_halts = 0
        self._armed_out: set[int] = set()
        super().__init__(
            _Spec(
                speed=Speed.HIGH,
                product_id=0x0008,
                product="Simulated stalling device",
                configuration=self._configuration(512, 16),
                other_speed=self._configuration(64, 16, other_speed=True),
            )
        )

    @classmethod
    def _configuration(
        cls, bulk: int, interrupt: int, *, other_speed: bool = False
    ) -> bytes:
        endpoints = [
            *_bulk_pair(cls.BULK_IN, cls.BULK_OUT, bulk),
            build.endpoint(
                cls.INTERRUPT_IN, INTERRUPT, interrupt, 1 if other_speed else 4
            ),
        ]
        body = _interface(0, ch9.CLASS_VENDOR, endpoints)
        return _config(body, count=1, other_speed=other_speed)

    def emit(self, address: int, data: bytes, zlp: bool = False) -> bool:
        """Queue IN data on 0x81 or 0x83; False if the device is not live."""
        if address not in (self.BULK_IN, self.INTERRUPT_IN):
            raise ValueError(f"{address:#04x} is not an IN endpoint of this model")
        with self.locked():
            return self._queue(address, data, zlp=zlp)

    def vendor_request(self, setup: Setup, data: bytes) -> bytes | None:
        request = (setup.request_type, setup.request)
        if request == (VENDOR_OUT_DEVICE, ARM_STALL) and setup.length == 0:
            return b"" if self._arm(setup.value, setup.index) else None
        if request == (VENDOR_IN_DEVICE, GET_CLEAR_HALTS):
            return bytes([min(self.clear_halts, 0xFF)])
        return None  # ALWAYS_STALL and anything unknown

    def _arm(self, address: int, partial: int) -> bool:
        pipe = self.pipes.get(address)
        if pipe is None:
            return False
        if isinstance(pipe, InPipe):
            whole = partial - partial % pipe.packet_size
            if whole:
                pipe.send(_pattern(whole))
            self.halt_in(address)
        else:
            self._armed_out.add(address)
        return True

    def on_out(self, address: int, packet: bytes) -> Verdict:
        if address in self._armed_out:
            self._armed_out.discard(address)
            return Verdict.STALL
        self.received += packet
        return Verdict.ACCEPT

    def on_clear_halt(self, address: int) -> None:
        self.clear_halts += 1
        if address == self.BULK_IN:
            self._queue(self.BULK_IN, RECOVERED)

    def on_configuration(self, value: int) -> None:
        self._armed_out.clear()

    def on_reset(self) -> None:
        self._armed_out.clear()


class DisconnectingDevice(_SimDevice):
    """Model of a device that unplugs mid-transfer; not hardware evidence.

    Bulk IN 0x81 and bulk OUT 0x02 (512). After :meth:`arm_disconnect`, once
    the armed number of OUT bytes has been accepted the model NAKs further
    OUT packets and unplugs as soon as the model lock is released, so the
    in-flight OUT URB is still pending and completes with ``-ESHUTDOWN``.
    """

    BULK_IN: ClassVar[int] = 0x81
    BULK_OUT: ClassVar[int] = 0x02

    def __init__(self) -> None:
        self.received = bytearray()
        self._remaining: int | None = None
        self._disconnecting = False
        super().__init__(
            _Spec(
                speed=Speed.HIGH,
                product_id=0x0009,
                product="Simulated disconnecting device",
                configuration=self._configuration(512),
                other_speed=self._configuration(64, other_speed=True),
            )
        )

    @classmethod
    def _configuration(cls, size: int, *, other_speed: bool = False) -> bytes:
        body = _interface(
            0, ch9.CLASS_VENDOR, _bulk_pair(cls.BULK_IN, cls.BULK_OUT, size)
        )
        return _config(body, count=1, other_speed=other_speed)

    def emit(self, data: bytes) -> bool:
        """Queue IN data; False if the device is not live."""
        with self.locked():
            return self._queue(self.BULK_IN, data)

    def arm_disconnect(self, after_out_bytes: int) -> None:
        """Unplug once ``after_out_bytes`` more OUT bytes are accepted."""
        if after_out_bytes < 1:
            raise ValueError("after_out_bytes must be at least 1")
        with self.locked():
            self._remaining = after_out_bytes

    def on_out(self, address: int, packet: bytes) -> Verdict:
        if self._disconnecting:
            return Verdict.NAK
        self.received += packet
        if self._remaining is not None:
            self._remaining -= len(packet)
            if self._remaining <= 0:
                self._remaining = None
                self._disconnecting = True
                self.defer(self.unplug)
        return Verdict.ACCEPT

    def on_configuration(self, value: int) -> None:
        self._disconnecting = False


# --- rejection fixtures ------------------------------------------------------


class HubDevice(_SimDevice):
    """Rejection fixture: a full-speed hub (device class 9); a model only."""

    def __init__(self) -> None:
        body = _interface(0, ch9.CLASS_HUB, [build.endpoint(0x81, INTERRUPT, 1, 255)])
        super().__init__(
            _Spec(
                speed=Speed.FULL,
                product_id=0x000A,
                product="Simulated hub",
                bcd_usb=0x0110,
                device_class=(ch9.CLASS_HUB, 0, 0),
                configuration=_config(body, count=1, attributes=0xE0),
            )
        )


class IsoAudioDevice(_SimDevice):
    """Rejection fixture: full-speed isochronous OUT streaming; a model only."""

    def __init__(self) -> None:
        idle = _interface(0, 0x01, [], subclass=0x02)
        streaming = _interface(
            0,
            0x01,
            [build.endpoint(0x01, ISO_ADAPTIVE, 192, 1)],
            subclass=0x02,
            alternate=1,
        )
        super().__init__(
            _Spec(
                speed=Speed.FULL,
                product_id=0x000B,
                product="Simulated isochronous audio",
                configuration=_config(idle, streaming, count=1),
            )
        )


class ManyEndpointsDevice(_SimDevice):
    """Rejection fixture: nine bulk IN endpoints exceed the UDC budget."""

    def __init__(self) -> None:
        super().__init__(
            _Spec(
                speed=Speed.HIGH,
                product_id=0x000C,
                product="Simulated many-endpoint device",
                configuration=self._configuration(512),
                other_speed=self._configuration(64, other_speed=True),
            )
        )

    @staticmethod
    def _configuration(size: int, *, other_speed: bool = False) -> bytes:
        endpoints = [build.endpoint(0x80 | n, BULK, size) for n in range(1, 10)]
        body = _interface(0, ch9.CLASS_VENDOR, endpoints)
        return _config(body, count=1, other_speed=other_speed)


SS_ENDPOINT_COMPANION = bytes([6, ch9.DT_SS_ENDPOINT_COMP, 0, 0, 0, 0])
SS_USB_DEVICE_CAPABILITY = struct.pack(
    "<BBBBHBBH", 10, ch9.DT_DEVICE_CAPABILITY, 0x03, 0, 0x000E, 1, 10, 0x07FF
)


class SuperSpeedDevice(_SimDevice):
    """Rejection fixture: a minimal SuperSpeed vendor device; a model only."""

    def __init__(self) -> None:
        endpoints = [
            build.endpoint(0x81, BULK, 1024) + SS_ENDPOINT_COMPANION,
            build.endpoint(0x02, BULK, 1024) + SS_ENDPOINT_COMPANION,
        ]
        super().__init__(
            _Spec(
                speed=Speed.SUPER,
                product_id=0x000D,
                product="Simulated SuperSpeed device",
                bcd_usb=0x0300,
                max_packet0=9,  # 2**9 = 512 bytes at SuperSpeed
                configuration=_config(
                    _interface(0, ch9.CLASS_VENDOR, endpoints), count=1
                ),
                bos=build.bos(build.usb2_extension(lpm=True), SS_USB_DEVICE_CAPABILITY),
            )
        )


ALL_DEVICES: dict[str, Callable[[], DeviceModel]] = {
    "keyboard": HidKeyboard,
    "mouse": HidMouse,
    "mass_storage": MassStorage,
    "cdc_acm": CdcAcm,
    "composite": CompositeDevice,
    "loopback": Loopback,
    "stalling": StallingDevice,
    "disconnecting": DisconnectingDevice,
}
"""Factories for the functional models, keyed by a short name."""
