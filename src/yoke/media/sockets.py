"""IPv4 socket configuration for the standalone reference media shell."""

from __future__ import annotations

import ipaddress
import socket
import sys
from dataclasses import dataclass


def ipv4_address(value: str, *, multicast: bool = False) -> str:
    """Validate a literal IPv4 address without DNS or socket side effects."""
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError as exc:
        raise ValueError(f"invalid IPv4 address: {value!r}") from exc
    if address.is_multicast != multicast:
        kind = "multicast" if multicast else "unicast/interface"
        raise ValueError(f"expected {kind} IPv4 address: {value!r}")
    if not multicast and int(address) == 0xFFFFFFFF:
        raise ValueError("broadcast is not a unicast/interface address")
    return str(address)


def valid_port(port: int) -> int:
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("port must be in [1, 65535]")
    return port


@dataclass(frozen=True)
class MulticastOptions:
    group: str
    interface: str = "0.0.0.0"
    ttl: int = 1
    loopback: bool = True

    def __post_init__(self) -> None:
        ipv4_address(self.group, multicast=True)
        ipv4_address(self.interface)
        if type(self.ttl) is not int or not 0 <= self.ttl <= 255:
            raise ValueError("multicast TTL must be in [0, 255]")
        if type(self.loopback) is not bool:
            raise ValueError("multicast loopback must be boolean")


def _configure_multicast(sock: socket.socket, options: MulticastOptions) -> None:
    sock.setsockopt(
        socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(options.interface)
    )
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, options.ttl)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, int(options.loopback))


def open_receiver_socket(
    port: int, *, multicast: MulticastOptions | None = None
) -> socket.socket:
    """Bind media port; independent multicast listeners each receive a copy.

    Membership cleanup follows socket close, including failed setup. Senders
    must use an ephemeral port instead of joining this shared receive port.
    """
    valid_port(port)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        if multicast is not None:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if sys.platform.startswith("linux"):
                # Linux's default (1) also delivers other locally joined
                # groups on this port. UAPI linux/in.h defines option 49;
                # Python does not expose it on every supported version.
                all_groups = getattr(socket, "IP_MULTICAST_ALL", 49)
                sock.setsockopt(socket.IPPROTO_IP, all_groups, 0)
        sock.bind(("0.0.0.0", port))
        if multicast is not None:
            _configure_multicast(sock, multicast)
            membership = socket.inet_aton(multicast.group) + socket.inet_aton(
                multicast.interface
            )
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
        sock.setblocking(False)
        return sock
    except BaseException:
        sock.close()
        raise


def open_sender_socket(*, multicast: MulticastOptions | None = None) -> socket.socket:
    """Bind an ephemeral source port; select outbound multicast explicitly."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("0.0.0.0", 0))
        if multicast is not None:
            _configure_multicast(sock, multicast)
        sock.setblocking(False)
        return sock
    except BaseException:
        sock.close()
        raise
