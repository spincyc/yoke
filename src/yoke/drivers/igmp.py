"""Reference consumer driver: decoder-side multicast group membership.

In the standard AVoIP pattern (ARCHITECTURE.md) every encoder transmits
its stream to a fixed multicast group forever; "switching" a display is
purely the decoder host joining one group and leaving another. On a
switch with IGMP snooping, the kernel-level ``IP_ADD_MEMBERSHIP`` this
driver performs emits an IGMP membership report, which is what makes the
switch start forwarding the producer's stream to this host's port — the
attached renderer (a GStreamer pipeline, a hardware decoder fed from
this NIC, ...) then receives it.

That is the entire job: **membership only**. The socket is bound so the
kernel keeps the membership alive, but this driver never reads a byte
from it — yoke must never receive media payload (ADR 0002/0004).

The socket factory is injectable so tests exercise every path with a
recording fake and no real sockets.
"""

from __future__ import annotations

import ipaddress
import socket
import struct
from collections.abc import Callable
from typing import Protocol

from yoke.core.types import Body, DriverStatus, NodeState
from yoke.drivers.base import DriverError


class MembershipSocket(Protocol):
    """The slice of ``socket.socket`` this driver needs."""

    def setsockopt(self, level: int, optname: int, value: int | bytes, /) -> None: ...

    def bind(self, address: tuple[str, int], /) -> None: ...

    def close(self) -> None: ...


SocketFactory = Callable[[], MembershipSocket]


def _default_socket_factory() -> MembershipSocket:
    return socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


def _validate_media(media: Body) -> tuple[str, int]:
    """Check ``{"group": ipv4-multicast, "port": int}``; DriverError if bad."""
    group = media.get("group")
    port = media.get("port")
    if not isinstance(group, str):
        raise DriverError(f"igmp media: bad group: {group!r}")
    try:
        addr = ipaddress.IPv4Address(group)
    except ValueError as exc:
        raise DriverError(f"igmp media: bad group: {group!r}") from exc
    if not addr.is_multicast:  # 224.0.0.0/4
        raise DriverError(f"igmp media: group not multicast (224.0.0.0/4): {group}")
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
        raise DriverError(f"igmp media: bad port: {port!r}")
    return group, port


class IgmpJoinDriver:
    """Consumer driver holding one multicast group membership."""

    def __init__(self, socket_factory: SocketFactory = _default_socket_factory) -> None:
        self._socket_factory = socket_factory
        self._sock: MembershipSocket | None = None
        self._joined: tuple[str, int] | None = None
        self._last_error: str = ""

    def attach(self, producer: str, media: Body) -> None:
        """Join ``media``'s group. Re-attach leaves any current group
        first (closing the socket drops the membership), so a pairing
        change is a leave+join exactly as the AVoIP pattern expects."""
        try:
            group, port = _validate_media(media)
        except DriverError as exc:
            self._last_error = str(exc)
            raise
        self.detach()
        sock = self._socket_factory()
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("", port))
            mreq = struct.pack(
                "4s4s", socket.inet_aton(group), socket.inet_aton("0.0.0.0")
            )
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except OSError as exc:
            sock.close()
            self._last_error = f"join {group}:{port} failed: {exc}"
            raise DriverError(self._last_error) from exc
        self._sock = sock
        self._joined = (group, port)
        self._last_error = ""

    def detach(self) -> None:
        """Leave the group by closing the socket. Idempotent; also
        clears any recorded attach failure."""
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        self._joined = None
        self._last_error = ""

    def poll(self) -> DriverStatus:
        if self._last_error:
            return DriverStatus(NodeState.ERROR, self._last_error)
        if self._sock is not None and self._joined is not None:
            group, port = self._joined
            return DriverStatus(NodeState.ACTIVE, f"member of {group}:{port}")
        return DriverStatus(NodeState.IDLE)

    def describe(self) -> Body | None:
        return None
