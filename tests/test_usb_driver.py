"""The metadata-only usb-gadget driver, the worker and their control channel.

Runs the real chain offline: driver -> Unix socket -> worker -> proxy session
-> simulated UDC/host, with UDC sysfs evidence in a temporary directory.
Abstract socket names avoid sockaddr_un path limits under deep temp paths.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import textwrap
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.usbsim import build
from tests.usbsim.device import DeviceModel, Verdict
from tests.usbsim.harness import Transport
from tests.usbsim.host import UsbHost
from tests.usbsim.udc import FakeRawGadgetKernel
from tests.usbsim.usbipd import FakeUsbipd
from yoke.config import AgentConfig, ConfigError
from yoke.core.types import Body, Kind, NodeConfig, NodeState, Role
from yoke.drivers.base import Driver, DriverError
from yoke.drivers.factory import build_driver
from yoke.drivers.gadget import UsbGadgetDriver, target_from_media
from yoke.usb import control
from yoke.usb.ch9 import Speed
from yoke.usb.control import ControlClient, ControlError, Route, Target
from yoke.usb.policy import ProxyPolicy
from yoke.usb.proxy import Proxy, TeardownError
from yoke.usb.session import open_session
from yoke.usb.worker import BuildTicket, ControlServer, Worker

MEDIA: Body = {"host": "192.168.7.10", "port": 3240, "busids": ["1-1"]}


class Echo(DeviceModel):
    def __init__(self, device_class: int = 0) -> None:
        body = (
            build.interface(0, endpoints=2, cls=0xFF)
            + build.endpoint(0x81, 2, 512)
            + build.endpoint(0x02, 2, 512)
        )
        super().__init__(
            speed=Speed.HIGH,
            device=build.device(
                vendor=0x1209, product=0x4242, device_class=device_class
            ),
            configurations=[build.configuration(body, interfaces=1)],
            strings={1: "Yoke", 2: "Echo", 3: "E1"},
            qualifier=build.qualifier(),
        )

    def on_out(self, address: int, packet: bytes) -> Verdict:
        self.in_pipe(0x81).packets.append(packet)
        self.in_pipe(0x81).pump()
        return Verdict.ACCEPT


@dataclass
class Bench:
    kernel: FakeRawGadgetKernel
    host: UsbHost
    usbipd: FakeUsbipd
    worker: Worker
    server: ControlServer
    driver: UsbGadgetDriver
    socket: str
    sysfs: Path
    stop: threading.Event
    thread: threading.Thread

    def wait_state(self, state: NodeState, text: str = "", timeout: float = 5.0) -> str:
        deadline = time.monotonic() + timeout
        status = self.driver.poll()
        while (
            status.state is not state or text not in status.detail
        ) and time.monotonic() < deadline:
            time.sleep(0.01)
            status = self.driver.poll()
        assert status.state is state and text in status.detail, status
        return status.detail


def sim_factory(
    usbipd: FakeUsbipd, kernel: FakeRawGadgetKernel
) -> Callable[[Target], Proxy]:
    transport = Transport()

    def factory(target: Target, ticket: BuildTicket) -> Proxy:
        stream = transport.connect(usbipd)
        ticket.register(stream)
        return open_session(
            stream, target.busid, kernel.backend(), ProxyPolicy(stop_timeout=1.0)
        )

    return factory


def make_bench(tmp_path: Path, device: DeviceModel) -> Bench:
    sysfs = tmp_path / "udc"
    kernel = FakeRawGadgetKernel(sysfs_root=sysfs)
    usbipd = FakeUsbipd({"1-1": device})
    worker = Worker(sim_factory(usbipd, kernel), udc=kernel.name, retry_min=0.02)
    name = f"\0yoke-gadget-test-{uuid.uuid4().hex}"
    server = ControlServer(name, worker)
    server.start()
    stop = threading.Event()
    thread = threading.Thread(target=worker.run, args=(stop, 0.01), daemon=True)
    thread.start()
    driver = UsbGadgetDriver(
        udc=kernel.name, socket_path=name, sysfs_udc=sysfs, ack_timeout=5.0
    )
    return Bench(
        kernel,
        UsbHost(kernel),
        usbipd,
        worker,
        server,
        driver,
        name,
        sysfs,
        stop,
        thread,
    )


@pytest.fixture
def bench(tmp_path: Path) -> Iterator[Bench]:
    bench = make_bench(tmp_path, Echo())
    yield bench
    bench.stop.set()
    bench.thread.join(2)
    bench.worker.shutdown()
    bench.server.close()
    bench.usbipd.stop()


def make_cfg(role: Role, driver: Body, kind: Kind = Kind.USB) -> AgentConfig:
    return AgentConfig(
        node=NodeConfig(node_id="desk-usb", role=role, kind=kind),
        state_file=Path("/nonexistent/state.json"),
        psk=b"",
        driver=driver,
    )


def test_factory_builds_metadata_driver() -> None:
    driver = build_driver(
        make_cfg(Role.CONSUMER, {"type": "usb-gadget", "udc": "fcc00000.usb"})
    )
    assert isinstance(driver, UsbGadgetDriver) and isinstance(driver, Driver)
    assert driver.describe() is None


@pytest.mark.parametrize(
    ("role", "kind", "table", "match"),
    [
        (Role.PRODUCER, Kind.USB, {"udc": "u"}, "usb consumer"),
        (Role.CONSUMER, Kind.HDMI, {"udc": "u"}, "usb consumer"),
        (Role.CONSUMER, Kind.USB, {}, r"driver\.udc"),
        (Role.CONSUMER, Kind.USB, {"udc": "../x"}, r"driver\.udc"),
        (Role.CONSUMER, Kind.USB, {"udc": "u", "busid": "../1"}, r"driver\.busid"),
        (Role.CONSUMER, Kind.USB, {"udc": "u", "socket": "rel"}, r"driver\.socket"),
        (Role.CONSUMER, Kind.USB, {"udc": "u", "ack_timeout": 0}, "ack_timeout"),
        (Role.CONSUMER, Kind.USB, {"udc": "u", "extra": 1}, r"driver\.extra"),
    ],
)
def test_factory_validation(role: Role, kind: Kind, table: Body, match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        build_driver(make_cfg(role, {"type": "usb-gadget", **table}, kind))


def test_target_selection_from_producer_media() -> None:
    assert target_from_media(MEDIA, None) == Target("192.168.7.10", 3240, "1-1")
    two = {**MEDIA, "busids": ["1-1", "1-2"]}
    with pytest.raises(DriverError, match=r"Set driver\.busid"):
        target_from_media(two, None)
    assert target_from_media(two, "1-2").busid == "1-2"
    with pytest.raises(DriverError, match="does not advertise"):
        target_from_media(MEDIA, "2-1")
    with pytest.raises(DriverError, match="host"):
        target_from_media({**MEDIA, "host": "-bad"}, None)


def test_control_messages_are_closed_schemas() -> None:
    route = Route(3, Target("10.0.0.2", 3240, "1-1.2"))
    assert (
        control.parse_request(
            control.decode(control.encode(control.route_request(route)))
        )
        == route
    )
    for bad in (
        {"schema": 1, "op": "route", "generation": 1, "target": None, "data": "x"},
        {"schema": 2, "op": "status"},
        {"schema": 1, "op": "payload"},
    ):
        with pytest.raises(ControlError):
            control.parse_request(bad)
    with pytest.raises(ControlError):
        control.decode(b"[]\n")
    with pytest.raises(ControlError):
        Route(0, None)


def test_attach_enumerate_active_detach(bench: Bench) -> None:
    assert bench.driver.poll().state is NodeState.IDLE
    bench.driver.attach("prod-usb", MEDIA)
    bench.wait_state(NodeState.PENDING, "awaiting computer enumeration")
    bench.host.attach()
    bench.host.enumerate("windows")
    detail = bench.wait_state(NodeState.ACTIVE)
    assert "1209:4242" in detail and "high-speed" in detail
    assert "state configured" in detail and "unverified" in detail
    bench.driver.detach()
    assert bench.driver.poll().state is NodeState.IDLE
    assert not bench.kernel.bound  # gadget closed: the computer saw a disconnect
    bench.driver.detach()  # repeated success is harmless


def test_repeated_attach_is_idempotent_and_survives_driver_restart(
    bench: Bench,
) -> None:
    bench.driver.attach("prod-usb", MEDIA)
    bench.host.attach()
    bench.host.enumerate("linux")
    bench.wait_state(NodeState.ACTIVE)
    session = bench.worker._proxy
    generation = bench.worker.status().generation
    bench.driver.attach("prod-usb", MEDIA)  # producer flapped back to active
    fresh = UsbGadgetDriver(
        udc=bench.kernel.name, socket_path=bench.socket, sysfs_udc=bench.sysfs
    )
    fresh.attach("prod-usb", MEDIA)  # agent restart: adopts, no re-enumeration
    assert bench.worker._proxy is session
    assert bench.worker.status().generation == generation
    assert fresh.poll().state is NodeState.ACTIVE
    fresh.detach()
    fresh.detach()
    assert bench.worker.status().generation == generation + 1


def test_source_address_reaches_the_worker_and_config(bench: Bench) -> None:
    sourced = UsbGadgetDriver(
        udc=bench.kernel.name, socket_path=bench.socket, source="10.20.30.2"
    )
    sourced.attach("prod-usb", MEDIA)
    status = bench.worker.status()
    assert status.target is not None and status.target.source == "10.20.30.2"
    cfg = make_cfg(Role.CONSUMER, {"type": "usb-gadget", "udc": "u"})
    vlan = AgentConfig(
        node=cfg.node,
        state_file=cfg.state_file,
        psk=b"",
        interface="10.20.30.2",
        driver=cfg.driver,
    )
    driver = build_driver(vlan)
    assert isinstance(driver, UsbGadgetDriver) and driver._source == "10.20.30.2"
    with pytest.raises(ConfigError, match=r"driver\.source"):
        build_driver(
            make_cfg(Role.CONSUMER, {"type": "usb-gadget", "udc": "u", "source": "::1"})
        )


def test_worker_restart_is_detected_by_generation(bench: Bench) -> None:
    bench.driver.attach("prod-usb", MEDIA)
    bench.wait_state(NodeState.PENDING)
    bench.worker.shutdown()
    replacement = Worker(lambda t, k: (_ for _ in ()).throw(OSError("down")), udc="u")
    bench.server.worker = replacement
    status = bench.driver.poll()
    assert status.state is NodeState.ERROR and "route lost" in status.detail
    bench.server.worker = bench.worker
    bench.driver.attach("prod-usb", MEDIA)
    bench.wait_state(NodeState.PENDING)


def test_stale_or_unreachable_worker_is_an_error(bench: Bench) -> None:
    bench.driver.attach("prod-usb", MEDIA)
    late = UsbGadgetDriver(
        udc="u", socket_path=bench.socket, clock=lambda: time.time() + 60
    )
    late._routed = True
    assert "stale" in late.poll().detail
    gone = UsbGadgetDriver(udc="u", socket_path="\0yoke-no-such-worker")
    assert gone.poll().state is NodeState.ERROR
    with pytest.raises(DriverError, match="unreachable"):
        gone.attach("prod-usb", MEDIA)


def test_unconfirmed_teardown_retains_route_and_blocks_replacement(
    bench: Bench,
) -> None:
    bench.driver.attach("prod-usb", MEDIA)
    bench.wait_state(NodeState.PENDING, "awaiting computer enumeration")
    proxy = bench.worker._proxy
    assert proxy is not None
    real_stop = proxy.stop

    def stuck() -> None:
        raise TeardownError("endpoint pumps did not stop: 0x81")

    proxy.stop = stuck  # type: ignore[method-assign]
    with pytest.raises(DriverError, match="unconfirmed"):
        bench.driver.detach()
    assert bench.driver.poll().state is NodeState.ERROR
    assert bench.kernel.bound  # old session still owns the UDC
    with pytest.raises(DriverError):
        bench.driver.attach("prod-usb", {**MEDIA, "host": "192.168.7.11"})
    proxy.stop = real_stop  # type: ignore[method-assign]
    bench.driver.detach()
    assert not bench.kernel.bound


def test_rejected_device_is_reported(tmp_path: Path) -> None:
    bench = make_bench(tmp_path, Echo(device_class=9))
    try:
        bench.driver.attach("prod-usb", MEDIA)
        detail = bench.wait_state(NodeState.ERROR)
        assert "rejected" in detail and "hub" in detail
        assert not bench.kernel.bound
    finally:
        bench.stop.set()
        bench.thread.join(2)
        bench.server.close()


def test_raw_control_client_status(bench: Bench) -> None:
    status = ControlClient(bench.socket, timeout=2).status()
    assert status.generation == 0 and status.state == "idle"
    assert status.udc == bench.kernel.name


BLOCKED = (
    "yoke.usb.proxy",
    "yoke.usb.client",
    "yoke.usb.rawgadget",
    "yoke.usb.session",
    "yoke.usb.worker",
    "yoke.usb.policy",
    "yoke.usb.descriptors",
    "yoke.usb.gadget",
    "yoke.usb.usbip",
)


def test_agent_side_never_imports_payload_modules() -> None:
    """ADR 0002/0011: the driver and factory load only metadata modules."""
    guard = f"""import importlib.abc, sys
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name in {BLOCKED!r}:
            raise AssertionError("payload module imported: " + name)
sys.meta_path.insert(0, Guard())
"""
    program = textwrap.dedent(
        """
        import yoke.drivers.factory
        from yoke.drivers.gadget import UsbGadgetDriver
        UsbGadgetDriver(udc="fcc00000.usb").describe()
        """
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", guard + program],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_worker_connects_over_ipv4_from_the_announced_source() -> None:
    from tests.usbsim.devices import Loopback
    from yoke.usb.client import import_device
    from yoke.usb.worker import connect_usbip

    usbipd = FakeUsbipd({"1-1": Loopback()})
    _, port = usbipd.listen()
    try:
        sock = connect_usbip(
            Target("localhost", port, "1-1", source="127.0.0.1"), timeout=2
        )
        assert sock.family == socket.AF_INET
        assert sock.getsockname()[0] == "127.0.0.1"
        assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)
        assert import_device(sock, "1-1", timeout=2).busid == "1-1"
        sock.close()
    finally:
        usbipd.stop()
