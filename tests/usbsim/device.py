"""Device-model base: descriptors, standard requests and endpoint pipes.

A :class:`DeviceModel` is what the simulated exporter's host controller
talks to. URBs are completed with Linux semantics: an IN URB of ``length``
bytes completes on a full buffer or a short packet; an OUT URB is split into
packets (plus a ZLP when ``URB_ZERO_PACKET`` asks for one); a stall
completes the active URB with ``-EPIPE`` and halts the endpoint until
``clear_halt``. Completion callbacks run outside the model lock.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum

from tests.usbsim.build import langids, string
from tests.usbsim.packets import BabbleError, Receiver, packetize
from yoke.usb import ch9
from yoke.usb.ch9 import Setup, Speed
from yoke.usb.descriptors import Configuration, Endpoint, parse_configuration
from yoke.usb.usbip import EOVERFLOW, EPIPE, ESHUTDOWN

Complete = Callable[[int, int, bytes], None]
"""``(status, actual_length, in_data)``; status is 0 or a negative errno."""


class Verdict(Enum):
    ACCEPT = "accept"
    NAK = "nak"
    STALL = "stall"


@dataclass(eq=False)
class Urb:
    address: int
    length: int
    complete: Complete
    data: bytes = b""
    zero_packet: bool = False
    receiver: Receiver | None = None
    packets: deque[bytes] = field(default_factory=deque)
    accepted: int = 0
    finished: bool = False


class Pipe:
    def __init__(self, model: DeviceModel, endpoint: Endpoint) -> None:
        self.model = model
        self.endpoint = endpoint
        self.urbs: deque[Urb] = deque()
        self.halted = False

    @property
    def packet_size(self) -> int:
        return self.endpoint.packet_size

    def finish(self, urb: Urb, status: int, actual: int, data: bytes = b"") -> None:
        if urb.finished:
            return
        urb.finished = True
        self.model.defer(lambda: urb.complete(status, actual, data))

    def cancel(self, urb: Urb) -> bool:
        if urb in self.urbs:
            self.urbs.remove(urb)
            urb.finished = True
            return True
        return False

    def shutdown(self, status: int) -> None:
        while self.urbs:
            urb = self.urbs.popleft()
            data = bytes(urb.receiver.data) if urb.receiver else b""
            self.finish(urb, status, len(data) or urb.accepted, data)

    def fail_halted(self) -> None:
        """A halted endpoint answers every transaction with STALL (-EPIPE)."""
        while self.urbs:
            urb = self.urbs.popleft()
            self.finish(urb, -EPIPE, 0)

    def pump(self) -> None:
        raise NotImplementedError


class InPipe(Pipe):
    """Device-to-host endpoint: the model queues packets with :meth:`send`."""

    def __init__(self, model: DeviceModel, endpoint: Endpoint) -> None:
        super().__init__(model, endpoint)
        self.packets: deque[bytes] = deque()

    def send(self, data: bytes, *, zlp: bool = False) -> None:
        self.packets.extend(packetize(data, self.packet_size, zlp=zlp))
        self.pump()

    def stall(self) -> None:
        """Halt now: the active URB fails with -EPIPE after partial data."""
        self.halted = True
        if self.urbs:
            urb = self.urbs.popleft()
            data = bytes(urb.receiver.data) if urb.receiver else b""
            self.finish(urb, -EPIPE, len(data), data)
        self.fail_halted()

    def pump(self) -> None:
        if self.halted:
            self.fail_halted()
            return
        while not self.halted and self.urbs and self.packets:
            urb = self.urbs[0]
            assert urb.receiver is not None
            try:
                urb.receiver.feed(self.packets.popleft())
            except BabbleError:
                self.urbs.popleft()
                data = bytes(urb.receiver.data)
                self.finish(urb, -EOVERFLOW, len(data), data)
                continue
            if urb.receiver.done:
                self.urbs.popleft()
                data = bytes(urb.receiver.data)
                self.finish(urb, 0, len(data), data)


class OutPipe(Pipe):
    """Host-to-device endpoint: packets go to :meth:`DeviceModel.on_out`."""

    def pump(self) -> None:
        if self.halted:
            self.fail_halted()
            return
        while not self.halted and self.urbs:
            urb = self.urbs[0]
            while urb.packets:
                verdict = self.model.on_out(self.endpoint.address, urb.packets[0])
                if verdict is Verdict.NAK:
                    return
                if verdict is Verdict.STALL:
                    self.halted = True
                    self.urbs.popleft()
                    self.finish(urb, -EPIPE, urb.accepted)
                    self.fail_halted()
                    return
                urb.accepted += len(urb.packets.popleft())
            self.urbs.popleft()
            self.finish(urb, 0, urb.accepted)


class DeviceModel:
    """Base device: standard requests, configuration state and pipes.

    Subclasses initialize their own attributes *before* calling
    ``super().__init__`` because the base applies the initial configuration
    (the exporting kernel configures a device before usbip-host binds it),
    which runs :meth:`on_configuration`.
    """

    def __init__(
        self,
        *,
        speed: Speed,
        device: bytes,
        configurations: list[bytes],
        strings: dict[int, str] | None = None,
        lang: int = 0x0409,
        qualifier: bytes | None = None,
        other_speed: bytes | None = None,
        bos: bytes | None = None,
        class_descriptors: dict[tuple[int, int, int], bytes] | None = None,
    ) -> None:
        self.speed = speed
        self.device_descriptor = device
        self.config_descriptors = configurations
        self.configurations = [parse_configuration(c) for c in configurations]
        self.strings = strings or {}
        self.lang = lang
        self.qualifier = qualifier
        self.other_speed = other_speed
        self.bos = bos
        self.class_descriptors = class_descriptors or {}
        self.lock = threading.RLock()
        self._depth = 0
        self._deferred: list[Callable[[], None]] = []
        self.configuration = 0
        self.alternates: dict[int, int] = {}
        self.pipes: dict[int, Pipe] = {}
        self.unplugged = False
        self.unplug_listeners: list[Callable[[], None]] = []
        self.control_log: list[Setup] = []
        self.violations: list[str] = []
        self.resets = 0
        self.remote_wakeup = False
        # The exporting kernel configures a device before usbip-host binds it.
        self.set_configuration(self.configurations[0].value)

    # --- locking and deferred completion ---------------------------------

    def defer(self, callback: Callable[[], None]) -> None:
        self._deferred.append(callback)

    @contextmanager
    def locked(self) -> Iterator[None]:
        ready: list[Callable[[], None]] = []
        with self.lock:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
                if self._depth == 0:
                    ready, self._deferred = self._deferred, []
        for callback in ready:
            callback()

    # --- configuration ---------------------------------------------------

    def active_configuration(self) -> Configuration | None:
        for config in self.configurations:
            if config.value == self.configuration:
                return config
        return None

    def _rebuild_pipes(self) -> None:
        for pipe in self.pipes.values():
            pipe.shutdown(-ESHUTDOWN)
        self.pipes = {}
        config = self.active_configuration()
        if config is None:
            return
        for number, alt in self.alternates.items():
            setting = config.alt(number, alt)
            assert setting is not None
            for ep in setting.endpoints:
                self.pipes[ep.address] = (
                    InPipe(self, ep) if ep.is_in else OutPipe(self, ep)
                )

    def set_configuration(self, value: int) -> bool:
        with self.locked():
            if value != 0 and not any(c.value == value for c in self.configurations):
                return False
            self.configuration = value
            config = self.active_configuration()
            self.alternates = dict.fromkeys(config.interfaces, 0) if config else {}
            self._rebuild_pipes()
            self.on_configuration(value)
            return True

    def set_interface(self, number: int, alternate: int) -> bool:
        with self.locked():
            config = self.active_configuration()
            if config is None or config.alt(number, alternate) is None:
                return False
            self.alternates[number] = alternate
            self._rebuild_pipes()
            self.on_interface(number, alternate)
            return True

    def clear_halt(self, address: int) -> bool:
        with self.locked():
            pipe = self.pipes.get(address)
            if pipe is None:
                return address & 0x0F == 0
            pipe.halted = False
            self.on_clear_halt(address)
            pipe.pump()
            return True

    def reset(self) -> bool:
        """Port reset: the exporting kernel restores the active configuration."""
        with self.locked():
            self.resets += 1
            value = self.configuration
            saved = dict(self.alternates)
            self.on_reset()
            self.set_configuration(value)
            for number, alt in saved.items():
                if alt:
                    self.set_interface(number, alt)
            return True

    def unplug(self) -> None:
        with self.locked():
            self.unplugged = True
            for pipe in self.pipes.values():
                pipe.shutdown(-ESHUTDOWN)
            listeners = list(self.unplug_listeners)
        for listener in listeners:
            listener()

    def replug(self) -> None:
        with self.locked():
            self.unplugged = False
            self.set_configuration(self.configurations[0].value)

    # --- hooks for subclasses -------------------------------------------

    def on_configuration(self, value: int) -> None:
        """Called with the lock held after SET_CONFIGURATION."""

    def on_interface(self, number: int, alternate: int) -> None:
        """Called with the lock held after SET_INTERFACE."""

    def on_clear_halt(self, address: int) -> None:
        """Called with the lock held after CLEAR_FEATURE(ENDPOINT_HALT)."""

    def on_reset(self) -> None:
        """Called with the lock held on port reset, before reconfiguration."""

    def on_out(self, address: int, packet: bytes) -> Verdict:
        """Consume one OUT packet. Called with the lock held."""
        return Verdict.ACCEPT

    def class_request(self, setup: Setup, data: bytes) -> bytes | None:
        """Answer a class request (IN data, b"" for OUT/no-data, None=STALL)."""
        return None

    def vendor_request(self, setup: Setup, data: bytes) -> bytes | None:
        return None

    # --- endpoint access for the exporter --------------------------------

    def endpoint_type(self, address: int) -> int | None:
        if address & 0x0F == 0:
            return ch9.XFER_CONTROL
        pipe = self.pipes.get(address)
        return pipe.endpoint.transfer_type if pipe else None

    def in_pipe(self, address: int) -> InPipe:
        pipe = self.pipes[address]
        assert isinstance(pipe, InPipe)
        return pipe

    def submit_in(self, address: int, length: int, complete: Complete) -> Urb:
        with self.locked():
            urb = Urb(address, length, complete)
            pipe = self.pipes.get(address)
            if self.unplugged or pipe is None:
                urb.finished = True
                self.defer(lambda: complete(-ESHUTDOWN, 0, b""))
                return urb
            urb.receiver = Receiver(length, pipe.packet_size)
            pipe.urbs.append(urb)
            pipe.pump()
            return urb

    def submit_out(
        self, address: int, data: bytes, zero_packet: bool, complete: Complete
    ) -> Urb:
        with self.locked():
            urb = Urb(address, len(data), complete, data, zero_packet)
            pipe = self.pipes.get(address)
            if self.unplugged or pipe is None:
                urb.finished = True
                self.defer(lambda: complete(-ESHUTDOWN, 0, b""))
                return urb
            urb.packets = deque(packetize(data, pipe.packet_size, zlp=zero_packet))
            pipe.urbs.append(urb)
            pipe.pump()
            return urb

    def cancel(self, urb: Urb) -> bool:
        with self.locked():
            pipe = self.pipes.get(urb.address)
            return pipe.cancel(urb) if pipe else False

    def resume_out(self, address: int) -> None:
        """Retry NAKed OUT packets after the model frees buffer space."""
        with self.locked():
            pipe = self.pipes.get(address)
            if pipe is not None:
                pipe.pump()

    # --- endpoint zero ---------------------------------------------------

    def control(self, setup: Setup, data: bytes = b"") -> bytes | None:
        """Process a control transfer; return IN data, b"" or None (STALL)."""
        with self.locked():
            self.control_log.append(setup)
            if self.unplugged:
                return None
            if setup.is_standard:
                result = self._standard(setup, data)
            elif setup.kind == ch9.TYPE_CLASS:
                result = self.class_request(setup, data)
            elif setup.kind == ch9.TYPE_VENDOR:
                result = self.vendor_request(setup, data)
            else:
                result = None
            if result is not None and setup.is_in:
                result = result[: setup.length]
            return result

    def _descriptor(self, setup: Setup) -> bytes | None:
        dtype, index = setup.descriptor_type, setup.descriptor_index
        if setup.recipient == ch9.RECIP_INTERFACE:
            return self.class_descriptors.get((setup.index, dtype, index))
        if dtype == ch9.DT_DEVICE:
            return self.device_descriptor
        if dtype == ch9.DT_CONFIG:
            if index < len(self.config_descriptors):
                return self.config_descriptors[index]
            return None
        if dtype == ch9.DT_STRING:
            if index == 0:
                return langids(self.lang) if self.strings else None
            text = self.strings.get(index)
            return string(text) if text is not None else None
        if dtype == ch9.DT_DEVICE_QUALIFIER:
            return self.qualifier
        if dtype == ch9.DT_OTHER_SPEED_CONFIG:
            return self.other_speed
        if dtype == ch9.DT_BOS:
            return self.bos
        return None

    def _standard(self, setup: Setup, data: bytes) -> bytes | None:
        request, recipient = setup.request, setup.recipient
        if request == ch9.GET_DESCRIPTOR and setup.is_in:
            return self._descriptor(setup)
        if request == ch9.SET_ADDRESS:
            self.violations.append("SET_ADDRESS reached the exported device")
            return None
        if request == ch9.GET_STATUS and setup.is_in:
            if recipient == ch9.RECIP_DEVICE:
                config = self.active_configuration()
                powered = bool(config and config.attributes & 0x40)
                return bytes([int(powered) | int(self.remote_wakeup) << 1, 0])
            if recipient == ch9.RECIP_ENDPOINT:
                pipe = self.pipes.get(setup.index & 0x8F)
                return bytes([int(bool(pipe and pipe.halted)), 0])
            return b"\0\0"
        if request == ch9.GET_CONFIGURATION:
            return bytes([self.configuration])
        if request == ch9.GET_INTERFACE:
            return bytes([self.alternates.get(setup.index, 0)])
        if request == ch9.SET_CONFIGURATION:
            return b"" if self.set_configuration(setup.value) else None
        if request == ch9.SET_INTERFACE:
            return b"" if self.set_interface(setup.index, setup.value) else None
        if request in (ch9.CLEAR_FEATURE, ch9.SET_FEATURE):
            if recipient == ch9.RECIP_ENDPOINT and setup.value == 0:
                pipe = self.pipes.get(setup.index & 0x8F)
                if pipe is None:
                    return None
                if request == ch9.CLEAR_FEATURE:
                    self.clear_halt(setup.index & 0x8F)
                elif isinstance(pipe, InPipe):
                    pipe.stall()
                else:
                    pipe.halted = True
                return b""
            if recipient == ch9.RECIP_DEVICE and setup.value == 1:
                self.remote_wakeup = request == ch9.SET_FEATURE
                return b""
        return None
