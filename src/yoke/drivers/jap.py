"""Just Add Power decoder driver.

JAP endpoints implement yoke's model natively: every encoder transmits
continuously to a multicast group derived from its channel number, and a
decoder switches source by being told a channel — it IGMP-leaves the old
group and joins the new one. The device API is on the decoder itself
(Telnet port 23 line commands; an HTTP "justAPI" also exists), so the
control plane needs no vendor software.

Bench-verification pending (doc/research/HARDWARE.md risks): the exact
command vocabulary per firmware, and the channel→group mapping. Both are
isolated here: commands in ``_CMD_*`` and the mapping in
:func:`jap_group`. Tests script the link transcript, so every assumption
is executable and adjustable after a bench session.
"""

from __future__ import annotations

import contextlib
import socket
from collections.abc import Callable
from ipaddress import IPv4Address
from typing import Protocol

from yoke.core.types import Body, DriverStatus, NodeState
from yoke.drivers.base import DriverError

_CMD_SET_CHANNEL = "channel {channel}"
_CMD_GET_CHANNEL = "channel"

JAP_VIDEO_BASE = "239.92.0.0"


def jap_group(channel: int, base: str = JAP_VIDEO_BASE) -> str:
    """Multicast group for a JAP channel: base address + channel.

    The last two octets encode the channel (channel 256 = x.y.1.0).
    Bench-verify against real units before relying on it for snooping.
    """
    if not 1 <= channel <= 65535:
        raise DriverError(f"jap channel out of range: {channel}")
    return str(IPv4Address(int(IPv4Address(base)) + channel))


class JapLink(Protocol):
    """One command/response exchange with a JAP device's line API."""

    def send_command(self, command: str) -> str:
        """Send one line, return the device's reply text (stripped)."""

    def close(self) -> None: ...


class TelnetLink:
    """Minimal line-oriented client for the JAP Telnet CLI.

    JAP's CLI is a plain line protocol; we deliberately implement no
    Telnet option negotiation (bytes 0xFF sequences are stripped).
    """

    def __init__(self, host: str, port: int = 23, timeout: float = 5.0) -> None:
        try:
            self._sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as exc:
            raise DriverError(f"jap connect {host}:{port}: {exc}") from exc
        self._sock.settimeout(timeout)

    def send_command(self, command: str) -> str:
        try:
            self._sock.sendall(command.encode("ascii") + b"\r\n")
            data = self._sock.recv(4096)
        except OSError as exc:
            raise DriverError(f"jap command {command!r}: {exc}") from exc
        cleaned = bytes(b for b in data if b != 0xFF)
        return cleaned.decode("ascii", errors="replace").strip()

    def close(self) -> None:
        self._sock.close()


LinkFactory = Callable[[str], JapLink]


def telnet_link_factory(host: str) -> JapLink:
    return TelnetLink(host)


class JapDecoderDriver:
    """Consumer-side driver: point a JAP decoder at an encoder's channel.

    ``media`` advertised by a JAP producer must be
    ``{"host": "<decoder-reachable encoder id — informational>",
       "channel": <int>}``; only ``channel`` is required. The decoder's
    own address is fixed configuration of this driver, because the
    decoder is the device being driven.
    """

    def __init__(self, decoder_host: str, link_factory: LinkFactory) -> None:
        self._decoder_host = decoder_host
        self._link_factory = link_factory
        self._want_channel: int | None = None
        self._last_error: str | None = None

    def _channel_of(self, media: Body) -> int:
        channel = media.get("channel")
        if not isinstance(channel, int) or isinstance(channel, bool):
            raise DriverError(f"jap media needs integer channel, got {media!r}")
        if not 1 <= channel <= 65535:
            raise DriverError(f"jap channel out of range: {channel}")
        return channel

    def _command(self, command: str) -> str:
        link = self._link_factory(self._decoder_host)
        try:
            return link.send_command(command)
        finally:
            link.close()

    def attach(self, producer: str, media: Body) -> None:
        channel = self._channel_of(media)
        reply = self._command(_CMD_SET_CHANNEL.format(channel=channel))
        if "error" in reply.lower():
            self._last_error = reply
            raise DriverError(f"jap decoder refused channel {channel}: {reply}")
        self._want_channel = channel
        self._last_error = None

    def detach(self) -> None:
        # JAP decoders always show some channel; "detached" for yoke means
        # channel 0 (device-dependent no-op if unsupported). Idempotent.
        if self._want_channel is None:
            return
        # Detach must be idempotent and non-fatal.
        with contextlib.suppress(DriverError):
            self._command(_CMD_SET_CHANNEL.format(channel=0))
        self._want_channel = None

    def poll(self) -> DriverStatus:
        if self._want_channel is None:
            return DriverStatus(NodeState.IDLE)
        try:
            reply = self._command(_CMD_GET_CHANNEL)
        except DriverError as exc:
            return DriverStatus(NodeState.ERROR, str(exc))
        digits = "".join(ch for ch in reply if ch.isdigit())
        if not digits:
            return DriverStatus(
                NodeState.ERROR, f"unparseable channel reply: {reply!r}"
            )
        if int(digits) == self._want_channel:
            return DriverStatus(NodeState.ACTIVE)
        return DriverStatus(
            NodeState.PENDING, f"decoder on {digits}, want {self._want_channel}"
        )

    def describe(self) -> Body | None:
        return None


class JapEncoderConfig:
    """Producer side: JAP encoders need no runtime driving — they always
    transmit on their channel. This helper only produces the ``media``
    dict a yoke producer agent (warden) advertises for one."""

    def __init__(self, channel: int) -> None:
        if not 1 <= channel <= 65535:
            raise DriverError(f"jap channel out of range: {channel}")
        self.channel = channel

    def media(self) -> Body:
        return {"channel": self.channel, "group": jap_group(self.channel)}
