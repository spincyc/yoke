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
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.bind(("", port))
    mreq = struct.pack("4s4s", socket.inet_aton(group), socket.inet_aton(interface))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1 if loopback else 0)
    return sock
