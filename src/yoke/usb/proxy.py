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

A remote IN STALL is reproduced exactly: the pump writes any partial data,
unlinks later URBs, halts the gadget endpoint, clears the remote halt once
and probes (parking with backoff if the device stays halted). A remote OUT
STALL reaches the computer one transaction late, because the UDC already
acknowledged the refused packet; a control OUT request *with data* is
acknowledged before the device answers, so its STALL cannot reach the
computer at all (counted as ``late_out_stalls``). The computer's own
CLEAR_FEATURE(HALT) is answered by the UDC; dwc3 surfaces it only as EPIPE
on a started request, which the pump mirrors to the device. The owner
reacts to :attr:`Proxy.ended` by calling :meth:`Proxy.stop`, which closes
the gadget so the computer sees a disconnect.
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
    control_data,
)
from yoke.usb.descriptors import Configuration, Endpoint, Presentation
from yoke.usb.gadget import (
    HEADROOM,
    EventType,
    GadgetBackend,
    GadgetError,
    GadgetEvent,
    GadgetHaltClearedError,
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
BOT_RESET_REQUEST_TYPE = 0x21  # class, interface, host to device
BOT_RESET = 0xFF  # Bulk-Only Mass Storage Reset (BOT 1.0 section 3.1)


class TeardownError(RuntimeError):
    """A proxy thread did not stop within the bound; resources are retained."""


class _Stop(Exception):  # noqa: N818 - a pump unwind signal, not an error
    """Internal: unwind a pump."""


class _Abandoned(Exception):  # noqa: N818 - a control-flow unwind, not an error
    """Internal: a gadget event ended the control request being handled.

    ``bus`` is set for a bus reset or disconnect, which leaves a SETUP that
    has not been answered on ep0 owed to Raw Gadget.
    """

    def __init__(self, *, bus: bool) -> None:
        super().__init__("control request abandoned")
        self.bus = bus


@dataclass
class Counters:
    control: int = 0
    cached: int = 0
    forwarded: int = 0
    local_stalls: int = 0
    remote_stalls: int = 0
    remote_clears: int = 0
    host_clear_halts: int = 0
    parked: int = 0
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
    """Bulk-Only Transport transfer sizes shared by an interface's pumps.

    ``in_sizes`` are the IN URB sizes the IN pump submits next (data, then
    the CSW); ``out_sizes`` are the OUT pump's planned data-out reads. A CBW
    plans both. :meth:`discard` ends a plan that a data-out STALL, the
    host's clear-halt or a Bulk-Only Mass Storage Reset cut short, so the
    next 31-byte CBW is parsed instead of read as data.
    """

    def __init__(self, interface: int) -> None:
        self.interface = interface
        self.lock = threading.Lock()
        self.in_sizes: deque[int] = deque()
        self.out_sizes: deque[int] = deque()
        self.epoch = 0  # bumped by discard(); a read planned before it is stale
        self.wake: Callable[[], None] = lambda: None

    def plan_out(self, sizes: list[int]) -> None:
        with self.lock:
            self.out_sizes.extend(sizes)

    def pop_out(self) -> tuple[int | None, int]:
        """The next planned data-out read size (or None) and the plan epoch."""
        with self.lock:
            size = self.out_sizes.popleft() if self.out_sizes else None
            return size, self.epoch

    def current(self, epoch: int) -> bool:
        with self.lock:
            return self.epoch == epoch

    def discard(self) -> None:
        """Drop planned data-out reads; keep only the final IN size (the CSW)."""
        with self.lock:
            self.out_sizes.clear()
            while len(self.in_sizes) > 1:
                self.in_sizes.popleft()
            self.epoch += 1

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
        self.kicked = False
        self.remote_halted = False  # remote stalled; no success since
        self.cond = threading.Condition()
        self.inflight: deque[Urb] = deque()
        self.sizes: dict[int, int] = {}
        name = f"usb-ep{self.address:02x}"
        self.thread = threading.Thread(target=self._main, name=name, daemon=True)

    def wake(self, _urb: Urb | None = None) -> None:
        with self.cond:
            self.cond.notify_all()

    def kick(self) -> None:
        """Host activity that may have released a device-side halt."""
        with self.cond:
            self.kicked = True
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

    def _cancel(self, urbs: list[Urb]) -> None:
        """Unlink URBs and wait (bounded) until every one is resolved."""
        try:
            self.proxy.conn.cancel(urbs, timeout=self.proxy.policy.stop_timeout)
        except TimeoutError as exc:
            raise TeardownError(
                f"endpoint {self.address:#04x}: unlink unconfirmed"
            ) from exc

    def _take_inflight(self) -> list[Urb]:
        with self.cond:
            urbs = list(self.inflight)
            self.inflight.clear()
        return urbs

    def _clear_remote(self) -> None:
        """CLEAR_FEATURE(ENDPOINT_HALT) on the device (stub: usb_clear_halt).

        Called only while no URB of this endpoint is in flight, because Linux
        resets the host endpoint state when it clears a halt.
        """
        clear = Setup(
            ch9.RECIP_ENDPOINT,
            ch9.CLEAR_FEATURE,
            ch9.FEATURE_ENDPOINT_HALT,
            self.address,
        )
        self.proxy.counters.remote_clears += 1
        if (
            self.proxy.conn.control(clear, timeout=self.proxy.policy.control_timeout)
            is None
        ):
            self.proxy._fault(f"remote refused to clear halt on {self.address:#04x}")

    def _local_halt(self) -> None:
        try:
            self.proxy.backend.ep_set_halt(self.handle)
        except GadgetError as exc:
            if self.stopping:
                raise _Stop from exc
            self.proxy._fault(f"halt {self.address:#04x}: {exc}")
        else:
            self.proxy._control.set_halted(self.address, True)
        self.proxy.counters.local_stalls += 1

    def _host_cleared(self) -> None:
        """The UDC returned a started request with EPIPE: the host cleared
        this endpoint's halt (dwc3). Mirror it on the device, then retry."""
        if self.bot is not None and not self.endpoint.is_in:
            self.bot.discard()
        self.proxy._control.set_halted(self.address, False)
        self.proxy.counters.host_clear_halts += 1
        later = self._take_inflight()
        self._cancel(later)
        self._requeue(later)
        self._clear_remote()
        self.remote_halted = False

    def _requeue(self, urbs: list[Urb]) -> None:
        sizes = [self.sizes.pop(id(u), self.size) for u in urbs]
        if self.bot is not None and self.endpoint.is_in and urbs:
            self.bot.requeue_in(sizes)

    def _io(self, call: Callable[[], int], what: str) -> int:
        while True:
            try:
                return call()
            except GadgetInterruptedError as exc:
                if self.stopping:
                    raise _Stop from exc
            except GadgetHaltClearedError:
                if self.stopping:
                    raise _Stop from None
                self._host_cleared()
            except GadgetShutdownError as exc:
                raise _Stop from exc
            except GadgetError as exc:
                self.proxy._fault(f"{what} {self.address:#04x}: {exc}")
                raise _Stop from exc


class _InPump(_Pump):
    """Device-to-computer: prefetch URBs, write completions in order.

    After a remote STALL the pump halts the gadget endpoint, clears the
    remote halt once and probes with a single URB. A device that stalls the
    probe too is left alone ("parked") until host activity (a forwarded
    control request or OUT traffic) or an exponential backoff (≤ 2 s), so a
    device that stays halted until a class reset cannot cause a submit storm.
    """

    PARK_MIN = 0.05
    PARK_MAX = 2.0

    def __init__(
        self, proxy: Proxy, endpoint: Endpoint, handle: int, bot: _BotLink | None
    ) -> None:
        super().__init__(proxy, endpoint, handle, bot)
        self.probing = False
        self.parked_until: float | None = None
        self.backoff = self.PARK_MIN

    def _next_size(self) -> int | None:
        if self.bot is not None:
            return self.bot.pop_in()
        return self.size

    def _can_submit(self, depth: int) -> bool:
        """A BOT size hint arrived while the queue has room (no lost wakeup)."""
        return (
            self.parked_until is None
            and self.bot is not None
            and bool(self.bot.in_sizes)
            and len(self.inflight) < depth
        )

    def _write(self, buf: bytearray, length: int, zero: bool) -> None:
        backend = self.proxy.backend
        self._io(lambda: backend.ep_write(self.handle, buf, length, zero), "write")
        counters = self.proxy.counters
        counters.gadget_writes += 1
        counters.in_bytes += length

    def _unpark(self) -> None:
        """Wait while parked; then clear the remote halt and probe again."""
        with self.cond:
            while not self.stopping and not self.kicked:
                assert self.parked_until is not None
                remaining = self.parked_until - time.monotonic()
                if remaining <= 0:
                    break
                self.cond.wait(remaining)
            if self.stopping:
                raise _Stop
            self.kicked = False
            self.parked_until = None
        self._clear_remote()
        self.probing = True

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
            if self.parked_until is not None:
                self._unpark()
            limit = 1 if self.probing else depth
            batch: list[Submission] = []
            while len(self.inflight) + len(batch) < limit and not self.stopping:
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
                    and not self._can_submit(limit)
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
            if urb.result.unlinked or status in CANCELLED:
                self._requeue([urb])
                if self.stopping:
                    raise _Stop
                index += 1
                continue
            if status == 0:
                run: list[tuple[int, int]] = []
                for item in done[index:] if policy.coalesce_in else [urb]:
                    if (
                        item.result is None
                        or item.result.status != 0
                        or item.result.unlinked
                    ):
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
                self.probing = False
                self.remote_halted = False
                self.backoff = self.PARK_MIN
                self._write(buf, total, zero)
                index += count
                continue
            self.sizes.pop(id(urb), None)
            if status in GONE:
                raise ConnectionLostError(f"device gone (status {status})")
            # STALL (or a transaction error, reported to the computer as STALL).
            counters = self.proxy.counters
            if status == -usbip.EPIPE:
                counters.remote_stalls += 1
            else:
                counters.transaction_errors += 1
            if urb.result.actual_length:
                assert urb.result.buffer is not None
                self._write(urb.result.buffer, urb.result.actual_length, False)
            later = done[index + 1 :] + self._take_inflight()
            self._cancel(later)
            self._requeue([urb, *later] if self.bot is not None else later)
            self._local_halt()
            if self.remote_halted and not self.kicked:
                # The device stalled again right after we cleared it: it is
                # waiting for something only the host can do. Park.
                self.parked_until = time.monotonic() + self.backoff
                self.backoff = min(self.backoff * 2, self.PARK_MAX)
                counters.parked += 1
            else:
                with self.cond:
                    self.kicked = False
                self._clear_remote()
                self.remote_halted = True
                self.probing = True
            return


class _OutPump(_Pump):
    """Computer-to-device: read from the gadget, submit in order.

    After a remote STALL the remote halt is cleared immediately and again
    before each later transfer until one succeeds, so a device that keeps its
    halt until a class reset (BOT) is released once the host has done it.
    """

    def __init__(
        self, proxy: Proxy, endpoint: Endpoint, handle: int, bot: _BotLink | None
    ) -> None:
        super().__init__(proxy, endpoint, handle, bot)
        self.bot_in_packet = 512

    def _read(self, buf: bytearray, size: int) -> int:
        backend = self.proxy.backend
        count = self._io(lambda: backend.ep_read(self.handle, buf, size), "read")
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
                if urb.result.status != 0 or urb.result.unlinked:
                    failed = True
                    break
                self.remote_halted = False
        return done, failed

    def _handle_failure(self, urb: Urb) -> None:
        assert urb.result is not None
        status = urb.result.status
        if status in GONE:
            raise ConnectionLostError(f"device gone (status {status})")
        if urb.result.unlinked or status in CANCELLED:
            if self.stopping:
                raise _Stop
            return
        counters = self.proxy.counters
        if status == -usbip.EPIPE:
            counters.remote_stalls += 1
        else:
            counters.transaction_errors += 1
        later = self._take_inflight()
        if self.proxy.policy.out_window > 1:
            counters.late_out_stalls += 1
        self._cancel(later)
        self._local_halt()
        if self.bot is not None:
            self.bot.discard()  # the command's data phase is over
        self._clear_remote()
        self.remote_halted = True

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
            planned_size, epoch = (
                self.bot.pop_out() if self.bot is not None else (None, 0)
            )
            size = self.size if planned_size is None else planned_size
            count = self._read(buf, size)
            # A plan discarded during the read (reset, clear-halt) is stale.
            planned = (
                planned_size is not None
                and self.bot is not None
                and self.bot.current(epoch)
            )
            with self.cond:
                late = any(
                    u.done and u.result and u.result.status for u in self.inflight
                )
            if late:
                # Deferred-stall semantics (out_window > 1): the UDC already
                # acknowledged this data; the device would have refused it.
                self.proxy.counters.dropped_out += 1
                continue
            if self.remote_halted and not self.inflight:
                self._clear_remote()  # the host has cleared; mirror it first
            if self.bot is not None and count == 31 and not planned:
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
            self.proxy._kick_parked()

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
            self.bot.plan_out(chunk_sizes(length, limit))
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
        self._stop_lock = threading.Lock()
        # The fetch thread only fetches gadget events; the ep0 thread handles
        # them, so a bus event can abandon a request it is still forwarding.
        self._event_thread = threading.Thread(
            target=self._fetch_main, name="usb-gadget-events", daemon=True
        )
        self._ep0_thread = threading.Thread(
            target=self._event_main, name="usb-gadget-ep0", daemon=True
        )
        self._events: deque[GadgetEvent] = deque()
        self._event_cond = threading.Condition()
        self._fetch_ended = False
        self._owed = False  # a SETUP taken but not yet answered on ep0
        self._must_finish = False  # SET_CONFIGURATION/SET_INTERFACE in progress
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
        self._ep0_thread.start()

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

    def _interrupt_join(self, thread: threading.Thread, timeout: float) -> bool:
        """Signal ``thread`` every 20 ms until it leaves a blocking call.

        A signal that arrives while the thread is not inside the ioctl is
        lost, so it is repeated. Each thread gets its own ``timeout``.
        """
        deadline = time.monotonic() + timeout
        while thread.is_alive() and time.monotonic() < deadline:
            ident = thread.ident
            if ident is not None:
                with contextlib.suppress(OSError, GadgetError):
                    self.backend.interrupt(ident)
            thread.join(0.02)
        return not thread.is_alive()

    def stop(self) -> None:
        """Confirmed teardown; raise :class:`TeardownError` if unconfirmed.

        Idempotent and serialized: concurrent callers share one teardown.
        """
        with self._stop_lock:
            if self.state == "stopped":
                return
            self._stop_locked()

    def _stop_locked(self) -> None:
        self._stopping = True
        self._end("stopped")
        # Close the USB/IP connection first, as the native worker does: the
        # stub kills every URB when its socket closes, so teardown never
        # waits for one RET_UNLINK per outstanding URB (which a busy or stuck
        # stub may not send in time).
        self.conn.close()
        with self._event_cond:
            self._event_cond.notify_all()
        timeout = self.policy.stop_timeout
        problems = []
        for thread, name in (
            (self._ep0_thread, "ep0 thread"),
            (self._event_thread, "event thread"),
        ):
            if thread.is_alive() and not self._interrupt_join(thread, timeout):
                problems.append(name)
        try:
            self._stop_pumps(list(self._pumps.values()))
        except TeardownError as exc:
            problems.append(str(exc))
        for thread in self._reset_threads:
            thread.join(timeout)
            if thread.is_alive():
                problems.append("remote reset thread")
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

    def _fetch_main(self) -> None:
        """Fetch gadget events into the queue the ep0 thread handles."""
        backend = self.backend
        try:
            while not self._stopping:
                try:
                    event = backend.fetch_event()
                except GadgetInterruptedError:
                    continue
                except GadgetError as exc:
                    if not self._stopping:
                        self._end(f"gadget event error: {exc}", error=True)
                    return
                with self._event_cond:
                    self._events.append(event)
                    self._event_cond.notify_all()
        finally:
            with self._event_cond:
                self._fetch_ended = True
                self._event_cond.notify_all()

    def _event_main(self) -> None:
        while True:
            with self._event_cond:
                while not (self._events or self._stopping or self._fetch_ended):
                    self._event_cond.wait()
                if self._stopping or not self._events:
                    return
                event = self._events.popleft()
            try:
                self._dispatch(event)
            except _Abandoned as abandoned:
                # Raw Gadget keeps an unanswered SETUP pending across bus
                # reset and disconnect, stalling every later SETUP: release it.
                if abandoned.bus and self._owed and not self._stopping:
                    self._stall("bus reset or disconnect abandoned the request")
            except TeardownError as exc:
                self._end(str(exc), error=True)
                return
            except ConnectionLostError as exc:
                self._remote_lost(exc)
            finally:
                self._owed = False
                self._must_finish = False

    def _dispatch(self, event: GadgetEvent) -> None:
        if event.type is EventType.CONTROL:
            assert event.setup is not None
            self._owed = True
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

    def _notify_events(self, _urb: Urb | None = None) -> None:
        with self._event_cond:
            self._event_cond.notify_all()

    def _await(self, ready: Callable[[], bool], timeout: float) -> bool:
        """Wait (ep0 thread) until ``ready()``; False on timeout.

        Raises :class:`_Abandoned` when a queued event ends the request: a
        bus reset or disconnect, or a new SETUP while this one is still owed
        its ep0 answer (the host gave up on it), except for SET_CONFIGURATION
        and SET_INTERFACE. Any other flow finishes first (an OUT request whose
        data was read must still reach the device), and the new SETUP is
        handled next, in arrival order (proxy.c e0_control).
        """
        deadline = time.monotonic() + timeout
        with self._event_cond:
            while not ready():
                if self._stopping:
                    raise _Abandoned(bus=False)
                kinds = {e.type for e in self._events}
                if kinds & {EventType.RESET, EventType.DISCONNECT}:
                    raise _Abandoned(bus=True)
                if EventType.CONTROL in kinds and self._owed and not self._must_finish:
                    raise _Abandoned(bus=False)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._event_cond.wait(remaining)
        return True

    def _ep0(self, action: Callable[[], object]) -> None:
        self._owed = False
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
        if (
            setup.request_type == BOT_RESET_REQUEST_TYPE
            and setup.request == BOT_RESET
            and setup.length == 0
        ):
            # Bulk-Only Mass Storage Reset: the command in progress is gone.
            for link in {p.bot for p in self._pumps.values() if p.bot is not None}:
                if link.interface == setup.index & 0xFF:
                    link.discard()
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
            if not decision.halt:
                self._control.set_halted(decision.address, False)
            self._forward(setup, False)

    def _gate(self) -> None:
        """Wait for an in-flight remote reset before touching the device."""
        self._await(self._reset_done.is_set, self.policy.control_timeout)
        self._remote_dirty = True

    def _remote_control(self, setup: Setup, data: bytes = b"") -> bytes | None:
        """Forward one control request; a bus event abandons the wait.

        An abandoned URB is left to complete (or die with the connection)
        and its answer is ignored, as the native worker orphans it.
        """
        self._gate()
        self.counters.forwarded += 1
        try:
            timeout = self.policy.control_timeout
            urb = self.conn.submit_control(setup, data, callback=self._notify_events)
            if not self._await(lambda: urb.done, timeout):
                with contextlib.suppress(UsbipError, TimeoutError):
                    self.conn.cancel([urb], timeout=timeout)
                raise TimeoutError(f"URB {urb.seqnum} timed out")
            if urb.error is not None:
                raise urb.error
            assert urb.result is not None
            return control_data(urb.result)
        finally:
            self._kick_parked()

    def _kick_parked(self) -> None:
        """Host activity may have released a device-side halt (e.g. BOT reset)."""
        for pump in list(self._pumps.values()):
            if isinstance(pump, _InPump) and pump.parked_until is not None:
                pump.kick()

    def _forward(self, setup: Setup, cache: bool) -> None:
        data = b""
        if not setup.is_in and setup.length:
            self._owed = False  # the UDC completes the status stage with the data
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
            return _BotLink(number)
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

    def _stop_pumps(self, pumps: list[_Pump]) -> None:
        for pump in pumps:
            pump.request_stop()
        timeout = self.policy.stop_timeout
        stuck = [
            f"{p.address:#04x}"
            for p in pumps
            if p.thread.is_alive() and not self._interrupt_join(p.thread, timeout)
        ]
        if stuck:
            raise TeardownError(f"endpoint pumps did not stop: {', '.join(stuck)}")
        for pump in pumps:
            with pump.cond:
                outstanding = list(pump.inflight)
                pump.inflight.clear()
            if outstanding and self.conn.lost is None:
                pump._cancel(outstanding)
            self._pumps.pop(pump.address, None)

    def _disable_endpoints(self, addresses: list[int]) -> None:
        for address in addresses:
            handle = self._handles.pop(address, None)
            if handle is not None:
                with contextlib.suppress(GadgetError):
                    self.backend.ep_disable(handle)
            self._control.set_halted(address, False)

    def _quiesce_all(self) -> None:
        self._stop_pumps(list(self._pumps.values()))
        self._disable_endpoints(list(self._handles))

    def _on_bus_reset(self, state: str) -> None:
        self.counters.bus_resets += 1
        self._quiesce_all()
        self._control.reset()
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
            self._notify_events()

    def _start(self, pumps: list[_Pump]) -> None:
        for pump in pumps:
            self._pumps[pump.address] = pump
            pump.thread.start()

    def _set_configuration(self, setup: Setup, value: int) -> None:
        self._must_finish = True
        self._quiesce_all()
        try:
            result = self._remote_control(setup)
        except (UsbipError, TimeoutError) as exc:
            if isinstance(exc, ConnectionLostError):
                raise
            self._stall(f"remote SET_CONFIGURATION failed: {exc}")
            return
        self._control.reset()
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
        self._must_finish = True
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
