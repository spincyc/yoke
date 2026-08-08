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
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from yoke.config import AgentConfig, ConfigError
from yoke.core.frames import decode, encode
from yoke.core.node import Node
from yoke.core.types import (
    Body,
    Frame,
    FrameType,
    Kind,
    NodeConfig,
    PersistedState,
    Role,
    SendFrame,
    SendRaw,
)
from yoke.drivers.igmp import IgmpJoinDriver
from yoke.drivers.jap import JapDecoderDriver
from yoke.drivers.shellcmd import CommandResult, FakeRunner
from yoke.drivers.sim import SimDriver
from yoke.drivers.usbip import UsbipAttachDriver, UsbipBindDriver
from yoke.net.runner import AgentRunner, build_driver, run_agent, run_agent_async

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
) -> Harness:
    log: list[LogEntry] = []
    saved: list[PersistedState] = []
    clock = ManualClock()
    transport = FakeTransport(log)
    inbox: asyncio.Queue[tuple[bytes, tuple[str, int]]] = asyncio.Queue()
    sim = driver if driver is not None else SimDriver()
    node = Node(
        NodeConfig(node_id=node_id, role=role, kind=Kind.HDMI, name="test"),
        state=PersistedState(),
        now=clock(),
        media=sim.describe(),
        seed=7,
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
        save_state=save_state if save_state is not None else default_save,
        max_sleep=0.02,
        driver_poll_interval=driver_poll_interval,
    )
    return Harness(
        runner=runner,
        node=node,
        driver=sim,
        transport=transport,
        inbox=inbox,
        clock=clock,
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


def pair_intent(version: int = 1, seq: int = 1) -> bytes:
    body: Body = {
        "op": "pair",
        "consumer": "cons-1",
        "producer": "prod-1",
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


async def test_persist_failure_keeps_agent_running_and_acking() -> None:
    def broken_save(path: Path, state: PersistedState) -> None:
        raise OSError("disk full")

    async with running_agent(save_state=broken_save) as h:
        await h.inbox.put((pair_intent(version=1, seq=1), ADDR))
        await eventually(lambda: len(h.acks()) >= 1)
        unpair: Body = {"op": "unpair", "consumer": "cons-1", "version": [2, "cli-1"]}
        await h.inbox.put((encode(FrameType.INT, "cli-1", 2, T0, unpair, PSK), ADDR))
        await eventually(lambda: len(h.acks()) >= 2)
        assert [a.body["version"] for a in h.acks()] == [[1, "cli-1"], [2, "cli-1"]]
        assert h.task is not None and not h.task.done()


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

    driver = ExplodingDriver()
    h = make_harness(driver=driver, driver_poll_interval=0.02)
    h.task = asyncio.create_task(h.runner.run())
    try:
        await eventually(lambda: "driver-error" in h.events())
        err = next(f for f in h.frames() if f.body.get("event") == "driver-error")
        assert "device on fire" in err.body["detail"]["detail"]
    finally:
        h.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await h.task


# --- build_driver factory ---------------------------------------------------


def make_cfg(role: Role, driver: Body, kind: Kind = Kind.HDMI) -> AgentConfig:
    return AgentConfig(
        node=NodeConfig(node_id="n-1", role=role, kind=kind, name="n"),
        state_file=Path("/nonexistent/state.json"),
        psk=b"",
        driver=driver,
    )


def test_build_driver_sim_consumer() -> None:
    driver = build_driver(make_cfg(Role.CONSUMER, {"type": "sim"}))
    assert isinstance(driver, SimDriver)
    assert driver.describe() is None


def test_build_driver_sim_producer_media() -> None:
    media = {"group": "239.1.2.3", "port": 5004}
    driver = build_driver(make_cfg(Role.PRODUCER, {"type": "sim", "media": media}))
    assert isinstance(driver, SimDriver)
    assert driver.describe() == media


def test_build_driver_sim_bad_media() -> None:
    with pytest.raises(ConfigError, match=r"driver\.media"):
        build_driver(make_cfg(Role.PRODUCER, {"type": "sim", "media": "nope"}))


def test_build_driver_igmp() -> None:
    driver = build_driver(make_cfg(Role.CONSUMER, {"type": "igmp"}))
    assert isinstance(driver, IgmpJoinDriver)


def test_build_driver_usbip_consumer() -> None:
    driver = build_driver(make_cfg(Role.CONSUMER, {"type": "usbip"}, kind=Kind.USB))
    assert isinstance(driver, UsbipAttachDriver)


def test_build_driver_usbip_producer() -> None:
    table: Body = {
        "type": "usbip",
        "host": "10.0.0.9",
        "port": 3240,
        "busids": ["1-1", "1-1.2"],
    }
    driver = build_driver(make_cfg(Role.PRODUCER, table, kind=Kind.USB))
    assert isinstance(driver, UsbipBindDriver)
    assert driver.describe() == {
        "host": "10.0.0.9",
        "port": 3240,
        "busids": ["1-1", "1-1.2"],
    }


@pytest.mark.parametrize("missing", ["host", "port", "busids"])
def test_build_driver_usbip_producer_missing_key(missing: str) -> None:
    table: Body = {"type": "usbip", "host": "h", "port": 3240, "busids": ["1-1"]}
    del table[missing]
    with pytest.raises(ConfigError, match=f"driver.{missing}"):
        build_driver(make_cfg(Role.PRODUCER, table, kind=Kind.USB))


def test_build_driver_jap_consumer() -> None:
    driver = build_driver(
        make_cfg(Role.CONSUMER, {"type": "jap", "decoder_host": "10.0.0.20"})
    )
    assert isinstance(driver, JapDecoderDriver)


def test_build_driver_jap_consumer_missing_decoder_host() -> None:
    with pytest.raises(ConfigError, match=r"driver\.decoder_host"):
        build_driver(make_cfg(Role.CONSUMER, {"type": "jap"}))


def test_build_driver_jap_producer_is_announce_only() -> None:
    driver = build_driver(make_cfg(Role.PRODUCER, {"type": "jap", "channel": 5}))
    assert isinstance(driver, SimDriver)
    assert driver.describe() == {"channel": 5, "group": "239.92.0.5"}


def test_build_driver_jap_producer_missing_channel() -> None:
    with pytest.raises(ConfigError, match=r"driver\.channel"):
        build_driver(make_cfg(Role.PRODUCER, {"type": "jap"}))


def test_build_driver_jap_producer_channel_out_of_range() -> None:
    with pytest.raises(ConfigError, match=r"driver\.channel"):
        build_driver(make_cfg(Role.PRODUCER, {"type": "jap", "channel": 70000}))


def test_build_driver_unknown_type() -> None:
    with pytest.raises(ConfigError, match="frobnicator"):
        build_driver(make_cfg(Role.CONSUMER, {"type": "frobnicator"}))


def test_build_driver_missing_type() -> None:
    with pytest.raises(ConfigError, match=r"driver\.type"):
        build_driver(make_cfg(Role.CONSUMER, {}))


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


async def test_run_agent_async_runs_and_cancels_cleanly(tmp_path: Path) -> None:
    cfg = AgentConfig(
        node=NodeConfig(node_id="smoke-1", role=Role.CONSUMER, kind=Kind.HDMI),
        state_file=tmp_path / "state.json",
        psk=b"k",
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
    fake.script(["usbip", "bind"], CommandResult(0, "", ""))
    fake.script(["usbip", "unbind"], CommandResult(0, "", ""))
    driver = UsbipBindDriver(fake, host="10.0.0.9", busids=["1-1"])
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


async def test_usbip_producer_binds_at_start_and_unbinds_at_stop() -> None:
    runner, fake, transport, _log = make_bind_harness()
    task = asyncio.create_task(runner.run())
    await eventually(lambda: ("usbip", "bind", "-b", "1-1") in fake.calls)
    # bind precedes the first announce
    assert transport.sent, "expected started/ann frames after bind"
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
