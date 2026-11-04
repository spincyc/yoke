"""End-to-end rig: host, simulated UDC, proxy, USB/IP, exporter and device."""

from __future__ import annotations

import socket
from dataclasses import dataclass, field
from pathlib import Path

from tests.usbsim.device import DeviceModel
from tests.usbsim.host import UsbHost
from tests.usbsim.streams import MemoryLink, TcpRelay, memory_pair
from tests.usbsim.udc import FakeRawGadgetKernel
from tests.usbsim.usbipd import FakeUsbipd
from yoke.usb.ch9 import Speed
from yoke.usb.client import ByteStream
from yoke.usb.gadget import UdcEndpoint
from yoke.usb.policy import ProxyPolicy
from yoke.usb.proxy import Proxy
from yoke.usb.session import open_session

BUSID = "1-1"


@dataclass
class Transport:
    """How the proxy reaches the exporter."""

    kind: str = "memory"  # "memory" or "tcp"
    latency: float = 0.0
    jitter: float = 0.0
    seed: int = 0
    links: list[MemoryLink] = field(default_factory=list)
    relay: TcpRelay | None = None
    address: tuple[str, int] | None = None

    def connect(self, usbipd: FakeUsbipd) -> ByteStream:
        if self.kind == "memory":
            client, server, link = memory_pair(self.latency, self.jitter, self.seed)
            self.links.append(link)
            usbipd.spawn(server)
            return client
        if self.address is None:
            self.address = usbipd.listen()
            if self.latency or self.jitter:
                self.relay = TcpRelay(
                    self.address, latency=self.latency, jitter=self.jitter
                )
        target = self.relay.address if self.relay is not None else self.address
        sock = socket.create_connection(target, timeout=5)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return sock

    def reset(self) -> None:
        """Abort the live connection (ECONNRESET / TCP RST)."""
        if self.kind == "memory":
            self.links[-1].reset()
        elif self.relay is not None:
            self.relay.reset_all()
        else:
            raise RuntimeError("TCP reset injection needs a relay")

    def close(self) -> None:
        if self.relay is not None:
            self.relay.close()


@dataclass
class Rig:
    device: DeviceModel
    usbipd: FakeUsbipd
    kernel: FakeRawGadgetKernel
    host: UsbHost
    policy: ProxyPolicy
    transport: Transport
    proxy: Proxy | None = None
    logs: list[str] = field(default_factory=list)

    def connect(self) -> Proxy:
        stream = self.transport.connect(self.usbipd)
        self.proxy = open_session(
            stream, BUSID, self.kernel.backend(), self.policy, log=self.logs.append
        )
        return self.proxy

    def stop(self) -> None:
        if self.proxy is not None:
            self.proxy.stop()

    def close(self) -> None:
        try:
            if self.proxy is not None and self.proxy.state != "stopped":
                self.proxy.stop()
        finally:
            self.usbipd.stop()
            self.transport.close()


def make_rig(
    device: DeviceModel,
    *,
    policy: ProxyPolicy | None = None,
    transport: Transport | None = None,
    host_speed: Speed = Speed.HIGH,
    udc_handles_standard: bool = True,
    sysfs_root: Path | None = None,
    service_delay: float = 0.0,
    reset_delay: float = 0.0,
    udc_endpoints: list[UdcEndpoint] | None = None,
    connect: bool = True,
) -> Rig:
    usbipd = FakeUsbipd(
        {BUSID: device}, service_delay=service_delay, reset_delay=reset_delay
    )
    kernel = FakeRawGadgetKernel(
        udc_handles_standard=udc_handles_standard,
        sysfs_root=sysfs_root,
        endpoints=udc_endpoints,
    )
    rig = Rig(
        device=device,
        usbipd=usbipd,
        kernel=kernel,
        host=UsbHost(kernel, max_speed=host_speed),
        policy=policy or ProxyPolicy(),
        transport=transport or Transport(),
    )
    if connect:
        rig.connect()
    return rig
