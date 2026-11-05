"""Userspace USB/IP client: import one device and multiplex its URBs.

The client speaks USB/IP 1.1.1 directly to a stock ``usbipd`` (ADR 0021),
so the computer-side kernel never binds the device. One reader thread
demultiplexes RET_SUBMIT/RET_UNLINK by sequence number; any thread may
submit. IN payload is received straight into a buffer with
:data:`~yoke.usb.gadget.HEADROOM` reserved bytes, so the gadget backend can
send it without another copy.

Stock-stub requirements honored here (``drivers/usb/usbip/stub_rx.c``): every
command carries the imported ``devid``; URBs target only endpoints of the
current configuration; interrupt URBs carry a positive interval; an unknown
or malformed reply closes the connection.
"""

from __future__ import annotations

import contextlib
import dataclasses
import socket
import struct
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from yoke.usb import usbip
from yoke.usb.ch9 import SETUP_SIZE, Setup
from yoke.usb.gadget import HEADROOM
from yoke.usb.usbip import (
    DIR_IN,
    DIR_OUT,
    HEADER_SIZE,
    NON_ISO_PACKETS,
    DeviceInfo,
    ProtocolError,
    RetSubmit,
    RetUnlink,
)

_SUBMIT = struct.Struct(">IIIIIIiiii8s")
_ZERO_SETUP = b"\0" * SETUP_SIZE


class ByteStream(Protocol):
    def sendall(self, data: bytes | bytearray | memoryview, /) -> None: ...

    def recv_into(self, buffer: bytearray | memoryview, nbytes: int = 0, /) -> int: ...

    def settimeout(self, timeout: float | None, /) -> None: ...

    def shutdown(self, how: int, /) -> None: ...

    def close(self) -> None: ...


def abort_stream(stream: ByteStream) -> None:
    """Shut down before closing: ``close()`` alone neither wakes a thread
    blocked in ``recv`` nor sends FIN while that thread holds the socket."""
    with contextlib.suppress(OSError):
        stream.shutdown(socket.SHUT_RDWR)
    with contextlib.suppress(OSError):
        stream.close()


class UsbipError(Exception):
    """Base class for USB/IP client failures."""


class ImportRejectedError(UsbipError):
    """The exporter refused OP_REQ_IMPORT (absent, busy or unbound device)."""


class ConnectionLostError(UsbipError):
    """The USB/IP connection closed; every pending URB fails with this."""


def recv_exact(stream: ByteStream, view: memoryview) -> None:
    got = 0
    while got < len(view):
        count = stream.recv_into(view[got:], len(view) - got)
        if count == 0:
            raise ConnectionLostError("USB/IP peer closed the connection")
        got += count


def _recv(stream: ByteStream, size: int) -> bytearray:
    buf = bytearray(size)
    recv_exact(stream, memoryview(buf))
    return buf


def import_device(stream: ByteStream, busid: str, *, timeout: float) -> DeviceInfo:
    """OP_REQ_IMPORT handshake; the stream then carries URB traffic."""
    stream.settimeout(timeout)
    stream.sendall(usbip.encode_req_import(busid))
    status = usbip.parse_op_header(
        _recv(stream, usbip.OP_HEADER_SIZE), usbip.OP_REP_IMPORT
    )
    if status != 0:
        raise ImportRejectedError(f"exporter refused busid {busid!r} (status {status})")
    info = DeviceInfo.unpack(_recv(stream, usbip.DEVICE_INFO_SIZE))
    if info.busid != busid:
        raise ProtocolError(f"imported busid {info.busid!r}, requested {busid!r}")
    stream.settimeout(None)
    return info


def list_devices(stream: ByteStream, *, timeout: float) -> list[DeviceInfo]:
    """OP_REQ_DEVLIST; the exporter closes the connection afterwards."""
    stream.settimeout(timeout)
    stream.sendall(usbip.encode_req_devlist())
    status = usbip.parse_op_header(_recv(stream, 8), usbip.OP_REP_DEVLIST)
    if status != 0:
        raise UsbipError(f"device list failed (status {status})")
    (count,) = struct.unpack(">I", _recv(stream, 4))
    if count > 256:
        raise ProtocolError("implausible exported device count")
    devices = []
    for _ in range(count):
        info = DeviceInfo.unpack(_recv(stream, usbip.DEVICE_INFO_SIZE))
        raw = _recv(stream, usbip.INTERFACE_INFO_SIZE * info.num_interfaces)
        interfaces = tuple(
            (raw[i], raw[i + 1], raw[i + 2]) for i in range(0, len(raw), 4)
        )
        devices.append(dataclasses.replace(info, interfaces=interfaces))
    return devices


@dataclass(slots=True)
class UrbResult:
    status: int
    actual_length: int
    buffer: bytearray | None = None
    """IN data at ``buffer[HEADROOM:HEADROOM + actual_length]``."""
    unlinked: bool = False
    """Resolved by CMD_UNLINK without data; never treat it as a transfer."""

    @property
    def data(self) -> bytes:
        if self.buffer is None:
            return b""
        return bytes(self.buffer[HEADROOM : HEADROOM + self.actual_length])


Callback = Callable[["Urb"], None]


def control_data(result: UrbResult) -> bytes | None:
    """A control URB's IN data or b"", None on STALL; raise on other errors."""
    if result.status == -usbip.EPIPE:
        return None
    if result.status != 0:
        raise UsbipError(f"control transfer failed with status {result.status}")
    return result.data


class Urb:
    """One submitted URB (or one unlink request)."""

    __slots__ = (
        "_event",
        "callback",
        "direction",
        "ep",
        "error",
        "length",
        "result",
        "seqnum",
        "target",
    )

    def __init__(
        self,
        seqnum: int,
        ep: int,
        direction: int,
        length: int,
        callback: Callback | None,
        target: Urb | None = None,
    ) -> None:
        self.seqnum = seqnum
        self.ep = ep
        self.direction = direction
        self.length = length
        self.callback = callback
        self.target = target
        self.result: UrbResult | None = None
        self.error: BaseException | None = None
        self._event = threading.Event() if callback is None else None

    @property
    def done(self) -> bool:
        return self.result is not None or self.error is not None

    def _resolve(self) -> None:
        if self._event is not None:
            self._event.set()
        elif self.callback is not None:
            self.callback(self)

    def wait(self, timeout: float | None) -> UrbResult:
        if self._event is None:
            raise RuntimeError("callback URBs cannot be waited on")
        if not self._event.wait(timeout):
            raise TimeoutError(f"URB {self.seqnum} timed out")
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


@dataclass(frozen=True, slots=True)
class Submission:
    ep: int
    direction: int
    length: int
    data: bytes | bytearray | memoryview | None = None
    setup: bytes = _ZERO_SETUP
    flags: int = 0
    interval: int = 0
    callback: Callback | None = None


class UsbipConnection:
    """URB multiplexer for one imported device."""

    def __init__(
        self,
        stream: ByteStream,
        info: DeviceInfo,
        *,
        on_lost: Callable[[BaseException], None] | None = None,
    ) -> None:
        self.info = info
        self._stream = stream
        self._devid = info.devid
        self._on_lost = on_lost
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._send_lock = threading.Lock()
        self._pending: dict[int, Urb] = {}
        self._unlinks: dict[int, Urb] = {}
        # Unlinked seqnums whose RET_SUBMIT may still arrive (stub_tx sends
        # all RET_SUBMITs before all RET_UNLINKs on each pass) -> direction.
        self._tombstones: OrderedDict[int, int] = OrderedDict()
        # Targets whose RET_UNLINK carried the URB's own status (0, -EPIPE):
        # a late RET_SUBMIT may still deliver their data.
        self._acknowledged: dict[int, int] = {}
        self.late_ret_submits = 0
        self._seqnum = 0
        self._lost: BaseException | None = None
        self._reader = threading.Thread(
            target=self._read_loop, name=f"usbip-rx-{info.busid}", daemon=True
        )
        self.submits = 0
        self.sends = 0
        self.bytes_out = 0
        self.bytes_in = 0

    @property
    def lost(self) -> BaseException | None:
        return self._lost

    def start(self) -> None:
        self._reader.start()

    def _next_seqnum(self) -> int:
        self._seqnum = self._seqnum % 0xFFFFFFFF + 1
        return self._seqnum

    def _send(self, parts: Sequence[bytes | bytearray | memoryview]) -> None:
        payload = parts[0] if len(parts) == 1 else b"".join(parts)
        with self._send_lock:
            try:
                self._stream.sendall(payload)
            except OSError as exc:
                self._fail(ConnectionLostError(f"USB/IP send failed: {exc}"))
                raise ConnectionLostError(str(exc)) from exc
            self.sends += 1
            self.bytes_out += len(payload)

    def submit_many(self, submissions: Sequence[Submission]) -> list[Urb]:
        """Submit several URBs with one stream write (ordered)."""
        parts: list[bytes | bytearray | memoryview] = []
        urbs: list[Urb] = []
        with self._lock:
            if self._lost is not None:
                raise ConnectionLostError(str(self._lost))
            for item in submissions:
                if item.direction == DIR_OUT:
                    size = 0 if item.data is None else len(item.data)
                    if size != item.length:
                        raise ValueError("OUT data length must equal length")
                seqnum = self._next_seqnum()
                urb = Urb(seqnum, item.ep, item.direction, item.length, item.callback)
                self._pending[seqnum] = urb
                urbs.append(urb)
                parts.append(
                    _SUBMIT.pack(
                        usbip.CMD_SUBMIT,
                        seqnum,
                        self._devid,
                        item.direction,
                        item.ep,
                        item.flags,
                        item.length,
                        0,
                        NON_ISO_PACKETS - (1 << 32),
                        item.interval,
                        item.setup,
                    )
                )
                if item.direction == DIR_OUT and item.length:
                    assert item.data is not None
                    parts.append(item.data)
            self.submits += len(urbs)
        self._send(parts)
        return urbs

    def submit(
        self,
        ep: int,
        direction: int,
        length: int,
        *,
        data: bytes | bytearray | memoryview | None = None,
        setup: bytes = _ZERO_SETUP,
        flags: int | None = None,
        interval: int = 0,
        callback: Callback | None = None,
    ) -> Urb:
        if flags is None:
            flags = usbip.submit_flags(direction)
        return self.submit_many(
            [Submission(ep, direction, length, data, setup, flags, interval, callback)]
        )[0]

    def unlink(self, urb: Urb, callback: Callback | None = None) -> Urb:
        """Request cancellation; the handle resolves with RET_UNLINK's status."""
        with self._lock:
            if self._lost is not None:
                raise ConnectionLostError(str(self._lost))
            seqnum = self._next_seqnum()
            handle = Urb(seqnum, urb.ep, urb.direction, 0, callback, target=urb)
            self._unlinks[seqnum] = handle
        self._send([usbip.CmdUnlink(seqnum, self._devid, urb.seqnum).pack()])
        return handle

    def cancel(
        self, urbs: Sequence[Urb], *, timeout: float, grace: float = 0.2
    ) -> None:
        """Unlink ``urbs`` and return once every one is resolved.

        RET_UNLINK with -ECONNRESET/-ENOENT resolves its target. RET_UNLINK
        with the URB's own status (0 or -EPIPE) is ambiguous: the URB finished
        and its RET_SUBMIT may still follow, so it gets ``grace`` seconds to
        deliver data before it is abandoned. Raises TimeoutError if RET_UNLINK
        replies do not arrive within ``timeout``.
        """
        deadline = time.monotonic() + timeout
        handles = []
        for urb in urbs:
            if not urb.done:
                try:
                    handles.append(self.unlink(urb))
                except ConnectionLostError:
                    break  # _fail() resolves every pending URB
        try:
            for handle in handles:
                handle.wait(max(0.0, deadline - time.monotonic()))
        except ConnectionLostError:
            return  # _fail() resolved every pending URB
        grace_end = min(deadline, time.monotonic() + grace)
        with self._cond:
            while not all(u.done for u in urbs):
                remaining = grace_end - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            abandoned = []
            for urb in urbs:
                if not urb.done and self._pending.pop(urb.seqnum, None) is not None:
                    status = self._acknowledged.pop(urb.seqnum, -usbip.ECONNRESET)
                    self._tombstone(urb.seqnum, urb.direction)
                    urb.result = UrbResult(status, 0, None, unlinked=True)
                    abandoned.append(urb)
        for urb in abandoned:
            urb._resolve()

    def _tombstone(self, seqnum: int, direction: int) -> None:
        self._tombstones[seqnum] = direction
        while len(self._tombstones) > 4096:
            self._tombstones.popitem(last=False)

    def control(
        self, setup: Setup, data: bytes = b"", *, timeout: float
    ) -> bytes | None:
        """Run one control transfer; return IN data or b"", None on STALL.

        A transfer error other than STALL raises :class:`UsbipError`. On
        timeout the URB is unlinked and :class:`TimeoutError` propagates.
        """
        urb = self.submit_control(setup, data)
        try:
            result = urb.wait(timeout)
        except TimeoutError:
            with contextlib.suppress(UsbipError, TimeoutError):
                self.cancel([urb], timeout=timeout)
            raise
        return control_data(result)

    def submit_control(
        self, setup: Setup, data: bytes = b"", callback: Callback | None = None
    ) -> Urb:
        """Submit one control URB; :func:`control_data` interprets its result."""
        # Linux treats an IN request with wLength 0 as OUT (usb_submit_urb
        # warns "BOGUS control dir" otherwise).
        direction = DIR_IN if setup.has_in_data else DIR_OUT
        return self.submit(
            0,
            direction,
            setup.length,
            data=data if direction == DIR_OUT else None,
            setup=setup.pack(),
            callback=callback,
        )

    def _read_loop(self) -> None:
        header = bytearray(HEADER_SIZE)
        view = memoryview(header)
        try:
            while True:
                recv_exact(self._stream, view)
                message = usbip.decode_header(header)
                if isinstance(message, RetSubmit):
                    self._ret_submit(message)
                elif isinstance(message, RetUnlink):
                    self._ret_unlink(message)
                else:
                    raise ProtocolError("exporter sent a command, not a reply")
        except (OSError, UsbipError, ProtocolError) as exc:
            self._fail(
                exc
                if isinstance(exc, ConnectionLostError)
                else ConnectionLostError(str(exc))
            )

    def _ret_submit(self, message: RetSubmit) -> None:
        with self._lock:
            urb = self._pending.get(message.seqnum)
            late = None
            if urb is None:
                late = self._tombstones.pop(message.seqnum, None)
                if late is None:
                    raise ProtocolError(
                        f"RET_SUBMIT for unknown seqnum {message.seqnum}"
                    )
            elif message.actual_length > urb.length:
                raise ProtocolError("RET_SUBMIT actual_length exceeds the request")
            # Keep it pending until its payload is read, so a loss fails it.
        if late is not None:
            # A RET_SUBMIT after its URB was abandoned: consume and drop.
            if late == DIR_IN and message.actual_length:
                recv_exact(self._stream, memoryview(bytearray(message.actual_length)))
            self.late_ret_submits += 1
            return
        assert urb is not None
        buffer = None
        if urb.direction == DIR_IN:
            buffer = bytearray(HEADROOM + message.actual_length)
            recv_exact(self._stream, memoryview(buffer)[HEADROOM:])
            self.bytes_in += message.actual_length
        with self._cond:
            if self._pending.pop(message.seqnum, None) is None:
                return  # failed concurrently by close()
            if self._acknowledged.pop(message.seqnum, None) is not None:
                self.late_ret_submits += 1  # RET_UNLINK(0) came first
            urb.result = UrbResult(message.status, message.actual_length, buffer)
            self._cond.notify_all()
        urb._resolve()

    def _ret_unlink(self, message: RetUnlink) -> None:
        cancelled = None
        with self._cond:
            handle = self._unlinks.pop(message.seqnum, None)
            if handle is None:
                raise ProtocolError(f"RET_UNLINK for unknown seqnum {message.seqnum}")
            target = handle.target
            assert target is not None
            if target.seqnum in self._pending:
                if message.status in (-usbip.ECONNRESET, -usbip.ENOENT):
                    cancelled = self._pending.pop(target.seqnum)
                    self._tombstone(target.seqnum, target.direction)
                    cancelled.result = UrbResult(message.status, 0, None, unlinked=True)
                else:
                    self._acknowledged[target.seqnum] = message.status
            handle.result = UrbResult(message.status, 0, None)
            self._cond.notify_all()
        if cancelled is not None:
            cancelled._resolve()
        handle._resolve()

    def _fail(self, exc: BaseException) -> None:
        with self._cond:
            if self._lost is not None:
                return
            self._lost = exc
            pending = list(self._pending.values()) + list(self._unlinks.values())
            self._pending.clear()
            self._unlinks.clear()
            for urb in pending:
                urb.error = exc
            self._cond.notify_all()
        abort_stream(self._stream)
        for urb in pending:
            urb._resolve()
        if self._on_lost is not None:
            self._on_lost(exc)

    def close(self) -> None:
        self._fail(ConnectionLostError("closed locally"))
        if self._reader.is_alive() and self._reader is not threading.current_thread():
            self._reader.join(2.0)
