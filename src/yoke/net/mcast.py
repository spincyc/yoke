"""Multicast socket setup for the control group.

One deliberately small function: everything above it (agent runner, CLI,
snooper) treats the result as a plain datagram socket, which keeps every
component testable with a fake socket object.
"""

from __future__ import annotations

import socket
import struct


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
    CLI, and a snooper share one host. IP_MULTICAST_LOOP stays on so
    same-host processes hear each other. TTL 1 keeps control traffic on
    the local network (PROTOCOL.md Transport).

    ``interface`` is the local IPv4 address of the NIC on the AV/control
    VLAN. On a multi-homed host it MUST be set (config key ``interface``)
    or the kernel picks the default-route interface for both the group
    membership and outbound multicast, leaving the agent deaf and mute on
    the wrong segment. ``0.0.0.0`` means "let the kernel choose" and is
    correct only on single-homed hosts.
    """
    if_addr = socket.inet_aton(interface)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.bind(("", port))
    mreq = struct.pack("4s4s", socket.inet_aton(group), if_addr)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    # Select the outbound multicast interface too, not just the
    # membership interface, so sends leave the right NIC on multi-homed
    # hosts.
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, if_addr)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1 if loopback else 0)
    return sock
