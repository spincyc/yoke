"""Proxy engine: present one imported USB/IP device through a gadget backend.

Threads (ADR 0021):

* the event thread fetches gadget events and answers endpoint zero;
* one pump per enabled endpoint moves bulk/interrupt data;
* the USB/IP reader thread (in :mod:`yoke.usb.client`) completes URBs.

Packet semantics are exact by construction. Without a class-specific size
hint a pump moves one (high-bandwidth) packet per URB, because a bulk
transfer boundary that falls on a packet multiple is invisible to the
receiving side. IN pumps keep several URBs outstanding and coalesce full
completions into one gadget write; the UDC re-packetizes them into the same
packets. Bulk-Only Transport interfaces derive exact transfer sizes from
each CBW (a payload inspection confined to this worker process).

A remote STALL is reproduced locally: the pump writes any partial data,
unlinks later URBs, halts the gadget endpoint and immediately clears the
remote halt; the computer's own CLEAR_FEATURE is handled by the UDC. The
owner reacts to :attr:`Proxy.ended` by calling :meth:`Proxy.stop`, which
closes the gadget so the computer sees a disconnect.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass

from yoke.usb import ch9, usbip
from yoke.usb.ch9 import SPEED_NAMES, Setup, Speed
from yoke.usb.client import (
    ConnectionLostError,
    Submission,
    Urb,
    UsbipConnection,
    UsbipError,
)
from yoke.usb.descriptors import Configuration, Endpoint, Presentation
from yoke.usb.gadget import (
    HEADROOM,
    EventType,
    GadgetBackend,
    GadgetError,
    GadgetInterruptedError,
    GadgetShutdownError,
    UdcEndpoint,
)
from yoke.usb.policy import (
    CSW_SIZE,
    Ack,
    ControlState,
    EndpointHalt,
    Forward,
    ProxyPolicy,
    Reply,
    SetConfiguration,
    SetInterface,
    Stall,
    chunk_sizes,
    decide,
    in_write_plan,
    parse_cbw,
)

TRANSACTION_ERRORS = frozenset(
    {-usbip.EPROTO, -usbip.EILSEQ, -usbip.ETIME, -usbip.EOVERFLOW, -usbip.EREMOTEIO}
)
GONE = frozenset({-usbip.ESHUTDOWN, -usbip.ENODEV})
CANCELLED = frozenset({-usbip.ECONNRESET, -usbip.ENOENT})


class TeardownError(RuntimeError):
    """A proxy thread did not stop within the bound; resources are retained."""


class _Stop(Exception):  # noqa: N818 - a pump unwind signal, not an error
    """Internal: unwind a pump."""


@dataclass
class Counters:
    control: int = 0
    cached: int = 0
    forwarded: int = 0
    local_stalls: int = 0
    remote_stalls: int = 0
    transaction_errors: int = 0
    late_out_stalls: int = 0
    dropped_out: int = 0
    bus_resets: int = 0
    remote_resets: int = 0
    disconnects: int = 0
    in_urbs: int = 0
    out_urbs: int = 0
    in_bytes: int = 0
    out_bytes: int = 0
    gadget_writes: int = 0
    gadget_reads: int = 0


class _BotLink:
    """Bulk-Only Transport transfer sizes shared by an interface's pumps."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.in_sizes: deque[int] = deque()
        self.wake: Callable[[], None] = lambda: None

    def push_in(self, sizes: list[int]) -> None:
        with self.lock:
            self.in_sizes.extend(sizes)
        self.wake()

    def pop_in(self) -> int | None:
        with self.lock:
            return self.in_sizes.popleft() if self.in_sizes else None

    def requeue_in(self, sizes: list[int]) -> None:
        with self.lock:
            self.in_sizes.extendleft(reversed(sizes))


class _Pump:
    def __init__(
        self, proxy: Proxy, endpoint: Endpoint, handle: int, bot: _BotLink | None
    ) -> None:
        self.proxy = proxy
        self.endpoint = endpoint
        self.handle = handle
        self.bot = bot
        self.address = endpoint.address
        self.packet_size = endpoint.packet_size
        self.size = endpoint.transfer_size
        self.interval = ch9.urb_interval(
            proxy.presentation.remote_speed, endpoint.transfer_type, endpoint.interval
        )
        self.stopping = False
        self.cond = threading.Condition()
        self.inflight: deque[Urb] = deque()
        self.sizes: dict[int, int] = {}
        name = f"usb-ep{self.address:02x}"
        self.thread = threading.Thread(target=self._main, name=name, daemon=True)

    def wake(self, _urb: Urb | None = None) -> None:
        with self.cond:
            self.cond.notify_all()

    def request_stop(self) -> None:
        with self.cond:
            self.stopping = True
            self.cond.notify_all()

    def _main(self) -> None:
        try:
            self._run()
        except _Stop:
            pass
        except ConnectionLostError as exc:
            self.proxy._remote_lost(exc)
        except Exception as exc:  # pragma: no cover - defensive; reported
            self.proxy._fault(f"endpoint {self.address:#04x}: {exc!r}")

    def _run(self) -> None:
        raise NotImplementedError

    def _unlink(self, urbs: list[Urb]) -> None:
        """Unlink not-yet-complete URBs and wait for every resolution."""
        conn = self.proxy.conn
        for urb in urbs:
            if not urb.done:
                with contextlib.suppress(UsbipError):
                    conn.unlink(urb, callback=self.wake)
        deadline = time.monotonic() + self.proxy.policy.stop_timeout
        with self.cond:
            while not all(u.done for u in urbs):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TeardownError(
                        f"endpoint {self.address:#04x}: unlink unconfirmed"
                    )
                self.cond.wait(remaining)

    def _halt_and_recover(self, later: list[Urb]) -> None:
        """Reproduce a remote STALL locally, then clear the remote halt."""
        self._unlink(later)
        if self.bot is not None and self.endpoint.is_in:
            self.bot.requeue_in([self.sizes.pop(id(u), self.size) for u in later])
        backend = self.proxy.backend
        try:
            backend.ep_set_halt(self.handle)
        except GadgetError as exc:
            if self.stopping:
                raise _Stop from exc
            self.proxy._fault(f"halt {self.address:#04x}: {exc}")
        self.proxy.counters.local_stalls += 1
        clear = Setup(
            ch9.RECIP_ENDPOINT,
            ch9.CLEAR_FEATURE,
            ch9.FEATURE_ENDPOINT_HALT,
            self.address,
        )
        result = self.proxy.conn.control(
            clear, timeout=self.proxy.policy.control_timeout
        )
        if result is None:
            self.proxy._fault(f"remote refused to clear halt on {self.address:#04x}")


class _InPump(_Pump):
    """Device-to-computer: prefetch URBs, write completions in order."""

    def _next_size(self) -> int | None:
        if self.bot is not None:
            return self.bot.pop_in()
        return self.size

    def _can_submit(self, depth: int) -> bool:
        """A BOT size hint arrived while the queue has room (no lost wakeup)."""
        return (
            self.bot is not None
            and bool(self.bot.in_sizes)
            and len(self.inflight) < depth
        )

    def _write(self, buf: bytearray, length: int, zero: bool) -> None:
        backend = self.proxy.backend
        while True:
            try:
                backend.ep_write(self.handle, buf, length, zero)
            except GadgetInterruptedError as exc:
                if self.stopping:
                    raise _Stop from exc
                continue
            except GadgetShutdownError as exc:
                raise _Stop from exc
            except GadgetError as exc:
                self.proxy._fault(f"write {self.address:#04x}: {exc}")
                raise _Stop from exc
            counters = self.proxy.counters
            counters.gadget_writes += 1
            counters.in_bytes += length
            return

    def _run(self) -> None:
        policy = self.proxy.policy
        conn = self.proxy.conn
        depth = (
            policy.in_depth_interrupt
            if self.endpoint.transfer_type == ch9.XFER_INT
            else policy.in_depth_bulk
        )
        scratch = bytearray(HEADROOM + policy.max_transfer)
        while True:
            batch: list[Submission] = []
            while len(self.inflight) + len(batch) < depth and not self.stopping:
                size = self._next_size()
                if size is None:
                    break
                batch.append(
                    Submission(
                        self.endpoint.number,
                        usbip.DIR_IN,
                        size,
                        flags=usbip.URB_DIR_IN,
                        interval=self.interval,
                        callback=self.wake,
                    )
                )
            if batch:
                groups = [batch] if policy.batch_submit else [[b] for b in batch]
                for group in groups:
                    for urb in conn.submit_many(group):
                        self.sizes[id(urb)] = urb.length
                        with self.cond:
                            self.inflight.append(urb)
                self.proxy.counters.in_urbs += len(batch)
            with self.cond:
                while (
                    not self.stopping
                    and not (self.inflight and self.inflight[0].done)
                    and not self._can_submit(depth)
                ):
                    self.cond.wait()
                if self.stopping:
                    raise _Stop
                done: list[Urb] = []
                while self.inflight and self.inflight[0].done:
                    done.append(self.inflight.popleft())
            self._deliver(done, scratch)

    def _deliver(self, done: list[Urb], scratch: bytearray) -> None:
        policy = self.proxy.policy
        index = 0
        while index < len(done):
            urb = done[index]
            if urb.error is not None:
                raise ConnectionLostError(str(urb.error))
            assert urb.result is not None
            status = urb.result.status
            if status == 0:
                run: list[tuple[int, int]] = []
                for item in done[index:] if policy.coalesce_in else [urb]:
                    assert item.result is not None or item.error is not None
                    if item.result is None or item.result.status != 0:
                        break
                    run.append((item.result.actual_length, item.length))
                count, total, zero = in_write_plan(
                    run, self.packet_size, policy.max_transfer
                )
                if count == 1:
                    assert urb.result.buffer is not None
                    buf = urb.result.buffer
                else:
                    offset = HEADROOM
                    for item in done[index : index + count]:
                        assert (
                            item.result is not None and item.result.buffer is not None
                        )
                        size = item.result.actual_length
                        scratch[offset : offset + size] = memoryview(
                            item.result.buffer
                        )[HEADROOM : HEADROOM + size]
                        offset += size
                    buf = scratch
                for item in done[index : index + count]:
                    self.sizes.pop(id(item), None)
                self._write(buf, total, zero)
                index += count
                continue
            self.sizes.pop(id(urb), None)
            if status in GONE:
                raise ConnectionLostError(f"device gone (status {status})")
            if status in CANCELLED:
                if self.stopping:
                    raise _Stop
                index += 1
                continue
            # STALL (or a transaction error, reported to the computer as STALL).
            counters = self.proxy.counters
            if status == -usbip.EPIPE:
                counters.remote_stalls += 1
            else:
                counters.transaction_errors += 1
            if urb.result.actual_length:
                assert urb.result.buffer is not None
                self._write(urb.result.buffer, urb.result.actual_length, False)
            with self.cond:
                later = done[index + 1 :] + list(self.inflight)
                self.inflight.clear()
            self._halt_and_recover(later)
            return


class _OutPump(_Pump):
    """Computer-to-device: read from the gadget, submit in order."""

    def __init__(
        self, proxy: Proxy, endpoint: Endpoint, handle: int, bot: _BotLink | None
    ) -> None:
        super().__init__(proxy, endpoint, handle, bot)
        self.read_sizes: deque[int] = deque()
        self.bot_in_packet = 512

    def _read(self, buf: bytearray, size: int) -> int:
        backend = self.proxy.backend
        while True:
            try:
                count = backend.ep_read(self.handle, buf, size)
            except GadgetInterruptedError as exc:
                if self.stopping:
                    raise _Stop from exc
                continue
            except GadgetShutdownError as exc:
                raise _Stop from exc
            except GadgetError as exc:
                self.proxy._fault(f"read {self.address:#04x}: {exc}")
                raise _Stop from exc
            self.proxy.counters.gadget_reads += 1
            return count

    def _completed(self) -> tuple[list[Urb], bool]:
        """Pop completed head URBs; report whether one of them failed."""
        done: list[Urb] = []
        failed = False
        with self.cond:
            while self.inflight and self.inflight[0].done:
                urb = self.inflight.popleft()
                done.append(urb)
                if urb.error is not None:
                    raise ConnectionLostError(str(urb.error))
                assert urb.result is not None
                if urb.result.status != 0:
                    failed = True
                    break
        return done, failed

    def _handle_failure(self, urb: Urb) -> None:
        assert urb.result is not None
        status = urb.result.status
        if status in GONE:
            raise ConnectionLostError(f"device gone (status {status})")
        if status in CANCELLED:
            if self.stopping:
                raise _Stop
            return
        counters = self.proxy.counters
        if status == -usbip.EPIPE:
            counters.remote_stalls += 1
        else:
            counters.transaction_errors += 1
        with self.cond:
            later = list(self.inflight)
            self.inflight.clear()
        if self.proxy.policy.out_window > 1:
            counters.late_out_stalls += 1
        self._halt_and_recover(later)

    def _run(self) -> None:
        policy = self.proxy.policy
        conn = self.proxy.conn
        buf = bytearray(HEADROOM + max(policy.max_transfer, self.size))
        while True:
            done, failed = self._completed()
            if failed:
                self._handle_failure(done[-1])
                continue
            with self.cond:
                if self.stopping:
                    raise _Stop
                if len(self.inflight) >= policy.out_window:
                    while not self.stopping and not self.inflight[0].done:
                        self.cond.wait()
                    continue
            size = self.read_sizes.popleft() if self.read_sizes else self.size
            count = self._read(buf, size)
            with self.cond:
                late = any(
                    u.done and u.result and u.result.status for u in self.inflight
                )
            if late:
                # Deferred-stall semantics (out_window > 1): the UDC already
                # acknowledged this data; the device would have refused it.
                self.proxy.counters.dropped_out += 1
                continue
            if self.bot is not None and count == 31:
                cbw = parse_cbw(memoryview(buf)[HEADROOM : HEADROOM + count])
                if cbw is not None:
                    self._plan(*cbw)
            urb = conn.submit(
                self.endpoint.number,
                usbip.DIR_OUT,
                count,
                data=memoryview(buf)[HEADROOM : HEADROOM + count],
                flags=0,
                interval=self.interval,
                callback=self.wake,
            )
            counters = self.proxy.counters
            counters.out_urbs += 1
            counters.out_bytes += count
            with self.cond:
                self.inflight.append(urb)

    def _plan(self, length: int, data_in: bool) -> None:
        assert self.bot is not None
        limit = self.proxy.policy.max_transfer
        csw = max(self.bot_in_packet, CSW_SIZE)
        if data_in:
            sizes = [
                -(-size // self.bot_in_packet) * self.bot_in_packet
                for size in chunk_sizes(length, limit)
            ]
            self.bot.push_in([*sizes, csw])
        else:
            self.read_sizes.extend(chunk_sizes(length, limit))
            self.bot.push_in([csw])


class Proxy:
    """Presents ``presentation`` on ``backend`` and relays to ``conn``."""

    def __init__(
        self,
        conn: UsbipConnection,
        backend: GadgetBackend,
        presentation: Presentation,
        policy: ProxyPolicy,
        *,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.conn = conn
        self.backend = backend
        self.presentation = presentation
        self.policy = policy
        self.counters = Counters()
        self.ended = threading.Event()
        self.end_reason: str | None = None
        self.error: str | None = None
        self.rejected: str | None = None
        self.state = "idle"
        self.suspended = False
        self._log = log or (lambda _message: None)
        self._control = ControlState()
        self._pumps: dict[int, _Pump] = {}
        self._handles: dict[int, int] = {}
        self._udc_endpoints: list[UdcEndpoint] = []
        self._stopping = False
        self._event_thread = threading.Thread(
            target=self._event_main, name="usb-gadget-events", daemon=True
        )
        self._remote_dirty = True
        self._reset_done = threading.Event()
        self._reset_done.set()
        self._reset_threads: list[threading.Thread] = []
        self._speed_checked = False
        self._link_speed = Speed.UNKNOWN

    # --- lifecycle -------------------------------------------------------

    def start(self) -> None:
        self.backend.start(self.presentation.speed)
        self.state = "presenting"
        try:
            self._udc_endpoints = self.backend.endpoints()
        except GadgetError as exc:
            self._log(f"EPS_INFO unavailable: {exc}")
        missing = self._unsupported_endpoints()
        if missing:
            self.rejected = f"UDC lacks endpoints: {', '.join(missing)}"
            self._end(self.rejected, error=True)
        self._event_thread.start()

    def _unsupported_endpoints(self) -> list[str]:
        if not self._udc_endpoints:
            return []
        missing = []
        for config in self.presentation.configurations:
            for endpoint in config.all_endpoints():
                if not any(
                    u.supports(
                        endpoint.address, endpoint.transfer_type, endpoint.packet_size
                    )
                    for u in self._udc_endpoints
                ):
                    missing.append(f"{endpoint.address:#04x}")
        return sorted(set(missing))

    def _end(self, reason: str, *, error: bool = False) -> None:
        if self.end_reason is None:
            self.end_reason = reason
            if error:
                self.error = reason
            self._log(f"proxy ending: {reason}")
        self.ended.set()

    def _remote_lost(self, exc: BaseException) -> None:
        self._end(f"remote lost: {exc}")

    def _fault(self, message: str) -> None:
        self._log(message)
        self.error = message

    def _interrupt_join(self, thread: threading.Thread, deadline: float) -> bool:
        while thread.is_alive() and time.monotonic() < deadline:
            ident = thread.ident
            if ident is not None:
                with contextlib.suppress(OSError, GadgetError):
                    self.backend.interrupt(ident)
            thread.join(0.02)
        return not thread.is_alive()

    def stop(self) -> None:
        """Confirmed teardown; raise :class:`TeardownError` if unconfirmed."""
        self._stopping = True
        self._end("stopped")
        deadline = time.monotonic() + self.policy.stop_timeout
        problems = []
        if self._event_thread.is_alive() and not self._interrupt_join(
            self._event_thread, deadline
        ):
            problems.append("event thread")
        try:
            self._stop_pumps(list(self._pumps.values()), deadline)
        except TeardownError as exc:
            problems.append(str(exc))
        for thread in self._reset_threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        self.conn.close()
        if problems:
            self.error = f"teardown unconfirmed: {'; '.join(problems)}"
            raise TeardownError(self.error)
        self._disable_endpoints(list(self._handles))
        self.backend.close()
        self.state = "stopped"

    # --- status ----------------------------------------------------------

    def snapshot(self) -> dict[str, object]:
        device = self.presentation.device
        return {
            "state": "suspended"
            if self.suspended and self.state == "configured"
            else self.state,
            "configuration": self._control.configuration,
            "vendor": f"{device.vendor:04x}",
            "product": f"{device.product:04x}",
            "remote_speed": SPEED_NAMES[self.presentation.remote_speed],
            "presented_speed": SPEED_NAMES[self.presentation.speed],
            "link_speed": SPEED_NAMES[self._link_speed],
            "error": self.error,
            "rejected": self.rejected,
            "ended": self.end_reason,
            "counters": asdict(self.counters),
        }

    # --- event thread ----------------------------------------------------

    def _event_main(self) -> None:
        backend = self.backend
        while not self._stopping:
            try:
                event = backend.fetch_event()
            except GadgetInterruptedError:
                continue
            except GadgetError as exc:
                if not self._stopping:
                    self._end(f"gadget event error: {exc}", error=True)
                return
            try:
                if event.type is EventType.CONTROL:
                    assert event.setup is not None
                    self._on_control(event.setup)
                elif event.type is EventType.RESET:
                    self._on_bus_reset("default")
                elif event.type is EventType.DISCONNECT:
                    self.counters.disconnects += 1
                    self._on_bus_reset("disconnected")
                elif event.type is EventType.SUSPEND:
                    self.suspended = True
                elif event.type is EventType.RESUME:
                    self.suspended = False
                elif event.type is EventType.CONNECT:
                    self.state = "attached"
            except TeardownError as exc:
                self._end(str(exc), error=True)
                return
            except ConnectionLostError as exc:
                self._remote_lost(exc)

    def _ep0(self, action: Callable[[], object]) -> None:
        try:
            action()
        except GadgetError as exc:
            self._log(f"ep0: {exc}")

    def _reply(self, setup: Setup, data: bytes) -> None:
        if setup.has_in_data:
            data = data[: setup.length]
            zero = len(data) < setup.length
            self._ep0(lambda: self.backend.ep0_write(data, zero))
        else:
            self._ep0(lambda: self.backend.ep0_read(0))

    def _stall(self, reason: str) -> None:
        self._log(f"stall: {reason}")
        self._ep0(self.backend.ep0_stall)

    def _on_control(self, setup: Setup) -> None:
        self.counters.control += 1
        if not self._speed_checked:
            self._speed_checked = True
            self._link_speed = self.backend.link_speed()
            if Speed.UNKNOWN < self._link_speed < self.presentation.speed:
                self.rejected = (
                    f"computer link is {SPEED_NAMES[self._link_speed]}; device needs "
                    f"{SPEED_NAMES[self.presentation.speed]} (unsupported)"
                )
                self._fault(self.rejected)
        if self.rejected:
            self._stall(self.rejected)
            return
        decision = decide(setup, self.presentation, self._control, self.policy)
        if isinstance(decision, Reply):
            self.counters.cached += 1
            self._reply(setup, decision.data)
        elif isinstance(decision, Ack):
            self._ep0(lambda: self.backend.ep0_read(0))
        elif isinstance(decision, Stall):
            self._stall(decision.reason)
        elif isinstance(decision, Forward):
            self._forward(setup, decision.cache)
        elif isinstance(decision, SetConfiguration):
            self._set_configuration(setup, decision.value)
        elif isinstance(decision, SetInterface):
            self._set_interface(setup, decision.interface, decision.alternate)
        elif isinstance(decision, EndpointHalt):
            self._forward(setup, False)

    def _gate(self) -> None:
        """Wait for an in-flight remote reset before touching the device."""
        self._reset_done.wait(self.policy.control_timeout)
        self._remote_dirty = True

    def _remote_control(self, setup: Setup, data: bytes = b"") -> bytes | None:
        self._gate()
        self.counters.forwarded += 1
        return self.conn.control(setup, data, timeout=self.policy.control_timeout)

    def _forward(self, setup: Setup, cache: bool) -> None:
        data = b""
        if not setup.is_in and setup.length:
            try:
                data = self.backend.ep0_read(setup.length)
            except GadgetError as exc:
                self._log(f"ep0 OUT data: {exc}")
                return
        try:
            result = self._remote_control(setup, data)
        except TimeoutError:
            self._stall("remote control request timed out")
            return
        except ConnectionLostError:
            raise
        except UsbipError as exc:
            self._stall(str(exc))
            return
        if cache:
            self._control.descriptor_misses[(setup.value, setup.index)] = (
                result,
                setup.length,
            )
        if not setup.is_in and setup.length:
            # The UDC completed the status stage when the data arrived.
            if result is None:
                self.counters.late_out_stalls += 1
                self._log(f"remote STALL after local status stage: {setup}")
            return
        if result is None:
            self._stall("remote STALL")
        else:
            self._reply(setup, result)

    # --- configuration ---------------------------------------------------

    def _bot_links(
        self, config: Configuration, number: int, alternate: int
    ) -> _BotLink | None:
        setting = config.alt(number, alternate)
        if (
            setting is None
            or not self.policy.bot_sizing
            or setting.interface_class != ch9.CLASS_MASS_STORAGE
            or setting.interface_protocol != 0x50
        ):
            return None
        kinds = {(e.is_in, e.transfer_type) for e in setting.endpoints}
        if {(True, ch9.XFER_BULK), (False, ch9.XFER_BULK)} <= kinds:
            return _BotLink()
        return None

    def _enable_interface(
        self, config: Configuration, number: int, alternate: int
    ) -> list[_Pump]:
        setting = config.alt(number, alternate)
        assert setting is not None
        bot = self._bot_links(config, number, alternate)
        pumps: list[_Pump] = []
        for endpoint in setting.endpoints:
            handle = self.backend.ep_enable(endpoint.raw)
            self._handles[endpoint.address] = handle
            pump: _Pump
            endpoint_bot = bot if endpoint.transfer_type == ch9.XFER_BULK else None
            if endpoint.is_in:
                pump = _InPump(self, endpoint, handle, endpoint_bot)
                if endpoint_bot is not None:
                    endpoint_bot.wake = pump.wake
            else:
                pump = _OutPump(self, endpoint, handle, endpoint_bot)
            pumps.append(pump)
        in_packets = [p.packet_size for p in pumps if isinstance(p, _InPump)]
        for pump in pumps:
            if isinstance(pump, _OutPump) and in_packets:
                pump.bot_in_packet = in_packets[0]
        return pumps

    def _stop_pumps(self, pumps: list[_Pump], deadline: float | None = None) -> None:
        if deadline is None:
            deadline = time.monotonic() + self.policy.stop_timeout
        for pump in pumps:
            pump.request_stop()
        stuck = [
            f"{p.address:#04x}"
            for p in pumps
            if p.thread.is_alive() and not self._interrupt_join(p.thread, deadline)
        ]
        if stuck:
            raise TeardownError(f"endpoint pumps did not stop: {', '.join(stuck)}")
        for pump in pumps:
            with pump.cond:
                outstanding = list(pump.inflight)
                pump.inflight.clear()
            if outstanding and self.conn.lost is None:
                pump._unlink(outstanding)
            self._pumps.pop(pump.address, None)

    def _disable_endpoints(self, addresses: list[int]) -> None:
        for address in addresses:
            handle = self._handles.pop(address, None)
            if handle is not None:
                with contextlib.suppress(GadgetError):
                    self.backend.ep_disable(handle)
            self._control.halted.discard(address)

    def _quiesce_all(self) -> None:
        self._stop_pumps(list(self._pumps.values()))
        self._disable_endpoints(list(self._handles))

    def _on_bus_reset(self, state: str) -> None:
        self.counters.bus_resets += 1
        self._quiesce_all()
        self._control.configuration = 0
        self._control.alternates = {}
        self.state = state
        self.suspended = False
        self._speed_checked = False
        if self.policy.reset_remote_on_bus_reset and self._remote_dirty:
            self._remote_dirty = False
            self._reset_done.clear()
            thread = threading.Thread(target=self._remote_reset, daemon=True)
            self._reset_threads = [t for t in self._reset_threads if t.is_alive()]
            self._reset_threads.append(thread)
            thread.start()

    def _remote_reset(self) -> None:
        setup = Setup(ch9.RT_PORT, ch9.SET_FEATURE, ch9.PORT_FEAT_RESET, 0, 0)
        try:
            self.conn.control(setup, timeout=self.policy.control_timeout)
            self.counters.remote_resets += 1
        except ConnectionLostError as exc:
            self._remote_lost(exc)
        except (UsbipError, TimeoutError) as exc:
            self._log(f"remote reset failed: {exc}")
        finally:
            self._reset_done.set()

    def _start(self, pumps: list[_Pump]) -> None:
        for pump in pumps:
            self._pumps[pump.address] = pump
            pump.thread.start()

    def _set_configuration(self, setup: Setup, value: int) -> None:
        self._quiesce_all()
        try:
            result = self._remote_control(setup)
        except (UsbipError, TimeoutError) as exc:
            if isinstance(exc, ConnectionLostError):
                raise
            self._stall(f"remote SET_CONFIGURATION failed: {exc}")
            return
        self._control.configuration = 0
        self._control.alternates = {}
        if result is None:
            self._stall("remote refused SET_CONFIGURATION")
            return
        config = self.presentation.configuration(value)
        pumps: list[_Pump] = []
        if config is not None:
            try:
                for number in sorted(config.interfaces):
                    pumps += self._enable_interface(config, number, 0)
                self.backend.vbus_draw(self.policy.power_ma)
                self.backend.configure()
            except GadgetError as exc:
                self._disable_endpoints(list(self._handles))
                self._fault(f"UDC could not configure: {exc}")
                self._stall("UDC endpoint enable failed")
                return
            self._control.configuration = value
            self._control.alternates = dict.fromkeys(config.interfaces, 0)
            self.state = "configured"
        else:
            self.state = "addressed"
        self._start(pumps)
        self._ep0(lambda: self.backend.ep0_read(0))

    def _set_interface(self, setup: Setup, number: int, alternate: int) -> None:
        config = self.presentation.configuration(self._control.configuration)
        assert config is not None
        current = config.alt(number, self._control.alternates.get(number, 0))
        old = [e.address for e in current.endpoints] if current else []
        self._stop_pumps([self._pumps[a] for a in old if a in self._pumps])
        try:
            result = self._remote_control(setup)
        except (UsbipError, TimeoutError) as exc:
            if isinstance(exc, ConnectionLostError):
                raise
            result = None
        if result is None:
            # The device keeps its previous alternate setting: restore its pumps.
            self._disable_endpoints(old)
            previous = self._control.alternates.get(number, 0)
            try:
                self._start(self._enable_interface(config, number, previous))
            except GadgetError as exc:
                self._fault(f"UDC could not restore alternate setting: {exc}")
            self._stall("remote refused SET_INTERFACE")
            return
        self._disable_endpoints(old)
        try:
            pumps = self._enable_interface(config, number, alternate)
        except GadgetError as exc:
            self._fault(f"UDC could not enable alternate setting: {exc}")
            self._stall("UDC endpoint enable failed")
            return
        self._control.alternates[number] = alternate
        self._start(pumps)
        self._ep0(lambda: self.backend.ep0_read(0))
