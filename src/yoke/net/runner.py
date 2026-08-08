"""Asyncio shell: turns a Node + a Driver + config into a running agent.

The sans-I/O core (``yoke.core.node``) makes every decision; this module
only performs its effects (ARCHITECTURE.md "Sans-I/O core"). It is meant
to stay thin enough to review by eye:

- a receive loop decodes datagrams and feeds the node,
- a timer loop calls ``node.poll`` around ``node.next_deadline()``,
- a driver poll loop reports media-plane health,
- an effect executor sends frames, persists state, and drives the media
  plane (in a thread, serialized by a lock, so a slow device call never
  blocks the control plane).

Everything the class touches is an injected seam; only
:func:`run_agent` / :func:`run_agent_async` construct real sockets,
drivers, and clocks.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import signal
import socket
import sys
import time
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
from yoke.drivers.usbip import UsbipAttachDriver, UsbipBindDriver
from yoke.net.mcast import open_multicast_socket
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
    raise ConfigError(f"driver.type: unknown driver type {driver_type!r}")


def _build_usbip(cfg: AgentConfig, table: dict[str, object]) -> Driver:
    if cfg.node.role is Role.CONSUMER:
        return UsbipAttachDriver(SubprocessRunner())
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
    return UsbipBindDriver(SubprocessRunner(), host=host, port=port, busids=busids)


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
    """Executes one Node's effects against injected I/O seams.

    All node calls happen on the event-loop thread, so the (not
    thread-safe) core never sees concurrency; only the driver's blocking
    calls are pushed to a worker thread, serialized by a lock so device
    commands keep their issue order.
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
        clock: Callable[[], float] = time.time,
        save_state: SaveStateFn = statefile.save,
        max_sleep: float = MAX_TIMER_SLEEP,
        driver_poll_interval: float = DRIVER_POLL_INTERVAL,
    ) -> None:
        self._node = node
        self._node_id = node.node_id
        self._transport = transport
        self._recv = recv
        self._driver = driver
        self._psk = psk
        self._state_path = state_path
        self._clock = clock
        self._save_state = save_state
        self._max_sleep = max_sleep
        self._driver_poll_interval = driver_poll_interval
        self._driver_lock = asyncio.Lock()
        self._driver_tasks: set[asyncio.Task[None]] = set()

    async def run(self) -> None:
        """Run until cancelled; the shutdown effects (bye) go out first."""
        await self._producer_lifecycle("bind")
        await self.execute(self._node.start(self._clock()))
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._receive_loop(), name="yoke-recv")
                tg.create_task(self._timer_loop(), name="yoke-timer")
                tg.create_task(self._driver_poll_loop(), name="yoke-driver-poll")
        finally:
            await self._shutdown()
            await self._producer_lifecycle("unbind")

    async def _producer_lifecycle(self, phase: str) -> None:
        """Producers that export devices (usbip bind/unbind) do so for the
        agent's lifetime; failures surface as driver status, not crashes."""
        hook = getattr(self._driver, phase, None)
        if hook is None:
            return
        try:
            async with self._driver_lock:
                await asyncio.to_thread(hook)
        except DriverError as exc:
            status = DriverStatus(NodeState.ERROR, f"{phase}: {exc}")
            await self.execute(self._node.on_driver_status(status, self._clock()))

    # -- effect execution ----------------------------------------------------

    async def execute(self, effects: Iterable[Effect]) -> None:
        """Perform one node call's effects, in order.

        Driver effects are handed to a background task (thread + lock)
        so device I/O never blocks the receive loop; the protocol
        tolerates this because driver outcomes surface later as
        ``on_driver_status`` events, never as part of the ack path.
        """
        for effect in effects:
            if isinstance(effect, SendFrame):
                self._send_frame(effect)
            elif isinstance(effect, SendRaw):
                self._transport.sendto(effect.raw)
            elif isinstance(effect, Persist):
                self._persist(effect.state)
            elif isinstance(effect, AttachMedia):
                self._spawn_driver(
                    partial(self._driver.attach, effect.producer, effect.media)
                )
            elif isinstance(effect, DetachMedia):
                self._spawn_driver(self._driver.detach)
            elif isinstance(effect, Log):
                _log.info("%s", effect.message)
            else:
                assert_never(effect)

    def _send_frame(self, effect: SendFrame) -> None:
        try:
            raw = frames.encode(
                effect.type,
                self._node_id,
                effect.seq,
                self._clock(),
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
        self._transport.sendto(raw)

    def _persist(self, state: PersistedState) -> None:
        # A full disk must not kill the control plane; the divergence is
        # made visible in the log and nowhere on the wire.
        try:
            self._save_state(self._state_path, state)
        except Exception:
            _log.exception(
                "persist to %s failed; continuing with divergent durable state",
                self._state_path,
            )

    def _spawn_driver(self, call: Callable[[], None]) -> None:
        task = asyncio.get_running_loop().create_task(self._drive(call))
        self._driver_tasks.add(task)
        task.add_done_callback(self._driver_tasks.discard)

    async def _drive(self, call: Callable[[], None]) -> None:
        try:
            async with self._driver_lock:
                await asyncio.to_thread(call)
        except Exception as exc:
            status = DriverStatus(NodeState.ERROR, str(exc))
            await self.execute(self._node.on_driver_status(status, self._clock()))

    # -- loops ---------------------------------------------------------------

    async def _receive_loop(self) -> None:
        while True:
            raw, _addr = await self._recv()
            now = self._clock()
            try:
                frame = frames.decode(raw, self._psk)
            except FrameDrop as drop:
                effects = self._node.on_drop(drop.reason, now)
            else:
                effects = self._node.on_frame(frame, raw, now)
            await self.execute(effects)

    async def _timer_loop(self) -> None:
        while True:
            delay = self._node.next_deadline() - self._clock()
            await asyncio.sleep(min(max(delay, 0.0), self._max_sleep))
            await self.execute(self._node.poll(self._clock()))

    async def _driver_poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self._driver_poll_interval)
            try:
                async with self._driver_lock:
                    status = await asyncio.to_thread(self._driver.poll)
            except Exception as exc:
                status = DriverStatus(NodeState.ERROR, str(exc))
            await self.execute(self._node.on_driver_status(status, self._clock()))

    # -- shutdown ------------------------------------------------------------

    async def _shutdown(self) -> None:
        # Bye first: execute() has no suspension points for pure-send
        # effect lists, so a second cancellation cannot swallow the bye.
        try:
            await self.execute(self._node.shutdown(self._clock()))
        except Exception:
            _log.exception("failed to execute shutdown effects")
        tasks = list(self._driver_tasks)
        self._driver_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


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
    """Wire real state, driver, socket, and signals; run until cancelled.

    Raises ``SystemExit(4)`` when the persisted state file is unusable —
    resolving that (restoring or deleting it) is an operator decision the
    agent must not make (see ``yoke.statefile``).
    """
    try:
        state = statefile.load(cfg.state_file)
    except StateError as exc:
        print(f"yoke agent: {exc}", file=sys.stderr)
        raise SystemExit(4) from exc
    driver = build_driver(cfg)
    clock = time.time
    node = Node(
        cfg.node,
        state=state,
        now=clock(),
        media=driver.describe(),
        seed=_stable_seed(cfg.node.node_id),
    )
    sock = open_multicast_socket(cfg.group, cfg.port, interface=cfg.interface)
    sock.setblocking(False)
    loop = asyncio.get_running_loop()

    async def recv() -> tuple[bytes, tuple[str, int]]:
        data, addr = await loop.sock_recvfrom(sock, _RECV_BUFSIZE)
        return data, addr

    runner = AgentRunner(
        node,
        transport=_SocketTransport(sock, (cfg.group, cfg.port)),
        recv=recv,
        driver=driver,
        psk=cfg.psk,
        state_path=cfg.state_file,
        clock=clock,
    )
    main_task = asyncio.current_task()
    installed: list[signal.Signals] = []
    if main_task is not None:
        for signum in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(signum, main_task.cancel)
            except (NotImplementedError, RuntimeError, ValueError):
                continue  # non-Unix loop or non-main thread: rely on defaults
            installed.append(signum)
    try:
        await runner.run()
    finally:
        for signum in installed:
            with contextlib.suppress(ValueError, RuntimeError):
                loop.remove_signal_handler(signum)
        sock.close()


def run_agent(cfg: AgentConfig) -> int:
    """Blocking entry point for the ``yoke agent`` subcommand."""
    # Graceful shutdown (the bye) already ran inside the loop.
    with contextlib.suppress(KeyboardInterrupt, asyncio.CancelledError):
        asyncio.run(run_agent_async(cfg))
    return 0
