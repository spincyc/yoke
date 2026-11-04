"""Multicast socket setup for the control group.

One deliberately small function: everything above it (agent runner, CLI,
snooper) treats the result as a plain datagram socket, which keeps every
component testable with a fake socket object.
"""

from __future__ import annotations

import socket
import struct
import sys
from ipaddress import IPv4Address


def validate_multicast(
    group: str,
    port: int,
    *,
    interface: str = "0.0.0.0",
    ttl: int = 1,
    loopback: bool = True,
) -> None:
    """Validate literal addresses and socket options before allocating I/O."""
    try:
        if not isinstance(group, str) or not IPv4Address(group).is_multicast:
            raise ValueError
    except (ValueError, TypeError) as exc:
        raise ValueError("group must be an IPv4 multicast address") from exc
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("port must be an integer from 1 through 65535")
    try:
        addr = IPv4Address(interface)
        if not isinstance(interface, str) or (
            interface != "0.0.0.0"
            and (
                addr.is_multicast
                or addr.is_unspecified
                or addr.is_reserved
                or int(addr) >> 24 == 0
                or addr == IPv4Address("255.255.255.255")
            )
        ):
            raise ValueError
    except (ValueError, TypeError) as exc:
        raise ValueError("interface must be a unicast IPv4 address or 0.0.0.0") from exc
    if type(ttl) is not int or ttl != 1:
        raise ValueError("control multicast ttl must be 1")
    if type(loopback) is not bool:
        raise ValueError("loopback must be a boolean")


def open_multicast_socket(
    group: str,
    port: int,
    *,
    ttl: int = 1,
    loopback: bool = True,
    interface: str = "0.0.0.0",
) -> socket.socket:
    """Open a UDP socket joined to ``group``:``port``, ready to send and
    receive control frames.

    SO_REUSEADDR (and SO_REUSEPORT where available) lets an agent, the
    CLI, and a snooper share one host. The socket is bound to the group
    address, so only datagrams addressed to the group are received.
    IP_MULTICAST_LOOP stays on so same-host processes hear each other.
    TTL 1 keeps control traffic on the local network (PROTOCOL.md
    Transport).

    ``interface`` is the local IPv4 address of the NIC on the AV/control
    VLAN. On a multi-homed host it MUST be set (config key ``interface``)
    or the kernel picks the default-route interface for both the group
    membership and outbound multicast, leaving the agent deaf and mute on
    the wrong segment. ``0.0.0.0`` means "let the kernel choose" and is
    correct only on single-homed hosts.
    """
    validate_multicast(group, port, interface=interface, ttl=ttl, loopback=loopback)
    if_addr = socket.inet_aton(interface)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        if sys.platform.startswith("linux"):
            # Python need not expose this Linux socket option (ip(7)).
            # Wildcard-bound sockets otherwise hear other joined groups.
            sock.setsockopt(
                socket.IPPROTO_IP, getattr(socket, "IP_MULTICAST_ALL", 49), 0
            )
        # Binding the group address makes the kernel deliver only datagrams
        # addressed to the group: unicast to this host's control port is
        # never read (ADR 0023). Outbound frames still use the interface's
        # unicast source address. Windows cannot bind a multicast address.
        sock.bind(("" if sys.platform.startswith("win") else group, port))
        mreq = struct.pack("4s4s", socket.inet_aton(group), if_addr)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, if_addr)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, int(loopback))
    except BaseException:
        sock.close()
        raise
    return sock
