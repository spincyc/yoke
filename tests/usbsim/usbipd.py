"""A simulated USB/IP exporter with stock ``usbipd``/``usbip-host`` semantics.

It speaks the real 1.1.1 wire format (via :mod:`yoke.usb.usbip`) over real
TCP sockets or in-memory streams and is deliberately strict where the Linux
stub is (``drivers/usb/usbip/stub_rx.c``): a wrong ``devid``, an endpoint
outside the current configuration, a non-positive interrupt interval or an
unknown command closes the connection and is recorded in ``violations``.
SET_CONFIGURATION, SET_INTERFACE, CLEAR_FEATURE(ENDPOINT_HALT) and the hub
port-reset request are applied as the stub's "tweaks" rather than sent to
the device. One importer per device at a time.
"""

from __future__ import annotations

import contextlib
import socket
import struct
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from tests.usbsim.device import DeviceModel, Urb
from yoke.usb import ch9, usbip
from yoke.usb.ch9 import Setup
from yoke.usb.client import ByteStream, ConnectionLostError, abort_stream, recv_exact
from yoke.usb.usbip import CmdSubmit, CmdUnlink, DeviceInfo, ProtocolError


@dataclass
class Export:
    device: DeviceModel
    busnum: int
    devnum: int
    attached: bool = False


@dataclass
class Stats:
    connections: int = 0
    imports: int = 0
    submits: int = 0
    unlinks: int = 0
    unlinked: int = 0
    tweaks: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def device_info(busid: str, export: Export) -> DeviceInfo:
    device = export.device
    desc = device.device_descriptor
    config = device.active_configuration()
    interfaces: tuple[tuple[int, int, int], ...] = ()
    if config is not None:
        interfaces = tuple(
            (alt.interface_class, alt.interface_subclass, alt.interface_protocol)
            for number in sorted(config.interfaces)
            for alt in [config.interfaces[number][device.alternates.get(number, 0)]]
        )
    vendor, product, bcd = struct.unpack_from("<HHH", desc, 8)
    return DeviceInfo(
        path=f"/sys/devices/platform/sim/usb{export.busnum}/{busid}",
        busid=busid,
        busnum=export.busnum,
        devnum=export.devnum,
        speed=int(device.speed),
        vendor=vendor,
        product=product,
        bcd_device=bcd,
        device_class=desc[4],
        device_subclass=desc[5],
        device_protocol=desc[6],
        configuration_value=device.configuration,
        num_configurations=desc[17],
        num_interfaces=len(interfaces),
        interfaces=interfaces,
    )


class _Connection:
    def __init__(
        self, server: FakeUsbipd, stream: ByteStream, busid: str, export: Export
    ) -> None:
        self.server = server
        self.stream = stream
        self.busid = busid
        self.export = export
        self.device = export.device
        self.devid = device_info(busid, export).devid
        self.send_lock = threading.Lock()
        self.lock = threading.Lock()
        self.pending: dict[int, Urb] = {}
        self.closed = False

    def send(self, data: bytes) -> None:
        with self.send_lock:
            if self.closed:
                return
            try:
                self.stream.sendall(data)
            except OSError:
                self.close()

    def close(self) -> None:
        with self.lock:
            if self.closed:
                return
            self.closed = True
            pending = list(self.pending.values())
            self.pending.clear()
        for urb in pending:
            self.device.cancel(urb)
        abort_stream(self.stream)
        self.server._detach(self)

    def violation(self, text: str) -> None:
        self.server.violations.append(text)
        self.close()

    def complete(
        self, seqnum: int, direction: int
    ) -> Callable[[int, int, bytes], None]:
        def done(status: int, actual: int, data: bytes) -> None:
            with self.lock:
                if self.pending.pop(seqnum, None) is None:
                    return  # unlinked first: RET_UNLINK replaces RET_SUBMIT
            payload = data[:actual] if direction == usbip.DIR_IN else b""
            self.send(usbip.RetSubmit(seqnum, status, actual).pack() + payload)

        return done

    def run(self) -> None:
        header = bytearray(usbip.HEADER_SIZE)
        try:
            while not self.closed:
                recv_exact(self.stream, memoryview(header))
                message = usbip.decode_header(header)
                if isinstance(message, CmdSubmit):
                    self.submit(message)
                elif isinstance(message, CmdUnlink):
                    self.unlink(message)
                else:
                    self.violation("client sent a reply message")
        except (OSError, ConnectionLostError, ProtocolError):
            pass
        finally:
            self.close()

    def submit(self, cmd: CmdSubmit) -> None:
        server = self.server
        server.stats.submits += 1
        if cmd.devid != self.devid:
            self.violation(f"devid {cmd.devid:#x} != {self.devid:#x}")
            return
        address = cmd.ep | (0x80 if cmd.direction == usbip.DIR_IN else 0)
        kind = self.device.endpoint_type(address)
        if kind is None:
            self.violation(
                f"CMD_SUBMIT to endpoint {address:#04x} not in configuration"
            )
            return
        data = b""
        if cmd.direction == usbip.DIR_OUT and cmd.transfer_buffer_length:
            buf = bytearray(cmd.transfer_buffer_length)
            recv_exact(self.stream, memoryview(buf))
            data = bytes(buf)
        if cmd.number_of_packets != usbip.NON_ISO_PACKETS:
            server.stats.notes.append("non-ISO number_of_packets != 0xffffffff")
        if kind == ch9.XFER_INT and cmd.interval <= 0:
            self.violation("interrupt URB with non-positive interval")
            return
        if cmd.direction == usbip.DIR_IN and cmd.transfer_flags & usbip.URB_DIR_IN == 0:
            server.stats.notes.append("IN URB without URB_DIR_IN")
        if server.service_delay:
            time.sleep(server.service_delay)
        if kind == ch9.XFER_CONTROL:
            self.control(cmd, data)
            return
        with self.lock:
            self.pending[cmd.seqnum] = Urb(address, 0, lambda *_: None)
        done = self.complete(cmd.seqnum, cmd.direction)
        if cmd.direction == usbip.DIR_IN:
            urb = self.device.submit_in(address, cmd.transfer_buffer_length, done)
        else:
            zero = (
                bool(cmd.transfer_flags & usbip.URB_ZERO_PACKET)
                and kind == ch9.XFER_BULK
            )
            urb = self.device.submit_out(address, data, zero, done)
        with self.lock:
            if cmd.seqnum in self.pending:
                self.pending[cmd.seqnum] = urb

    def control(self, cmd: CmdSubmit, data: bytes) -> None:
        setup = Setup.unpack(cmd.setup)
        tweaked = self.tweak(setup)
        if tweaked:
            status, payload = 0, b""
        else:
            result = self.device.control(setup, data)
            if result is None:
                status, payload = -usbip.EPIPE, b""
            else:
                status = 0
                payload = result[: cmd.transfer_buffer_length] if setup.is_in else b""
        actual = len(payload) if setup.is_in else (len(data) if status == 0 else 0)
        self.send(usbip.RetSubmit(cmd.seqnum, status, actual).pack() + payload)

    def tweak(self, setup: Setup) -> bool:
        """The stub's special requests; True when handled without the device."""
        tweaks = self.server.stats.tweaks
        if (
            setup.request == ch9.CLEAR_FEATURE
            and setup.request_type == ch9.RECIP_ENDPOINT
            and setup.value == ch9.FEATURE_ENDPOINT_HALT
        ):
            tweaks.append(f"clear_halt {setup.index:#04x}")
            return self.device.clear_halt(setup.index & 0x8F)
        if (
            setup.request == ch9.SET_INTERFACE
            and setup.request_type == ch9.RECIP_INTERFACE
        ):
            tweaks.append(f"set_interface {setup.index} {setup.value}")
            return self.device.set_interface(setup.index, setup.value)
        if (
            setup.request == ch9.SET_CONFIGURATION
            and setup.request_type == ch9.RECIP_DEVICE
        ):
            tweaks.append(f"set_configuration {setup.value}")
            return self.device.set_configuration(setup.value)
        if (
            setup.request == ch9.SET_FEATURE
            and setup.request_type == ch9.RT_PORT
            and setup.value == ch9.PORT_FEAT_RESET
        ):
            tweaks.append("reset_device")
            if self.server.reset_delay:
                time.sleep(self.server.reset_delay)
            return self.device.reset()
        return False

    def unlink(self, cmd: CmdUnlink) -> None:
        self.server.stats.unlinks += 1
        if cmd.devid != self.devid:
            self.violation("CMD_UNLINK devid mismatch")
            return
        with self.lock:
            urb = self.pending.pop(cmd.unlink_seqnum, None)
        status = 0
        if urb is not None and self.device.cancel(urb):
            status = -usbip.ECONNRESET
            self.server.stats.unlinked += 1
        elif urb is not None:
            # Completed concurrently: its RET_SUBMIT was suppressed by pop().
            status = -usbip.ECONNRESET
        self.send(usbip.RetUnlink(cmd.seqnum, status).pack())


class FakeUsbipd:
    """Exports device models by busid; serve streams or a TCP listener."""

    def __init__(
        self,
        devices: dict[str, DeviceModel],
        *,
        busnum: int = 1,
        service_delay: float = 0.0,
        reset_delay: float = 0.0,
    ) -> None:
        self.exports = {
            busid: Export(device, busnum, index + 2)
            for index, (busid, device) in enumerate(devices.items())
        }
        self.service_delay = service_delay
        self.reset_delay = reset_delay
        self.stats = Stats()
        self.violations: list[str] = []
        self._lock = threading.Lock()
        self._connections: list[_Connection] = []
        self._listener: socket.socket | None = None
        for busid, export in self.exports.items():
            export.device.unplug_listeners.append(self._unplugged(busid))

    def _unplugged(self, busid: str) -> Callable[[], None]:
        def listener() -> None:
            with self._lock:
                connections = [c for c in self._connections if c.busid == busid]
            for connection in connections:
                connection.close()

        return listener

    def _detach(self, connection: _Connection) -> None:
        with self._lock:
            if connection in self._connections:
                self._connections.remove(connection)
                connection.export.attached = False

    def serve(self, stream: ByteStream) -> None:
        """Handle one client connection until it closes (blocking)."""
        self.stats.connections += 1
        try:
            raw = bytearray(usbip.OP_HEADER_SIZE)
            recv_exact(stream, memoryview(raw))
            version, code, _status = struct.unpack(">HHI", raw)
            if version != usbip.USBIP_VERSION:
                self.violations.append(f"bad version {version:#06x}")
                return
            if code == usbip.OP_REQ_DEVLIST:
                visible = [
                    device_info(busid, e)
                    for busid, e in self.exports.items()
                    if not e.device.unplugged and not e.attached
                ]
                stream.sendall(usbip.encode_rep_devlist(visible))
                return
            if code != usbip.OP_REQ_IMPORT:
                self.violations.append(f"unknown op {code:#06x}")
                return
            body = bytearray(usbip.BUSID_SIZE)
            recv_exact(stream, memoryview(body))
            busid = usbip.decode_req_import(body)
            with self._lock:
                export = self.exports.get(busid)
                if export is None or export.attached or export.device.unplugged:
                    stream.sendall(usbip.encode_rep_import(None))
                    return
                export.attached = True
                connection = _Connection(self, stream, busid, export)
                self._connections.append(connection)
            self.stats.imports += 1
            stream.sendall(usbip.encode_rep_import(device_info(busid, export)))
        except (OSError, ConnectionLostError, ProtocolError):
            with contextlib.suppress(OSError):
                stream.close()
            return
        connection.run()

    def spawn(self, stream: ByteStream) -> threading.Thread:
        thread = threading.Thread(
            target=self._serve_closing, args=(stream,), daemon=True
        )
        thread.start()
        return thread

    def _serve_closing(self, stream: ByteStream) -> None:
        try:
            self.serve(stream)
        finally:
            with contextlib.suppress(OSError):
                stream.close()

    def listen(self, host: str = "127.0.0.1", port: int = 0) -> tuple[str, int]:
        listener = socket.create_server((host, port))
        self._listener = listener
        address: tuple[str, int] = listener.getsockname()[:2]

        def accept() -> None:
            while True:
                try:
                    client, _ = listener.accept()
                except OSError:
                    return
                client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self.spawn(client)

        threading.Thread(target=accept, daemon=True).start()
        return address

    def stop(self) -> None:
        """Simulate a daemon stop: close the listener and every connection."""
        if self._listener is not None:
            self._listener.close()
            self._listener = None
        with self._lock:
            connections = list(self._connections)
        for connection in connections:
            connection.close()

    @property
    def active_connections(self) -> int:
        with self._lock:
            return len(self._connections)
