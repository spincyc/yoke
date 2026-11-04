"""Tests for the asyncio shell (yoke.net.runner).

All offline. The runner is exercised with a fake transport (recorded
datagrams + a scripted inbound queue), a manually advanced clock, and a
SimDriver. The timer loops use real (short) sleeps, capped small via the
injected ``max_sleep``, so each test settles well under a second.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from tests.test_stream_driver import MEDIA as STREAM_MEDIA
from tests.test_stream_driver import FakeSupervisor
from yoke.config import AgentConfig, ConfigError
from yoke.core.frames import decode, encode
from yoke.core.node import Node
from yoke.core.types import (
    Body,
    DriverStatus,
    Frame,
    FrameType,
    Kind,
    NodeConfig,
    NodeState,
    PersistedState,
    Role,
    SendFrame,
    SendRaw,
)
from yoke.drivers.base import DriverError
from yoke.drivers.shellcmd import CommandResult, FakeRunner
from yoke.drivers.sim import SimDriver
from yoke.drivers.stream import StreamDriver
from yoke.drivers.usbip import UsbipBindDriver
from yoke.net.executor import SerialExecutor
from yoke.net.notify import SystemdNotifier
from yoke.net.runner import AgentRunner, run_agent, run_agent_async
from yoke.statefile import StateError

PSK = b"runner-test-psk"
T0 = 1_754_000_000.0
ADDR = ("10.0.0.5", 9753)

PROD_MEDIA: Body = {"group": "239.10.0.1", "port": 5004}
PROD_ANN: Body = {
    "role": "producer",
    "kind": "hdmi",
    "name": "cam",
    "state": "active",
    "err": None,
    "pairing": None,
    "media": PROD_MEDIA,
}

LogEntry = tuple[str, object]


class ManualClock:
    def __init__(self, now: float = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt


class FakeTransport:
    """Records every outbound datagram, and mirrors sends into a shared
    ordering log so persist-vs-send ordering is assertable."""

    def __init__(self, log: list[LogEntry]) -> None:
        self.sent: list[bytes] = []
        self._log = log

    def sendto(self, raw: bytes) -> None:
        self.sent.append(raw)
        self._log.append(("send", raw))


@dataclass
class Harness:
    runner: AgentRunner
    node: Node
    driver: SimDriver
    transport: FakeTransport
    inbox: asyncio.Queue[tuple[bytes, tuple[str, int]]]
    clock: ManualClock
    wall_clock: ManualClock
    log: list[LogEntry]
    saved: list[PersistedState]
    task: asyncio.Task[None] | None = field(default=None)

    def frames(self) -> list[Frame]:
        return [decode(raw, PSK) for raw in self.transport.sent]

    def events(self) -> list[str]:
        return [str(f.body["event"]) for f in self.frames() if f.type is FrameType.EVT]

    def acks(self) -> list[Frame]:
        return [f for f in self.frames() if f.type is FrameType.ACK]

    def attach_calls(self) -> int:
        return sum(1 for call in self.driver.calls if call[0] == "attach")


def make_harness(
    *,
    role: Role = Role.CONSUMER,
    node_id: str = "cons-1",
    driver: SimDriver | None = None,
    save_state: Callable[[Path, PersistedState], None] | None = None,
    driver_poll_interval: float = 30.0,  # quiet unless a test wants it
    driver_executor: SerialExecutor | None = None,
    on_ready: Callable[[], None] | None = None,
    admission: bool = False,
) -> Harness:
    log: list[LogEntry] = []
    saved: list[PersistedState] = []
    clock = ManualClock(100.0)
    wall_clock = ManualClock()
    transport = FakeTransport(log)
    inbox: asyncio.Queue[tuple[bytes, tuple[str, int]]] = asyncio.Queue()
    sim = driver if driver is not None else SimDriver()
    node = Node(
        NodeConfig(node_id=node_id, role=role, kind=Kind.HDMI, name="test"),
        state=PersistedState(),
        now=clock(),
        wall_time=wall_clock(),
        media=sim.describe(),
        seed=7,
        admission=admission,
    )

    def default_save(path: Path, state: PersistedState) -> None:
        saved.append(state)
        log.append(("persist", state))

    runner = AgentRunner(
        node,
        transport=transport,
        recv=inbox.get,
        driver=sim,
        psk=PSK,
        state_path=Path("/nonexistent/agent-state.json"),
        clock=clock,
        wall_clock=wall_clock,
        save_state=save_state if save_state is not None else default_save,
        max_sleep=0.02,
        driver_poll_interval=driver_poll_interval,
        driver_executor=driver_executor,
        on_ready=on_ready,
    )
    return Harness(
        runner=runner,
        node=node,
        driver=sim,
        transport=transport,
        inbox=inbox,
        clock=clock,
        wall_clock=wall_clock,
        log=log,
        saved=saved,
    )


@asynccontextmanager
async def running_agent(**kwargs: Any) -> AsyncIterator[Harness]:
    h = make_harness(**kwargs)
    h.task = asyncio.create_task(h.runner.run())
    try:
        yield h
    finally:
        if not h.task.done():
            h.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await h.task  # re-raises non-cancel crashes into the test


async def eventually(predicate: Callable[[], bool], timeout: float = 1.5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not met within timeout")


def pair_intent(version: int = 1, seq: int = 1, *, producer: str = "prod-1") -> bytes:
    body: Body = {
        "op": "pair",
        "consumer": "cons-1",
        "producer": producer,
        "version": [version, "cli-1"],
    }
    return encode(FrameType.INT, "cli-1", seq, T0, body, PSK)


# --- runner behavior --------------------------------------------------------


async def test_startup_emits_persist_then_started_and_ann() -> None:
    async with running_agent() as h:
        await eventually(lambda: len(h.transport.sent) >= 2)
        frames = h.frames()
        assert frames[0].type is FrameType.EVT
        assert frames[0].body["event"] == "started"
        assert frames[1].type is FrameType.ANN
        assert frames[1].body["role"] == "consumer"
        # start()'s Persist executed before anything went on the wire
        assert h.log[0][0] == "persist"
        assert h.saved and h.saved[0].pairing.producer is None


async def test_intent_acked_and_persisted_before_ack() -> None:
    async with running_agent() as h:
        await eventually(lambda: len(h.transport.sent) >= 2)
        await h.inbox.put((pair_intent(), ADDR))
        await eventually(lambda: len(h.acks()) >= 1)
        ack = h.acks()[0]
        assert ack.body == {
            "version": [1, "cli-1"],
            "applied": True,
            "reason": "applied",
        }
        assert h.node.pairing.producer == "prod-1"
        # ordering: the Persist carrying the new pairing precedes the ack
        persist_idx = next(
            i
            for i, (kind, payload) in enumerate(h.log)
            if kind == "persist"
            and isinstance(payload, PersistedState)
            and payload.pairing.producer == "prod-1"
        )
        ack_idx = next(
            i
            for i, (kind, payload) in enumerate(h.log)
            if kind == "send"
            and isinstance(payload, bytes)
            and decode(payload, PSK).type is FrameType.ACK
        )
        assert persist_idx < ack_idx


async def test_garbage_inbound_is_counted_and_announced() -> None:
    async with running_agent() as h:
        await h.inbox.put((b"\xff\xfenot a frame", ADDR))
        await eventually(lambda: "rx-drop" in h.events())
        rx_drop = next(f for f in h.frames() if f.body.get("event") == "rx-drop")
        assert rx_drop.body["detail"]["malformed"] == 1
        assert h.task is not None and not h.task.done()


async def test_attach_failure_emits_driver_error_and_retries() -> None:
    driver = SimDriver()
    driver.fail_attach_times(1)
    async with running_agent(driver=driver) as h:
        await h.inbox.put((encode(FrameType.ANN, "prod-1", 1, T0, PROD_ANN, PSK), ADDR))
        await h.inbox.put((pair_intent(), ADDR))
        await eventually(lambda: h.attach_calls() == 1 and "driver-error" in h.events())
        h.clock.advance(1.2)  # past RETRY_BACKOFF_BASE
        await eventually(lambda: h.attach_calls() >= 2)
        assert "driver-retry" in h.events()
        assert driver.attached_to == "prod-1"
        assert driver.attached_media == PROD_MEDIA


@pytest.mark.parametrize("role", [Role.CONSUMER, Role.PRODUCER])
async def test_startup_persist_failure_emits_nothing_and_stops(role: Role) -> None:
    class BindingDriver(SimDriver):
        bound = False

        def bind(self) -> None:
            self.bound = True

    driver = BindingDriver()

    def broken_save(path: Path, state: PersistedState) -> None:
        raise OSError("disk full")

    h = make_harness(role=role, driver=driver, save_state=broken_save)
    with pytest.raises(StateError, match="startup state"):
        await h.runner.run()
    assert h.transport.sent == []
    assert h.driver.calls == []
    assert not driver.bound


async def test_persist_failure_withholds_pairing_attach_and_success_ack() -> None:
    def broken_pair_save(path: Path, state: PersistedState) -> None:
        if state.pairing.producer is not None:
            raise OSError("disk full")

    async with running_agent(save_state=broken_pair_save) as h:
        await eventually(lambda: len(h.transport.sent) >= 2)
        await h.inbox.put((encode(FrameType.ANN, "prod-1", 1, T0, PROD_ANN, PSK), ADDR))
        await h.inbox.put((pair_intent(version=1, seq=1), ADDR))
        await eventually(lambda: "prod-1" in h.node.peers)
        # Queue a later frame as a barrier: its discovery proves the
        # failed save has completed and the receive loop remains alive.
        await h.inbox.put((encode(FrameType.ANN, "prod-2", 1, T0, PROD_ANN, PSK), ADDR))
        await eventually(lambda: "prod-2" in h.node.peers)
        assert h.acks() == []
        assert h.node.pairing.producer is None
        assert h.attach_calls() == 0
        assert h.task is not None and not h.task.done()


async def test_slow_persistence_does_not_block_event_loop_or_release_ack() -> None:
    entered = threading.Event()
    release = threading.Event()

    def slow_save(path: Path, state: PersistedState) -> None:
        if state.pairing.producer is not None:
            entered.set()
            if not release.wait(2):
                raise OSError("test save timed out")

    async with running_agent(save_state=slow_save) as h:
        await eventually(lambda: len(h.transport.sent) >= 2)
        await h.inbox.put((pair_intent(), ADDR))
        try:
            await eventually(entered.is_set)
            # This loop callback runs while the save is genuinely blocked
            # in its worker; no committed state or ACK is visible yet.
            assert h.acks() == []
            assert h.node.pairing.producer is None
        finally:
            release.set()
        await eventually(lambda: bool(h.acks()))
        assert h.node.pairing.producer == "prod-1"


@pytest.mark.parametrize("save_fails", [False, True])
async def test_service_ready_waits_for_successful_durable_startup(
    save_fails: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = threading.Event()
    release = threading.Event()

    def slow_save(path: Path, state: PersistedState) -> None:
        entered.set()
        assert release.wait(5)
        if save_fails:
            raise OSError("disk full")

    path = str(tmp_path.parent / ("notify-" + uuid.uuid4().hex[:8]))
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
        receiver.bind(path)
        receiver.setblocking(False)
        monkeypatch.setenv("NOTIFY_SOCKET", path)
        notifier = SystemdNotifier.capture()
        h = make_harness(save_state=slow_save, on_ready=notifier.ready)
        task = asyncio.create_task(h.runner.run())
        try:
            await eventually(entered.is_set)
            with pytest.raises(BlockingIOError):
                receiver.recv(64)
            release.set()
            if save_fails:
                with pytest.raises(StateError):
                    await task
                with pytest.raises(BlockingIOError):
                    receiver.recv(64)
            else:
                async with asyncio.timeout(1):
                    ready, _addr = await asyncio.get_running_loop().sock_recvfrom(
                        receiver, 64
                    )
                assert ready == b"READY=1"
                assert not task.done()
                await h.inbox.put((pair_intent(), ADDR))
                await eventually(lambda: bool(h.acks()))
        finally:
            release.set()
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError, StateError):
                await task


@pytest.mark.parametrize("bind_fails", [False, True])
async def test_control_ready_and_receives_while_producer_bind_is_blocked(
    bind_fails: bool,
) -> None:
    class BlockingBindDriver(SimDriver):
        def __init__(self) -> None:
            super().__init__(media=PROD_MEDIA)
            self.entered = threading.Event()
            self.release = threading.Event()
            self.order: list[str] = []

        def bind(self) -> None:
            self.order.append("bind-start")
            self.entered.set()
            assert self.release.wait(5)
            self.order.append("bind-end")
            if bind_fails:
                raise DriverError("device unavailable")

        def unbind(self) -> None:
            self.order.append("unbind")

    ready: list[bool] = []
    driver = BlockingBindDriver()
    async with running_agent(
        role=Role.PRODUCER, driver=driver, on_ready=lambda: ready.append(True)
    ) as h:
        try:
            await eventually(lambda: driver.entered.is_set() and bool(ready))
            assert driver.order == ["bind-start"]
            await h.inbox.put(
                (encode(FrameType.ANN, "prod-1", 1, T0, PROD_ANN, PSK), ADDR)
            )
            await eventually(lambda: "prod-1" in h.node.peers)
        finally:
            driver.release.set()
        if bind_fails:
            await eventually(lambda: "driver-error" in h.events())
        assert ready == [True]
    assert driver.order == ["bind-start", "bind-end", "unbind"]


async def test_send_raw_passes_datagram_through_verbatim() -> None:
    h = make_harness()
    payload = b"\x00\x01verbatim, not a valid frame"
    await h.runner.execute([SendRaw(payload)])
    assert h.transport.sent == [payload]


async def test_graceful_cancel_sends_stopping_then_bye() -> None:
    async with running_agent() as h:
        await eventually(lambda: len(h.transport.sent) >= 2)
        assert h.task is not None
        h.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await h.task
        frames = h.frames()
        assert frames[-1].type is FrameType.BYE
        assert frames[-2].type is FrameType.EVT
        assert frames[-2].body["event"] == "stopping"


async def test_encode_error_is_logged_and_not_fatal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    h = make_harness()
    oversize = SendFrame(
        type=FrameType.EVT,
        seq=1,
        body={"event": "huge", "detail": {"pad": "x" * 1300}},
    )
    fine = SendFrame(type=FrameType.EVT, seq=2, body={"event": "ok", "detail": {}})
    with caplog.at_level(logging.ERROR, logger="yoke.agent"):
        await h.runner.execute([oversize, fine])
    assert any("unencodable" in record.message for record in caplog.records)
    assert len(h.transport.sent) == 1
    assert decode(h.transport.sent[0], PSK).body["event"] == "ok"


async def test_driver_poll_error_is_reported_as_status() -> None:
    class ExplodingDriver(SimDriver):
        def poll(self) -> Any:
            raise RuntimeError("device on fire")

    # Use a producer: an unpaired consumer deliberately ignores non-idle
    # driver status (a torn-down attach must not resurrect state), so the
    # driver-error surfaces on a node that is actually driving media.
    driver = ExplodingDriver(media=PROD_MEDIA)
    h = make_harness(role=Role.PRODUCER, driver=driver, driver_poll_interval=0.02)
    h.task = asyncio.create_task(h.runner.run())
    try:
        await eventually(lambda: "driver-error" in h.events())
        err = next(f for f in h.frames() if f.body.get("event") == "driver-error")
        assert "device on fire" in err.body["detail"]["detail"]
    finally:
        h.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await h.task


async def test_failed_stream_metadata_cannot_promote_old_pipeline_health() -> None:
    class CountingStreamDriver(StreamDriver):
        poll_count = 0

        def poll(self) -> DriverStatus:
            self.poll_count += 1
            return super().poll()

    supervisor = FakeSupervisor()
    driver = CountingStreamDriver(supervisor)
    async with running_agent(driver=driver, driver_poll_interval=0.01) as h:
        valid = {**PROD_ANN, "media": STREAM_MEDIA}
        await h.inbox.put((encode(FrameType.ANN, "prod-1", 1, T0, valid, PSK), ADDR))
        await h.inbox.put((pair_intent(), ADDR))
        await eventually(lambda: h.node.node_state is NodeState.ACTIVE)
        before = len(h.events())
        invalid = {**PROD_ANN, "media": {**STREAM_MEDIA, "height": 0}}
        await h.inbox.put((encode(FrameType.ANN, "prod-1", 2, T0, invalid, PSK), ADDR))
        await eventually(lambda: "driver-error" in h.events()[before:])
        polls = driver.poll_count
        await eventually(lambda: driver.poll_count >= polls + 3)
        assert h.node.node_state is NodeState.ERROR
        assert "media-up" not in h.events()[before:]
        assert len(supervisor.started) == 1
        assert not supervisor.handles[0].terminated
        errors = h.events().count("driver-error")
        h.clock.advance(1.1)
        await eventually(lambda: h.events().count("driver-error") > errors)
        assert len(supervisor.started) == 1  # the invalid route was retried
        corrected = {**PROD_ANN, "media": {**STREAM_MEDIA, "height": 720}}
        await h.inbox.put(
            (encode(FrameType.ANN, "prod-1", 3, T0, corrected, PSK), ADDR)
        )
        await eventually(lambda: h.node.node_state is NodeState.ACTIVE)
        assert len(supervisor.started) == 2
        assert supervisor.handles[0].terminated


@pytest.mark.parametrize("replace_producer", [False, True])
async def test_failed_prevalidation_keeps_old_health_stale_and_retries(
    replace_producer: bool,
) -> None:
    class RejectingDriver(SimDriver):
        attempts = 0
        poll_count = 0
        reject_new = True

        def attach(self, producer: str, media: Body) -> None:
            self.attempts += 1
            if self.reject_new and media["port"] == 6000:
                raise DriverError("new metadata rejected before replacing old route")
            super().attach(producer, media)

        def poll(self) -> DriverStatus:
            self.poll_count += 1
            return super().poll()

    driver = RejectingDriver()
    async with running_agent(driver=driver, driver_poll_interval=0.01) as h:
        await h.inbox.put((encode(FrameType.ANN, "prod-1", 1, T0, PROD_ANN, PSK), ADDR))
        await h.inbox.put((pair_intent(), ADDR))
        await eventually(lambda: h.node.node_state is NodeState.ACTIVE)
        changed = {**PROD_ANN, "media": {**PROD_MEDIA, "port": 6000}}
        if replace_producer:
            await h.inbox.put(
                (encode(FrameType.ANN, "prod-2", 1, T0, changed, PSK), ADDR)
            )
            await h.inbox.put((pair_intent(2, 2, producer="prod-2"), ADDR))
        else:
            await h.inbox.put(
                (encode(FrameType.ANN, "prod-1", 2, T0, changed, PSK), ADDR)
            )
        await eventually(lambda: "driver-error" in h.events())
        polls = driver.poll_count
        await eventually(lambda: driver.poll_count >= polls + 3)
        assert h.node.node_state is NodeState.ERROR
        assert driver.attached_media == (None if replace_producer else PROD_MEDIA)
        h.clock.advance(1.1)
        await eventually(lambda: driver.attempts >= 3)
        assert h.node.node_state is NodeState.ERROR
        driver.reject_new = False
        h.clock.advance(2.1)
        await eventually(lambda: h.node.node_state is NodeState.ACTIVE)
        assert driver.attached_media == changed["media"]


async def test_successful_command_with_failed_health_poll_can_recover() -> None:
    class PollFlakeDriver(SimDriver):
        fail_next_poll = False

        def attach(self, producer: str, media: Body) -> None:
            super().attach(producer, media)
            self.fail_next_poll = media["port"] == 6000

        def poll(self) -> DriverStatus:
            if self.fail_next_poll:
                self.fail_next_poll = False
                raise DriverError("health query unavailable after successful attach")
            return super().poll()

    driver = PollFlakeDriver()
    async with running_agent(driver=driver, driver_poll_interval=0.01) as h:
        await h.inbox.put((encode(FrameType.ANN, "prod-1", 1, T0, PROD_ANN, PSK), ADDR))
        await h.inbox.put((pair_intent(), ADDR))
        await eventually(lambda: h.node.node_state is NodeState.ACTIVE)
        changed = {**PROD_ANN, "media": {**PROD_MEDIA, "port": 6000}}
        await h.inbox.put((encode(FrameType.ANN, "prod-1", 2, T0, changed, PSK), ADDR))
        await eventually(lambda: "driver-error" in h.events())
        await eventually(lambda: h.node.node_state is NodeState.ACTIVE)
        assert driver.attached_media == changed["media"]
        assert h.attach_calls() == 2  # recovery came from the actual new route's poll


async def test_changed_media_announces_pending_until_current_attach_completes() -> None:
    class ChangingDriver(SimDriver):
        def __init__(self) -> None:
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()

        def attach(self, producer: str, media: Body) -> None:
            if media["port"] == 6000:
                self.entered.set()
                assert self.release.wait(5)
            super().attach(producer, media)

        def poll(self) -> DriverStatus:
            if self.attached_media is not None:
                return DriverStatus(
                    NodeState.ACTIVE, f"port {self.attached_media['port']}"
                )
            return super().poll()

    driver = ChangingDriver()
    async with running_agent(driver=driver, driver_poll_interval=0.01) as h:
        await h.inbox.put((encode(FrameType.ANN, "prod-1", 1, T0, PROD_ANN, PSK), ADDR))
        await h.inbox.put((pair_intent(), ADDR))
        await eventually(lambda: h.node.node_state is NodeState.ACTIVE)
        before = len(h.frames())
        changed = {**PROD_ANN, "media": {**PROD_MEDIA, "port": 6000}}
        await h.inbox.put((encode(FrameType.ANN, "prod-1", 2, T0, changed, PSK), ADDR))
        try:
            await eventually(driver.entered.is_set)
            h.clock.advance(3)
            await eventually(
                lambda: sum(f.type is FrameType.ANN for f in h.frames()[before:]) >= 2
            )
            for frame in h.frames()[before:]:
                if frame.type is FrameType.ANN:
                    assert frame.body["state"] == "pending"
                    assert frame.body["err"] is None
                    assert "driver_status" not in frame.body
        finally:
            driver.release.set()
        await eventually(lambda: h.node.node_state is NodeState.ACTIVE)
        assert any(
            frame.type is FrameType.ANN
            and frame.body.get("driver_status") == {"detail": "port 6000"}
            for frame in h.frames()[before:]
        )


async def test_stale_poll_cannot_publish_previous_pairing_error() -> None:
    class BlockingPollDriver(SimDriver):
        def __init__(self) -> None:
            super().__init__()
            self.arm = threading.Event()
            self.entered = threading.Event()
            self.release = threading.Event()
            self.threads: set[int] = set()

        def attach(self, producer: str, media: Body) -> None:
            self.threads.add(threading.get_ident())
            super().attach(producer, media)

        def detach(self) -> None:
            self.threads.add(threading.get_ident())
            super().detach()

        def poll(self) -> DriverStatus:
            self.threads.add(threading.get_ident())
            if self.arm.is_set():
                self.arm.clear()
                self.entered.set()
                assert self.release.wait(2)
                return DriverStatus(NodeState.ERROR, "stale poll from prod-1")
            return super().poll()

    driver = BlockingPollDriver()
    async with running_agent(driver=driver, driver_poll_interval=0.01) as h:
        for producer in ("prod-1", "prod-2"):
            await h.inbox.put(
                (encode(FrameType.ANN, producer, 1, T0, PROD_ANN, PSK), ADDR)
            )
        await h.inbox.put((pair_intent(), ADDR))
        await eventually(lambda: h.node.node_state is NodeState.ACTIVE)
        driver.arm.set()
        try:
            await eventually(driver.entered.is_set)
            await h.inbox.put((pair_intent(2, 2, producer="prod-2"), ADDR))
            await eventually(lambda: h.node.pairing.producer == "prod-2")
            assert h.node.node_state is NodeState.PENDING
        finally:
            driver.release.set()
        await eventually(
            lambda: (
                driver.attached_to == "prod-2" and h.node.node_state is NodeState.ACTIVE
            )
        )
        assert "driver-error" not in h.events()
        assert len(driver.threads) == 1


@pytest.mark.parametrize("after_churn", ["latest", "unpair", "shutdown"])
async def test_route_churn_keeps_only_latest_work_and_bounded_queue(
    after_churn: str,
) -> None:
    class TrackingExecutor(SerialExecutor):
        def __init__(self) -> None:
            super().__init__()
            self.pending = 0
            self.peak_pending = 0

        def submit(self, call: Callable[[], Any]) -> asyncio.Future[Any]:
            self.pending += 1
            self.peak_pending = max(self.peak_pending, self.pending)
            future = super().submit(call)
            future.add_done_callback(self.completed)
            return future

        def completed(self, future: asyncio.Future[Any]) -> None:
            self.pending -= 1

    class BlockingDriver(SimDriver):
        def __init__(self) -> None:
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()
            self.routes: list[int] = []
            self.order: list[str] = []

        def attach(self, producer: str, media: Body) -> None:
            self.routes.append(media["port"])
            self.order.append("attach-start")
            if not self.entered.is_set():
                self.entered.set()
                assert self.release.wait(5)
            super().attach(producer, media)
            self.order.append("attach-end")

        def poll(self) -> DriverStatus:
            self.order.append("poll")
            return super().poll()

    worker = TrackingExecutor()
    driver = BlockingDriver()
    async with running_agent(
        driver=driver, driver_executor=worker, driver_poll_interval=0.01
    ) as h:
        await h.inbox.put((encode(FrameType.ANN, "prod-1", 1, T0, PROD_ANN, PSK), ADDR))
        await h.inbox.put((pair_intent(), ADDR))
        try:
            await eventually(driver.entered.is_set)
            for seq, port in enumerate(range(6000, 6301), start=2):
                body = {**PROD_ANN, "media": {**PROD_MEDIA, "port": port}}
                await h.inbox.put(
                    (encode(FrameType.ANN, "prod-1", seq, T0, body, PSK), ADDR)
                )
            await eventually(
                lambda: h.node.peers["prod-1"].media == {**PROD_MEDIA, "port": 6300}
            )
            await eventually(lambda: worker.pending == 2)  # call + one waiting poll
            assert worker.peak_pending <= 2
            assert driver.routes == [5004]
            assert h.runner._pending_attach is not None
            assert h.runner._pending_attach.media["port"] == 6300
            assert (
                sum(task.get_name() == "yoke-driver" for task in asyncio.all_tasks())
                == 1
            )
            if after_churn == "unpair":
                body = {
                    "op": "unpair",
                    "consumer": "cons-1",
                    "version": [2, "cli-1"],
                }
                await h.inbox.put(
                    (encode(FrameType.INT, "cli-1", 2, T0, body, PSK), ADDR)
                )
                await eventually(lambda: h.node.pairing.producer is None)
                assert h.runner._pending_attach is None
            elif after_churn == "shutdown":
                assert h.task is not None
                h.task.cancel()
                await asyncio.sleep(0.01)
                assert not h.task.done()
        finally:
            driver.release.set()
        if after_churn == "shutdown":
            assert h.task is not None
            with pytest.raises(asyncio.CancelledError):
                await h.task
            assert driver.routes == [5004]
            assert driver.attached_to is None
            assert h.node.pairing.producer == "prod-1"
        elif after_churn == "unpair":
            await eventually(
                lambda: driver.attached_to is None and ("detach",) in driver.calls
            )
            assert driver.routes == [5004]
            assert h.node.pairing.producer is None
        else:
            await eventually(lambda: h.node.node_state is NodeState.ACTIVE)
            assert driver.routes == [5004, 6300]
            # Command-status polling plus the already queued periodic
            # poll both run before the latest pending route can start.
            start = driver.order.index("attach-end")
            assert driver.order[start + 1 : start + 3] == ["poll", "poll"]
        assert worker.peak_pending <= 2


async def test_replacement_cannot_attach_before_failed_teardown_retries() -> None:
    driver = SimDriver()
    async with running_agent(driver=driver, driver_poll_interval=0.01) as h:
        for producer in ("prod-1", "prod-2"):
            await h.inbox.put(
                (encode(FrameType.ANN, producer, 1, T0, PROD_ANN, PSK), ADDR)
            )
        await h.inbox.put((pair_intent(), ADDR))
        await eventually(lambda: h.node.node_state is NodeState.ACTIVE)
        driver.fail_detach_once()
        await h.inbox.put((pair_intent(2, 2, producer="prod-2"), ADDR))
        await eventually(lambda: h.node.node_state is NodeState.ERROR)
        assert h.node.pairing.producer == "prod-2"
        assert driver.attached_to == "prod-1"
        assert h.attach_calls() == 1
        h.clock.advance(1.1)
        await eventually(lambda: h.node.node_state is NodeState.ACTIVE)
        assert driver.attached_to == "prod-2"
        assert [call[0] for call in driver.calls] == [
            "attach",
            "detach",
            "detach",
            "attach",
        ]


async def test_failed_unpair_detach_retries_without_restoring_pairing() -> None:
    driver = SimDriver()
    async with running_agent(driver=driver, driver_poll_interval=0.01) as h:
        await h.inbox.put((encode(FrameType.ANN, "prod-1", 1, T0, PROD_ANN, PSK), ADDR))
        await h.inbox.put((pair_intent(), ADDR))
        await eventually(lambda: h.node.node_state is NodeState.ACTIVE)
        driver.fail_detach_once()
        body = {"op": "unpair", "consumer": "cons-1", "version": [2, "cli-1"]}
        await h.inbox.put((encode(FrameType.INT, "cli-1", 2, T0, body, PSK), ADDR))
        await eventually(lambda: h.node.node_state is NodeState.ERROR)
        assert h.node.pairing.producer is None
        assert driver.attached_to == "prod-1"
        h.clock.advance(1.1)
        await eventually(lambda: h.node.node_state is NodeState.IDLE)
        assert driver.attached_to is None
        assert h.node.pairing.producer is None
        assert sum(call == ("detach",) for call in driver.calls) == 2


@pytest.mark.parametrize("reason", [[], {}])
async def test_malformed_ack_reason_cannot_kill_receive_loop(reason: object) -> None:
    async with running_agent() as h:
        body = {"version": [1, "cli-1"], "applied": False, "reason": reason}
        await h.inbox.put((encode(FrameType.ACK, "other-1", 1, T0, body, PSK), ADDR))
        await h.inbox.put((pair_intent(), ADDR))
        await eventually(lambda: bool(h.acks()))
        assert h.node.pairing.producer == "prod-1"
        assert h.task is not None and not h.task.done()


async def test_cancel_drains_blocked_attach_before_final_detach() -> None:
    class BlockingAttachDriver(SimDriver):
        def __init__(self) -> None:
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()
            self.order: list[str] = []
            self.threads: set[int] = set()

        def attach(self, producer: str, media: Body) -> None:
            self.threads.add(threading.get_ident())
            self.order.append("attach-start")
            self.entered.set()
            assert self.release.wait(2)
            super().attach(producer, media)
            self.order.append("attach-end")

        def detach(self) -> None:
            self.threads.add(threading.get_ident())
            self.order.append("detach")
            super().detach()

    driver = BlockingAttachDriver()
    async with running_agent(driver=driver) as h:
        await h.inbox.put((encode(FrameType.ANN, "prod-1", 1, T0, PROD_ANN, PSK), ADDR))
        await h.inbox.put((pair_intent(), ADDR))
        try:
            await eventually(driver.entered.is_set)
            assert h.task is not None
            h.task.cancel()
            await asyncio.sleep(0.01)
            assert not h.task.done()
            h.task.cancel()  # a repeated stop still must drain the real call
            await asyncio.sleep(0.01)
            assert not h.task.done()
        finally:
            driver.release.set()
        with pytest.raises(asyncio.CancelledError):
            await h.task
        assert driver.order == ["attach-start", "attach-end", "detach"]
        assert driver.attached_to is None
        assert h.node.pairing.producer == "prod-1"
        assert len(driver.threads) == 1


async def test_failed_shutdown_reservation_still_quiesces_consumer() -> None:
    refuse_saves = threading.Event()

    def save(path: Path, state: PersistedState) -> None:
        if refuse_saves.is_set():
            raise OSError("disk full during shutdown reservation")

    async with running_agent(save_state=save) as h:
        await h.inbox.put((encode(FrameType.ANN, "prod-1", 1, T0, PROD_ANN, PSK), ADDR))
        await h.inbox.put((pair_intent(), ADDR))
        await eventually(lambda: h.node.node_state is NodeState.ACTIVE)
        # Reach the exact durable boundary without generating thousands
        # of unrelated announcements. Shutdown must detach even when it
        # cannot reserve sequence numbers for stopping/bye.
        h.node._seq = h.node._reserved_until
        refuse_saves.set()
        assert h.task is not None
        h.task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await h.task
        assert h.driver.attached_to is None
        assert h.driver.calls[-1] == ("detach",)
        assert h.node.pairing.producer == "prod-1"


async def test_wire_timestamps_use_wall_clock_but_peer_timers_do_not() -> None:
    async with running_agent() as h:
        await eventually(lambda: len(h.transport.sent) >= 2)
        assert all(frame.ts == T0 for frame in h.frames())
        await h.inbox.put((encode(FrameType.ANN, "prod-1", 1, T0, PROD_ANN, PSK), ADDR))
        await eventually(lambda: "prod-1" in h.node.peers)
        h.wall_clock.advance(86_400)
        # A local pairing ACK carries the changed wall time without
        # changing the monotonic age of the discovered producer.
        await h.inbox.put((pair_intent(), ADDR))
        await eventually(lambda: bool(h.acks()))
        assert h.acks()[0].ts == T0 + 86_400
        assert "peer-lost" not in h.events()
        h.clock.advance(7)
        await eventually(lambda: "peer-lost" in h.events())
        assert h.node.pairing.producer == "prod-1"
        assert h.node.node_state is NodeState.PENDING


async def test_transient_receive_error_is_contained() -> None:
    h = make_harness()
    attempts = 0
    original_recv = h.runner._recv

    async def flaky_recv() -> tuple[bytes, tuple[str, int]]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("interface unavailable")
        return await original_recv()

    h.runner._recv = flaky_recv
    task = asyncio.create_task(h.runner.run())
    try:
        await h.inbox.put((pair_intent(), ADDR))
        await eventually(lambda: bool(h.acks()))
        assert attempts >= 2
        assert not task.done()
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


# --- real wiring ------------------------------------------------------------


async def test_run_agent_async_corrupt_state_exits_4(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text("{this is not json")
    cfg = AgentConfig(
        node=NodeConfig(node_id="agent-1", role=Role.CONSUMER, kind=Kind.HDMI),
        state_file=state_path,
        psk=b"k",
        driver={"type": "sim"},
    )
    with pytest.raises(SystemExit) as excinfo:
        await run_agent_async(cfg)
    assert excinfo.value.code == 4


async def test_invalid_media_description_is_operator_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready: list[bool] = []
    monkeypatch.setenv("NOTIFY_SOCKET", "/unused-notify.sock")
    monkeypatch.setattr(SystemdNotifier, "ready", lambda self: ready.append(True))
    cfg = AgentConfig(
        node=NodeConfig(node_id="producer-1", role=Role.PRODUCER, kind=Kind.HDMI),
        state_file=tmp_path / "state.json",
        psk=b"k",
        allow_new_state=True,
        driver={"type": "sim", "media": {"pad": "x" * 1500}},
    )
    with pytest.raises(ConfigError, match="agent initialization"):
        await run_agent_async(cfg)
    assert not cfg.state_file.exists()
    assert ready == []
    assert "NOTIFY_SOCKET" not in os.environ


async def test_control_socket_initialization_failure_never_signals_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready: list[bool] = []
    monkeypatch.setattr(SystemdNotifier, "ready", lambda self: ready.append(True))

    def unavailable_socket(*args: object, **kwargs: object) -> None:
        raise OSError("control interface unavailable")

    monkeypatch.setattr("yoke.net.runner.open_multicast_socket", unavailable_socket)
    cfg = AgentConfig(
        node=NodeConfig(node_id="consumer-1", role=Role.CONSUMER, kind=Kind.HDMI),
        state_file=tmp_path / "state.json",
        psk=b"k",
        allow_new_state=True,
        driver={"type": "sim"},
    )
    with pytest.raises(OSError, match="control interface unavailable"):
        await run_agent_async(cfg)
    assert ready == []


async def test_driver_description_failure_is_operator_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BadDescriptionDriver(SimDriver):
        def describe(self) -> Body | None:
            raise DriverError("missing export description")

    monkeypatch.setattr(
        "yoke.net.runner.build_driver", lambda cfg: BadDescriptionDriver()
    )
    cfg = AgentConfig(
        node=NodeConfig(node_id="producer-1", role=Role.PRODUCER, kind=Kind.HDMI),
        state_file=tmp_path / "state.json",
        psk=b"k",
        allow_new_state=True,
        driver={"type": "sim"},
    )
    with pytest.raises(ConfigError, match="driver description"):
        await run_agent_async(cfg)


async def test_run_agent_async_refuses_uncommissioned_missing_state(
    tmp_path: Path,
) -> None:
    cfg = AgentConfig(
        node=NodeConfig(node_id="agent-1", role=Role.CONSUMER, kind=Kind.HDMI),
        state_file=tmp_path / "missing.json",
        psk=b"k",
        driver={"type": "sim"},
    )
    with pytest.raises(SystemExit) as excinfo:
        await run_agent_async(cfg)
    assert excinfo.value.code == 4
    assert not cfg.state_file.exists()


async def test_real_wiring_runs_description_and_teardown_on_same_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    threads: list[int] = []

    class ThreadDriver(SimDriver):
        def describe(self) -> Body | None:
            assert "NOTIFY_SOCKET" not in os.environ
            threads.append(threading.get_ident())
            return None

        def detach(self) -> None:
            threads.append(threading.get_ident())
            super().detach()

    class FakeSocket:
        def __init__(self) -> None:
            self.sent: list[bytes] = []
            self.closed = False

        def setblocking(self, value: bool) -> None:
            assert value is False

        def sendto(self, raw: bytes, destination: tuple[str, int]) -> None:
            self.sent.append(raw)

        def close(self) -> None:
            self.closed = True

    sock = FakeSocket()
    driver = ThreadDriver()
    inbox: asyncio.Queue[tuple[bytes, tuple[str, int]]] = asyncio.Queue()

    original_recvfrom = asyncio.get_running_loop().sock_recvfrom

    async def recvfrom(source: Any, size: int) -> Any:
        if source is sock:
            return await inbox.get()
        return await original_recvfrom(source, size)

    monkeypatch.setattr("yoke.net.runner.build_driver", lambda cfg: driver)
    monkeypatch.setattr("yoke.net.runner.open_multicast_socket", lambda *a, **kw: sock)
    monkeypatch.setattr(asyncio.get_running_loop(), "sock_recvfrom", recvfrom)
    cfg = AgentConfig(
        node=NodeConfig(node_id="threaded-1", role=Role.CONSUMER, kind=Kind.HDMI),
        state_file=tmp_path / "state.json",
        psk=b"k",
        allow_new_state=True,
        driver={"type": "sim"},
    )
    receiver = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    notify_path = str(tmp_path.parent / ("notify-" + uuid.uuid4().hex[:8]))
    receiver.bind(notify_path)
    receiver.setblocking(False)
    monkeypatch.setenv("NOTIFY_SOCKET", notify_path)
    task = asyncio.create_task(run_agent_async(cfg))
    try:
        async with asyncio.timeout(1):
            ready, _addr = await asyncio.get_running_loop().sock_recvfrom(receiver, 64)
        assert ready == b"READY=1"
        assert cfg.state_file.exists()
        await eventually(lambda: len(sock.sent) >= 2)
        # A second process targeting the same state must be rejected
        # before it can describe/drive hardware or reserve sequences.
        with pytest.raises(SystemExit) as excinfo:
            await run_agent_async(cfg)
        assert excinfo.value.code == 4
        assert len(threads) == 1
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        receiver.close()
    assert sock.closed
    assert len(threads) == 2  # describe, then orderly consumer detach
    assert threads[0] == threads[1]
    assert threads[0] != threading.get_ident()


async def test_run_agent_async_runs_and_cancels_cleanly(tmp_path: Path) -> None:
    cfg = AgentConfig(
        node=NodeConfig(node_id="smoke-1", role=Role.CONSUMER, kind=Kind.HDMI),
        state_file=tmp_path / "state.json",
        psk=b"k",
        allow_new_state=True,
        port=29754,  # high port: never collide with a real agent
        driver={"type": "sim"},
    )
    task = asyncio.create_task(run_agent_async(cfg))
    await asyncio.sleep(0.2)
    if task.done():
        exc = task.exception()
        if isinstance(exc, OSError):  # pragma: no cover - environment-dependent
            pytest.skip(f"multicast unavailable in this environment: {exc}")
        raise AssertionError(f"agent died unexpectedly: {exc!r}")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # start() persisted through the real statefile before anything else
    assert (tmp_path / "state.json").exists()


# --- producer lifecycle (usbip bind/unbind) ---------------------------------


def make_bind_harness() -> tuple[
    AgentRunner, FakeRunner, FakeTransport, list[LogEntry]
]:
    log: list[LogEntry] = []
    clock = ManualClock()
    transport = FakeTransport(log)
    inbox: asyncio.Queue[tuple[bytes, tuple[str, int]]] = asyncio.Queue()
    fake = FakeRunner()
    bound: set[str] = set()

    def bind(argv: Sequence[str]) -> CommandResult:
        bound.add(argv[-1])
        return CommandResult(0, "", "")

    def unbind(argv: Sequence[str]) -> CommandResult:
        bound.discard(argv[-1])
        return CommandResult(0, "", "")

    fake.script(["usbip", "bind"], bind)
    fake.script(["usbip", "unbind"], unbind)
    fake.script(
        ["usbip", "--tcp-port", "3240", "list", "-r", "127.0.0.1"],
        CommandResult(0, " 1-1: test USB device", ""),
    )
    driver = UsbipBindDriver(
        fake,
        host="10.0.0.9",
        busids=["1-1"],
        export_status=lambda busid: 1 if busid in bound else 0,
    )
    node = Node(
        NodeConfig(node_id="usb-prod", role=Role.PRODUCER, kind=Kind.USB),
        state=PersistedState(),
        now=clock(),
        media=driver.describe(),
        seed=7,
    )
    runner = AgentRunner(
        node,
        transport=transport,
        recv=inbox.get,
        driver=driver,
        psk=PSK,
        state_path=Path("/nonexistent/agent-state.json"),
        clock=clock,
        save_state=lambda path, state: None,
        max_sleep=0.02,
        driver_poll_interval=30.0,
    )
    return runner, fake, transport, log


async def test_usbip_producer_binds_in_background_and_unbinds_at_stop() -> None:
    runner, fake, transport, _log = make_bind_harness()
    task = asyncio.create_task(runner.run())
    try:
        await eventually(
            lambda: (
                bool(transport.sent) and ("usbip", "bind", "-b", "1-1") in fake.calls
            )
        )
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert ("usbip", "unbind", "-b", "1-1") in fake.calls
    types = [decode(raw, PSK).type for raw in transport.sent]
    assert FrameType.BYE in types  # bye still goes out around unbind


async def test_usbip_producer_bind_failure_surfaces_as_driver_error() -> None:
    runner, fake, transport, _log = make_bind_harness()
    fake.scripts.clear()
    fake.script(["usbip", "bind"], CommandResult(1, "", "device busy"))
    fake.script(["usbip", "unbind"], CommandResult(0, "", ""))
    task = asyncio.create_task(runner.run())
    await eventually(
        lambda: any(
            decode(raw, PSK).type is FrameType.EVT
            and decode(raw, PSK).body.get("event") == "driver-error"
            for raw in transport.sent
        )
    )
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def test_run_agent_returns_zero_on_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(coro: Any, **kwargs: Any) -> None:
        coro.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(asyncio, "run", fake_run)
    cfg = AgentConfig(
        node=NodeConfig(node_id="kb-1", role=Role.CONSUMER, kind=Kind.HDMI),
        state_file=Path("/nonexistent/state.json"),
        psk=b"",
        driver={"type": "sim"},
    )
    assert run_agent(cfg) == 0


# --- ADR 0023/0024: wall clock, source address and local logging -------------


NS = 1_000_000_000


async def test_runner_rereads_wall_clock_for_each_frame() -> None:
    """An agent started before NTP recovers when the clock steps, no restart."""
    async with running_agent() as h:
        await eventually(lambda: len(h.transport.sent) >= 2)
        h.wall_clock.now = T0 - 7200  # the OS clock is two hours behind
        ann = encode(FrameType.ANN, "prod-1", int(T0) * NS + 5, T0, PROD_ANN, PSK)
        await h.inbox.put((ann, ADDR))
        await eventually(lambda: h.node._drops["stale"] == 1)
        assert "prod-1" not in h.node.peers
        h.wall_clock.now = T0  # NTP stepped the clock
        ann = encode(FrameType.ANN, "prod-1", int(T0) * NS + 6, T0, PROD_ANN, PSK)
        await h.inbox.put((ann, ADDR))
        await eventually(lambda: "prod-1" in h.node.peers)


class AdmittingDriver(SimDriver):
    def __init__(self, media: Body) -> None:
        super().__init__(media=media)
        self.admitted: list[tuple[tuple[str, str], ...]] = []

    def admit(self, peers: tuple[tuple[str, str], ...]) -> None:
        self.admitted.append(peers)


async def test_runner_forwards_admission_with_datagram_source() -> None:
    driver = AdmittingDriver(PROD_MEDIA)
    async with running_agent(
        role=Role.PRODUCER, node_id="prod-1", driver=driver, admission=True
    ) as h:
        await eventually(lambda: driver.admitted == [()])
        claim: Body = {
            "role": "consumer",
            "kind": "hdmi",
            "name": "tv",
            "state": "pending",
            "err": None,
            "pairing": {"producer": "prod-1", "version": [1, "cli-1"]},
            "media": None,
        }
        for seq in (1, 2):
            raw = encode(FrameType.ANN, "tv-1", seq, T0, claim, PSK)
            await h.inbox.put((raw, ("10.0.0.44", 9753)))
        await eventually(lambda: driver.admitted[-1] == (("tv-1", "10.0.0.44"),))


def test_configure_logging_formats_for_journal_and_console() -> None:
    import io

    from yoke.net.runner import configure_logging

    logger = logging.getLogger("yoke")
    saved_level = logger.level
    journal, console = io.StringIO(), io.StringIO()
    try:
        configure_logging(journal, environ={"JOURNAL_STREAM": "8:9"})
        logging.getLogger("yoke.agent").warning("driver error: %s", "x" * 300)
        assert journal.getvalue() == f"<4>yoke.agent: driver error: {'x' * 300}\n"
        configure_logging(console, environ={"YOKE_LOG_LEVEL": "debug"})
        logging.getLogger("yoke.agent").debug("detail")
        assert "DEBUG yoke.agent: detail" in console.getvalue()
        # The replacement handler does not duplicate output.
        assert journal.getvalue().count("driver error") == 1
        assert [h.get_name() for h in logger.handlers].count("yoke-agent") == 1
    finally:
        for handler in list(logger.handlers):
            if handler.get_name() == "yoke-agent":
                logger.removeHandler(handler)
        logger.setLevel(saved_level)


async def test_core_log_levels_reach_the_agent_logger(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="yoke.agent"):
        async with running_agent() as h:
            await eventually(lambda: len(h.transport.sent) >= 2)
            await h.inbox.put((pair_intent(), ADDR))
            await eventually(lambda: len(h.acks()) >= 1)
    messages = {(r.levelno, r.message) for r in caplog.records}
    assert any(
        level == logging.INFO and m.startswith("pairing committed: producer=prod-1")
        for level, m in messages
    )
