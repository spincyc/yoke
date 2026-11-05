"""Pair usbmon events into transfers and group BOT transfers into commands.

A transfer is one URB: its submit event ('S') paired with the completion
('C') or submission error ('E') that carries the same URB id on the same
bus. OUT payload is taken from the submit, IN payload from the completion.
Mass-storage Bulk-Only Transport (BOT) transfers are then grouped per device
into commands: a 31-byte CBW, its data phase, and the 13-byte CSW whose tag
matches. Stdlib only, Python >= 3.9.
"""

from __future__ import annotations

import errno
import struct
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

import pcapfmt

CBW_SIGNATURE = 0x43425355  # "USBC"
CSW_SIGNATURE = 0x53425355  # "USBS"
CBW_LENGTH = 31
CSW_LENGTH = 13
READ_10, WRITE_10 = 0x28, 0x2A

UNLINK_STATUSES = (-errno.ENOENT, -errno.ECONNRESET)
GONE_STATUSES = (-errno.ESHUTDOWN, -errno.ENODEV)


@dataclass(frozen=True)
class Transfer:
    urb_id: int
    busnum: int
    devnum: int
    endpoint: int  # endpoint number with 0x80 for IN
    xfer: int  # USB transfer type: 0 control, 1 isochronous, 2 bulk, 3 interrupt
    submit_ns: int
    complete_ns: int
    status: int
    requested: int  # transfer_buffer_length at submit
    actual: int  # actual_length at completion (0 after a submission error)
    data: bytes  # captured OUT (submit) or IN (completion) payload
    setup: bytes = b""  # control setup packet when captured
    outcome: str = "ok"  # ok | error | unlinked | gone | submit-error

    @property
    def is_in(self) -> bool:
        return bool(self.endpoint & 0x80)

    @property
    def device(self) -> tuple[int, int]:
        return (self.busnum, self.devnum)

    @property
    def payload_length(self) -> int:
        """Bytes USB/IP carries: the OUT buffer in CMD_SUBMIT, IN actual in RET."""
        return self.actual if self.is_in else self.requested


@dataclass
class Pairing:
    transfers: list[Transfer] = field(default_factory=list)
    unmatched_submits: int = 0  # no completion in the trace
    orphan_completions: int = 0  # completion without a submit in the trace


def outcome_of(kind: str, status: int) -> str:
    if kind == "E":
        return "submit-error"
    if status == 0:
        return "ok"
    if status in UNLINK_STATUSES:
        return "unlinked"
    if status in GONE_STATUSES:
        return "gone"
    return "error"


def _transfer(submit: pcapfmt.UsbmonEvent, done: pcapfmt.UsbmonEvent) -> Transfer:
    is_in = bool(submit.epnum & 0x80)
    actual = 0 if done.kind == "E" else done.length
    data = done.data if is_in else submit.data
    if is_in:
        data = data[:actual]
    setup = b""
    if (
        submit.xfer_type == pcapfmt.URB_CONTROL
        and submit.flag_setup == pcapfmt.FLAG_PRESENT
    ):
        setup = submit.setup
    return Transfer(
        urb_id=submit.urb_id,
        busnum=submit.busnum,
        devnum=submit.devnum,
        endpoint=submit.epnum,
        xfer=submit.usb_xfer,
        submit_ns=submit.ts_ns,
        complete_ns=max(done.ts_ns, submit.ts_ns),
        status=done.status,
        requested=submit.length,
        actual=actual,
        data=data,
        setup=setup,
        outcome=outcome_of(done.kind, done.status),
    )


def pair_events(events: Iterable[pcapfmt.UsbmonEvent]) -> Pairing:
    """Pair S with C/E by (bus, URB id); ids are reused once a URB completes."""
    pending: dict[tuple[int, int], pcapfmt.UsbmonEvent] = {}
    result = Pairing()
    for event in events:
        key = (event.busnum, event.urb_id)
        if event.kind == "S":
            if key in pending:
                result.unmatched_submits += 1
            pending[key] = event
            continue
        submit = pending.pop(key, None)
        if submit is None:
            result.orphan_completions += 1
            continue
        result.transfers.append(_transfer(submit, event))
    result.unmatched_submits += len(pending)
    result.transfers.sort(key=lambda t: (t.submit_ns, t.complete_ns))
    return result


@dataclass(frozen=True)
class Cbw:
    tag: int
    data_length: int
    data_in: bool
    lun: int
    cdb: bytes

    @property
    def opcode(self) -> int:
        return self.cdb[0] if self.cdb else -1

    def rw10(self) -> tuple[int, int] | None:
        """(LBA, blocks) of a READ(10)/WRITE(10), else None."""
        if self.opcode not in (READ_10, WRITE_10) or len(self.cdb) < 10:
            return None
        lba = int.from_bytes(self.cdb[2:6], "big")
        blocks = int.from_bytes(self.cdb[7:9], "big")
        return lba, blocks


def pack_cbw(tag: int, data_length: int, data_in: bool, lun: int, cdb: bytes) -> bytes:
    flags = 0x80 if data_in else 0
    padded = cdb.ljust(16, b"\x00")
    return struct.pack(
        "<IIIBBB16s", CBW_SIGNATURE, tag, data_length, flags, lun, len(cdb), padded
    )


def parse_cbw(data: bytes) -> Cbw | None:
    if len(data) != CBW_LENGTH:
        return None
    signature, tag, length, flags, lun, cb_len, cdb = struct.unpack("<IIIBBB16s", data)
    if signature != CBW_SIGNATURE or not 1 <= cb_len <= 16:
        return None
    return Cbw(tag, length, bool(flags & 0x80), lun & 0x0F, cdb[:cb_len])


def pack_csw(tag: int, residue: int, status: int) -> bytes:
    return struct.pack("<IIIB", CSW_SIGNATURE, tag, residue, status)


def parse_csw(data: bytes) -> tuple[int, int, int] | None:
    """(tag, residue, status) of a CSW, else None."""
    if len(data) != CSW_LENGTH:
        return None
    signature, tag, residue, status = struct.unpack("<IIIB", data)
    if signature != CSW_SIGNATURE:
        return None
    return tag, residue, status


@dataclass
class BotCommand:
    device: tuple[int, int]
    cbw: Cbw
    start_ns: int
    end_ns: int = 0
    members: list[int] = field(default_factory=list)  # transfer indices
    data_in: int = 0
    data_out: int = 0
    data_out_lengths: list[int] = field(default_factory=list)
    csw_status: int | None = None  # None: no CSW seen
    residue: int = 0

    @property
    def good(self) -> bool:
        return self.csw_status == 0 and self.residue == 0


def bot_commands(transfers: Sequence[Transfer]) -> list[BotCommand]:
    """Group bulk transfers per device into CBW..CSW commands.

    A bulk OUT transfer that carries a valid CBW opens a command; bulk
    transfers on that device until the CSW with the same tag are its data
    phase. A new CBW before the CSW closes the open command without status.
    """
    open_cmds: dict[tuple[int, int], BotCommand] = {}
    done: list[BotCommand] = []
    for index, transfer in enumerate(transfers):
        if transfer.xfer != pcapfmt.USB_BULK:
            continue
        current = open_cmds.get(transfer.device)
        cbw = None if transfer.is_in else parse_cbw(transfer.data)
        if cbw is not None:
            if current is not None:
                done.append(current)
            cmd = BotCommand(
                transfer.device, cbw, transfer.submit_ns, transfer.complete_ns
            )
            cmd.members.append(index)
            open_cmds[transfer.device] = cmd
            continue
        if current is None:
            continue
        current.members.append(index)
        current.end_ns = max(current.end_ns, transfer.complete_ns)
        csw = parse_csw(transfer.data) if transfer.is_in else None
        if csw is not None and csw[0] == current.cbw.tag:
            current.residue, current.csw_status = csw[1], csw[2]
            done.append(open_cmds.pop(transfer.device))
        elif transfer.is_in:
            current.data_in += transfer.payload_length
        else:
            current.data_out += transfer.payload_length
            current.data_out_lengths.append(transfer.payload_length)
    done.extend(open_cmds.values())
    done.sort(key=lambda c: c.start_ns)
    return done
