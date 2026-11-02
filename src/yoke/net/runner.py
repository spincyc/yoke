"""Asyncio shell: turns a Node + a Driver + config into a running agent.

The sans-I/O core (``yoke.core.node``) makes every decision; this module
only performs its effects (ARCHITECTURE.md "Sans-I/O core"). It is meant
to stay thin enough to review by eye:

- a receive loop decodes datagrams and feeds the node,
- a timer loop calls ``node.poll`` around ``node.next_deadline()``,
- a driver poll loop reports media-plane health,
- an effect executor sends frames, persists state, and drives the media
  plane (on a dedicated worker, so a slow device call never blocks
  the event loop).

Everything the class touches is an injected seam; only
:func:`run_agent` / :func:`run_agent_async` construct real sockets,
drivers, and clocks.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import math
import signal
import socket
import sys
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from functools import partial
from pathlib import Path
from typing import Protocol, assert_never

from yoke import statefile
from yoke.config import AgentConfig, ConfigError
from yoke.core import frames
from yoke.core.frames import EncodeError, FrameDrop
from yoke.core.node import Node
from yoke.core.types import (
    AttachMedia,
    DetachMedia,
    DriverStatus,
    Effect,
    Kind,
    Log,
    NodeState,
    Persist,
    PersistedState,
    Role,
    SendFrame,
    SendRaw,
)
from yoke.drivers.base import Driver, DriverError
from yoke.drivers.igmp import IgmpJoinDriver
from yoke.drivers.jap import JapDecoderDriver, JapEncoderConfig, telnet_link_factory
from yoke.drivers.shellcmd import SubprocessRunner
from yoke.drivers.sim import SimDriver
from yoke.drivers.stream import StreamDriver, SubprocessSupervisor
from yoke.drivers.usbip import UsbipAttachDriver, UsbipBindDriver
from yoke.net.executor import SerialExecutor
from yoke.net.mcast import open_multicast_socket
from yoke.net.notify import SystemdNotifier
from yoke.statefile import StateError

_log = logging.getLogger("yoke.agent")

#: Cap on one timer-loop sleep so a changed ``next_deadline()`` (new peer,
#: rescheduled retry) is noticed promptly even mid-wait.
MAX_TIMER_SLEEP = 0.5
#: How often the driver's health is polled and reported to the node.
DRIVER_POLL_INTERVAL = 1.0
#: Receive buffer: frames cap at MAX_FRAME_BYTES (1200); anything larger
#: is truncated here and dropped as ``oversize`` by the decoder anyway.
_RECV_BUFSIZE = 2048

RecvFn = Callable[[], Awaitable[tuple[bytes, tuple[str, int]]]]
SaveStateFn = Callable[[Path, PersistedState], None]


class Transport(Protocol):
    """The one-way send seam the runner needs (destination pre-bound)."""

    def sendto(self, raw: bytes) -> None: ...


# --- driver factory ---------------------------------------------------------


def build_driver(cfg: AgentConfig) -> Driver:
    """Construct the media-plane driver named by ``cfg.driver["type"]``.

    Raises :class:`~yoke.config.ConfigError` naming the offending key on
    an unknown type or a missing/invalid driver-specific key.
    """
    table = cfg.driver
    driver_type = table.get("type")
    if not isinstance(driver_type, str) or not driver_type:
        raise ConfigError("driver.type: missing")
    if driver_type == "sim":
        media = table.get("media")
        if media is not None and not isinstance(media, dict):
            raise ConfigError(f"driver.media: expected a table, got {media!r}")
        return SimDriver(media=media)
    if driver_type == "igmp":
        return IgmpJoinDriver()
    if driver_type == "usbip":
        return _build_usbip(cfg, table)
    if driver_type == "jap":
        return _build_jap(cfg, table)
    if driver_type == "stream":
        if cfg.node.role is not Role.CONSUMER or cfg.node.kind is not Kind.HDMI:
            raise ConfigError("driver.type: stream requires an hdmi consumer")
        timeout = table.get("stop_timeout", 5.0)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ConfigError("driver.stop_timeout: expected a finite positive number")
        try:
            timeout_seconds = float(timeout)
        except OverflowError as exc:
            raise ConfigError("driver.stop_timeout: number is too large") from exc
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ConfigError("driver.stop_timeout: expected a finite positive number")
        return StreamDriver(SubprocessSupervisor(), stop_timeout=timeout_seconds)
    raise ConfigError(f"driver.type: unknown driver type {driver_type!r}")


def _build_usbip(cfg: AgentConfig, table: dict[str, object]) -> Driver:
    if cfg.node.role is Role.CONSUMER:
        return UsbipAttachDriver(
            SubprocessRunner(),
            ownership_file=cfg.state_file.with_name(
                cfg.state_file.name + ".usbip.json"
            ),
        )
    host = table.get("host")
    if not isinstance(host, str) or not host:
        raise ConfigError("driver.host: required for a usbip producer")
    port = table.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
        raise ConfigError("driver.port: required for a usbip producer")
    raw_busids = table.get("busids")
    if not isinstance(raw_busids, list) or not raw_busids:
        raise ConfigError("driver.busids: required for a usbip producer")
    busids = [b for b in raw_busids if isinstance(b, str) and b]
    if len(busids) != len(raw_busids):
        raise ConfigError("driver.busids: entries must be non-empty strings")
    try:
        return UsbipBindDriver(SubprocessRunner(), host=host, port=port, busids=busids)
    except DriverError as exc:
        raise ConfigError(f"driver: {exc}") from exc


def _build_jap(cfg: AgentConfig, table: dict[str, object]) -> Driver:
    if cfg.node.role is Role.CONSUMER:
        decoder_host = table.get("decoder_host")
        if not isinstance(decoder_host, str) or not decoder_host:
            raise ConfigError("driver.decoder_host: required for a jap consumer")
        return JapDecoderDriver(decoder_host, telnet_link_factory)
    # JAP encoders need no runtime driving — they always transmit on their
    # channel. An announce-only warden with the derived media dict is the
    # honest producer shape, and SimDriver provides exactly that.
    channel = table.get("channel")
    if not isinstance(channel, int) or isinstance(channel, bool):
        raise ConfigError("driver.channel: required for a jap producer")
    try:
        media = JapEncoderConfig(channel).media()
    except DriverError as exc:
        raise ConfigError(f"driver.channel: {exc}") from exc
    return SimDriver(media=media)


# --- the runner -------------------------------------------------------------


class AgentRunner:
    """Execute a core's effects while preserving their actual issue order.

    Core inputs and their effect batches are serialized on the event-loop
    thread. All operations on the driver share one real worker; cancelling
    an asyncio waiter cannot release that worker before device I/O ends.
    """

    def __init__(
        self,
        node: Node,
        *,
        transport: Transport,
        recv: RecvFn,
        driver: Driver,
        psk: bytes,
        state_path: Path,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        save_state: SaveStateFn = statefile.save,
        max_sleep: float = MAX_TIMER_SLEEP,
        driver_poll_interval: float = DRIVER_POLL_INTERVAL,
        driver_executor: SerialExecutor | None = None,
        on_ready: Callable[[], None] | None = None,
    ) -> None:
        if not math.isfinite(max_sleep) or max_sleep <= 0:
            raise ValueError("max_sleep must be finite and positive")
        if not math.isfinite(driver_poll_interval) or driver_poll_interval <= 0:
            raise ValueError("driver_poll_interval must be finite and positive")
        self._node = node
        self._node_id = node.node_id
        self._transport = transport
        self._recv = recv
        self._driver = driver
        self._psk = psk
        self._state_path = state_path
        self._clock = clock
        self._wall_clock = wall_clock
        self._save_state = save_state
        self._max_sleep = max_sleep
        self._driver_poll_interval = driver_poll_interval
        self._core_lock = asyncio.Lock()
        self._driver_executor = driver_executor or SerialExecutor()
        self._on_ready = on_ready
        self._state_executor = SerialExecutor("yoke-state")
        self._driver_task: asyncio.Task[None] | None = None
        self._driver_wakeup = asyncio.Event()
        self._pending_detach = False
        self._pending_detach_generation = 0
        self._pending_attach: AttachMedia | None = None
        self._driver_busy = False
        self._driver_generation = 0
        self._generation = 0
        self._starting = False
        self._started = False
        self._closing = False
        self._producer_started = False
        self._producer_bind_task: asyncio.Task[None] | None = None

    async def run(self) -> None:
        """Run until cancelled, then drain device work and quiesce media."""
        try:
            self._starting = True
            await self._apply(self._node.start)
            self._starting = False
            self._started = True
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._receive_loop(), name="yoke-recv")
                tg.create_task(self._timer_loop(), name="yoke-timer")
                tg.create_task(self._driver_poll_loop(), name="yoke-driver-poll")
                if self._on_ready is not None:
                    self._on_ready()
        finally:
            self._starting = False
            self._closing = True
            self._generation += 1
            self._pending_attach = None
            self._pending_detach = False
            self._driver_wakeup.set()
            cleanup = asyncio.create_task(self._shutdown(), name="yoke-shutdown")
            # A repeated signal must not let an actual worker outlive the
            # agent. Python threads cannot safely be interrupted mid-call.
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
            await cleanup

    def _begin_producer_bind(self) -> None:
        hook = getattr(self._driver, "bind", None)
        if hook is None:
            return
        # Export readiness is hardware health. Keep the local control
        # runtime responsive while this bounded device operation runs.
        future = self._driver_executor.submit(partial(self._bind_status, hook))
        self._producer_bind_task = asyncio.create_task(
            self._finish_producer_bind(future), name="yoke-producer-bind"
        )

    def _bind_status(self, hook: Callable[[], None]) -> DriverStatus:
        try:
            hook()
        except Exception as exc:
            return DriverStatus(NodeState.ERROR, f"bind: {exc}")
        return self._poll_status()

    async def _finish_producer_bind(self, future: asyncio.Future[DriverStatus]) -> None:
        status = await asyncio.shield(future)
        await self._publish_driver(status, 0)

    async def _producer_lifecycle(self, phase: str) -> DriverStatus | None:
        hook = getattr(self._driver, phase, None)
        if hook is None:
            return None
        try:
            await self._driver_executor.call(hook)
        except Exception as exc:
            return DriverStatus(NodeState.ERROR, f"{phase}: {exc}")
        return None

    async def _apply(self, call: Callable[[float], Iterable[Effect]]) -> None:
        async with self._core_lock:
            await self._execute(call(self._clock()))

    # -- effect execution ----------------------------------------------------

    async def execute(self, effects: Iterable[Effect]) -> None:
        """Perform an externally supplied effect batch in issue order."""
        async with self._core_lock:
            await self._execute(effects)

    async def _execute(self, effects: Iterable[Effect]) -> None:
        pending = deque(effects)
        while pending:
            effect = pending.popleft()
            if isinstance(effect, SendFrame):
                self._send_frame(effect)
            elif isinstance(effect, SendRaw):
                self._send_raw(effect.raw)
            elif isinstance(effect, Persist):
                success = await self._persist(effect.state)
                continuation = self._node.on_persist_result(
                    effect.request_id, success, self._clock()
                )
                if not success:
                    if self._starting:
                        raise StateError(
                            f"cannot persist startup state to {self._state_path}"
                        )
                    # A failed save cannot release any trailing success
                    # effects, even if an invalid batch included them.
                    pending.clear()
                elif self._starting and not self._producer_started:
                    self._producer_started = True
                    self._begin_producer_bind()
                pending.extendleft(reversed(continuation))
            elif isinstance(effect, (AttachMedia, DetachMedia)):
                self._queue_driver(effect)
            elif isinstance(effect, Log):
                _log.info("%s", effect.message)
            else:
                assert_never(effect)

    def _send_raw(self, raw: bytes) -> None:
        try:
            self._transport.sendto(raw)
        except OSError as exc:
            _log.warning("multicast send failed: %s", exc)

    def _send_frame(self, effect: SendFrame) -> None:
        try:
            raw = frames.encode(
                effect.type,
                self._node_id,
                effect.seq,
                self._wall_clock(),
                effect.body,
                self._psk,
            )
        except EncodeError:
            _log.exception(
                "BUG: dropping unencodable %s frame (seq %d)",
                effect.type.value,
                effect.seq,
            )
            return
        self._send_raw(raw)

    async def _persist(self, state: PersistedState) -> bool:
        try:
            await self._state_executor.call(
                partial(self._save_state, self._state_path, state)
            )
        except Exception:
            _log.exception(
                "persist to %s failed; transition withheld", self._state_path
            )
            return False
        return True

    def _queue_driver(self, effect: AttachMedia | DetachMedia) -> None:
        self._generation += 1
        if isinstance(effect, DetachMedia):
            # An unpair supersedes pending attaches, while a later pair
            # may replace the target without removing this barrier.
            self._pending_detach = True
            self._pending_detach_generation = self._generation
            self._pending_attach = None
        else:
            self._pending_attach = effect
        self._driver_wakeup.set()
        if self._driver_task is None or self._driver_task.done():
            self._driver_task = asyncio.create_task(
                self._drive_pending(), name="yoke-driver"
            )

    async def _drive_pending(self) -> None:
        while True:
            if (
                self._closing
                and not self._pending_detach
                and self._pending_attach is None
            ):
                return
            await self._driver_wakeup.wait()
            self._driver_wakeup.clear()
            while self._pending_detach or self._pending_attach is not None:
                detach = self._pending_detach
                if detach:
                    self._pending_detach = False
                    generation = self._pending_detach_generation
                    call = self._driver.detach
                else:
                    attach = self._pending_attach
                    assert attach is not None
                    self._pending_attach = None
                    generation = self._generation
                    call = partial(self._driver.attach, attach.producer, attach.media)
                self._driver_busy = True
                try:
                    completed, status = await self._driver_executor.call(
                        partial(self._command_status, call)
                    )
                finally:
                    self._driver_busy = False
                if completed:
                    self._driver_generation = generation
                if detach and status.state is not NodeState.IDLE:
                    # Preserve teardown before the latest route. A timer
                    # retry or a newer desired route wakes the same task;
                    # never spin or attach past an uncertain teardown.
                    if status.state is not NodeState.ERROR:
                        status = DriverStatus(
                            NodeState.ERROR,
                            status.detail or "driver teardown is not confirmed idle",
                        )
                    if not self._closing:
                        self._pending_detach = True
                    await self._publish_driver(status, self._generation, barrier=True)
                    if self._closing:
                        return
                    break
                await self._publish_driver(status, generation)

    def _command_status(self, call: Callable[[], None]) -> tuple[bool, DriverStatus]:
        try:
            call()
        except Exception as exc:
            return False, DriverStatus(NodeState.ERROR, str(exc))
        # A failed query after successful application still belongs to
        # the new route. A failed command may have left the old route
        # alive, whose later polls must retain their old generation.
        return True, self._poll_status()

    async def _publish_driver(
        self, status: DriverStatus, generation: int, *, barrier: bool = False
    ) -> None:
        async with self._core_lock:
            if self._closing:
                if status.state is NodeState.ERROR:
                    _log.error(
                        "driver operation failed during shutdown: %s", status.detail
                    )
            elif generation == self._generation and (
                barrier
                or (
                    not self._driver_busy
                    and not self._pending_detach
                    and self._pending_attach is None
                )
            ):
                await self._execute(self._node.on_driver_status(status, self._clock()))

    # -- loops ---------------------------------------------------------------

    async def _receive_loop(self) -> None:
        muted = False
        while True:
            try:
                raw, _addr = await self._recv()
            except OSError as exc:
                if not muted:
                    _log.warning(
                        "multicast receive failed (suppressing repeats): %s", exc
                    )
                    muted = True
                await asyncio.sleep(min(self._max_sleep, 0.5))
                continue
            muted = False
            try:
                frame = frames.decode(raw, self._psk)
            except FrameDrop as drop:
                await self._apply(partial(self._node.on_drop, drop.reason))
            else:
                await self._apply(partial(self._node.on_frame, frame, raw))

    async def _timer_loop(self) -> None:
        while True:
            delay = self._node.next_deadline() - self._clock()
            await asyncio.sleep(min(max(delay, 0.0), self._max_sleep))
            await self._apply(self._node.poll)

    async def _driver_poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self._driver_poll_interval)
            generation = self._driver_generation
            status = await self._driver_executor.call(self._poll_status)
            await self._publish_driver(status, generation)

    def _poll_status(self) -> DriverStatus:
        try:
            return self._driver.poll()
        except Exception as exc:
            return DriverStatus(NodeState.ERROR, str(exc))

    # -- shutdown ------------------------------------------------------------

    async def _shutdown(self) -> None:
        try:
            if self._started:
                try:
                    await self._apply(self._node.shutdown)
                except Exception:
                    _log.exception("failed to execute shutdown effects")
            self._driver_wakeup.set()
            if self._driver_task is not None:
                await self._driver_task
            if self._producer_bind_task is not None:
                await self._producer_bind_task
            if self._producer_started:
                status = await self._producer_lifecycle("unbind")
                if status is not None:
                    _log.error("producer shutdown failed: %s", status.detail)
        finally:
            await self._driver_executor.close()
            await self._state_executor.close()


# --- real-world wiring ------------------------------------------------------


class _SocketTransport:
    """Bind ``sendto`` to the control group destination.

    Send errors are swallowed: an interface flap or a full send buffer
    (ENETUNREACH, ENOBUFS, EPERM) must not crash the agent — the protocol
    is loss-tolerant (announce/CLI retries, anti-entropy). Repeated
    failures are logged once until a send succeeds again, so an outage
    does not flood the journal (and, under the shipped systemd unit,
    does not trip the restart-rate limiter).
    """

    def __init__(self, sock: socket.socket, dest: tuple[str, int]) -> None:
        self._sock = sock
        self._dest = dest
        self._muted = False

    def sendto(self, raw: bytes) -> None:
        try:
            self._sock.sendto(raw, self._dest)
        except OSError as exc:
            if not self._muted:
                _log.warning("multicast send failed (suppressing repeats): %s", exc)
                self._muted = True
            return
        self._muted = False


def _stable_seed(node_id: str) -> int:
    """Deterministic per-node jitter seed (PROTOCOL.md: seeded per node)."""
    return int.from_bytes(hashlib.sha256(node_id.encode("utf-8")).digest()[:8], "big")


async def run_agent_async(cfg: AgentConfig) -> None:
    """Hold the state lease through startup, operation, and worker drain.

    An unusable or uncommissioned state file, a conflicting writer, or a
    failed startup reservation exits with operator-facing status 4.
    """
    notifier = SystemdNotifier.capture()
    try:
        with statefile.lock(cfg.state_file):
            await _run_agent_locked(cfg, notifier)
    except StateError as exc:
        print(f"yoke agent: {exc}", file=sys.stderr)
        raise SystemExit(4) from exc


async def _run_agent_locked(cfg: AgentConfig, notifier: SystemdNotifier) -> None:
    driver_executor = SerialExecutor()
    sock: socket.socket | None = None
    installed: list[signal.Signals] = []
    loop = asyncio.get_running_loop()
    try:
        state = statefile.load(cfg.state_file, allow_missing=cfg.allow_new_state)
        driver = build_driver(cfg)
        try:
            media = await driver_executor.call(driver.describe)
        except DriverError as exc:
            raise ConfigError(f"driver description: {exc}") from exc
        try:
            node = Node(
                cfg.node,
                state=state,
                now=time.monotonic(),
                wall_time=time.time(),
                media=media,
                seed=_stable_seed(cfg.node.node_id),
            )
        except ValueError as exc:
            raise ConfigError(f"agent initialization: {exc}") from exc
        sock = open_multicast_socket(cfg.group, cfg.port, interface=cfg.interface)
        sock.setblocking(False)
        agent_sock = sock

        async def recv() -> tuple[bytes, tuple[str, int]]:
            data, addr = await loop.sock_recvfrom(agent_sock, _RECV_BUFSIZE)
            return data, addr

        runner = AgentRunner(
            node,
            transport=_SocketTransport(sock, (cfg.group, cfg.port)),
            recv=recv,
            driver=driver,
            psk=cfg.psk,
            state_path=cfg.state_file,
            clock=time.monotonic,
            wall_clock=time.time,
            driver_executor=driver_executor,
            on_ready=notifier.ready,
        )
        main_task = asyncio.current_task()
        if main_task is not None:
            for signum in (signal.SIGTERM, signal.SIGINT):
                try:
                    loop.add_signal_handler(signum, main_task.cancel)
                except (NotImplementedError, RuntimeError, ValueError):
                    continue
                installed.append(signum)
        await runner.run()
    finally:
        for signum in installed:
            with contextlib.suppress(ValueError, RuntimeError):
                loop.remove_signal_handler(signum)
        if sock is not None:
            sock.close()
        # Also covers a cancelled describe or socket construction failure,
        # before the runner took responsibility for draining this worker.
        close_task = asyncio.create_task(driver_executor.close())
        while not close_task.done():
            try:
                await asyncio.shield(close_task)
            except asyncio.CancelledError:
                continue
        await close_task


def run_agent(cfg: AgentConfig) -> int:
    """Blocking entry point for the ``yoke agent`` subcommand."""
    # Graceful shutdown (the bye) already ran inside the loop.
    with contextlib.suppress(KeyboardInterrupt, asyncio.CancelledError):
        asyncio.run(run_agent_async(cfg))
    return 0
