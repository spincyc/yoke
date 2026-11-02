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

import re
import socket
import time
from collections.abc import Callable
from ipaddress import IPv4Address
from typing import Protocol

from yoke.core.types import Body, DriverStatus, NodeState
from yoke.drivers.base import DriverError, positive_timeout

_CMD_SET_CHANNEL = "channel {channel}"
_CMD_GET_CHANNEL = "channel"

JAP_VIDEO_BASE = "239.92.0.0"
_MAX_REPLY = 4096
_CHANNEL_REPLY = re.compile(
    r"(?:(?:current\s+)?channel\s*(?::|=|set\s+to)?\s*)?([0-9]+)",
    re.IGNORECASE,
)
_CHANNEL_PREFIX = re.compile(r"^(?:current\s+)?channel\b", re.IGNORECASE)
_ERROR_REPLY = re.compile(
    r"\b(?:error|invalid|failed|refused|unknown)\b", re.IGNORECASE
)


def _channel_reply(reply: str) -> int:
    """Isolated, bench-unverified channel response grammar."""
    channels = set()
    for line in reply.splitlines():
        line = line.strip()
        match = _CHANNEL_REPLY.fullmatch(line)
        if match is not None:
            digits = match[1]
            if len(digits) > 5 or int(digits) > 65535:
                raise DriverError(f"invalid channel reply: {reply!r}")
            channels.add(int(digits))
        elif _CHANNEL_PREFIX.match(line):
            raise DriverError(f"invalid channel reply: {reply!r}")
    if _ERROR_REPLY.search(reply) or len(channels) != 1:
        raise DriverError(f"unparseable channel reply: {reply!r}")
    return channels.pop()


def _check_set_reply(reply: str, channel: int) -> None:
    """Require an explicit affirmative reply, not merely absence of ERROR."""
    if not _ERROR_REPLY.search(reply):
        if reply.strip().lower() == "ok":
            return
        try:
            if _channel_reply(reply) == channel:
                return
        except DriverError:
            pass
    raise DriverError(f"jap decoder refused channel {channel}: {reply}")


def jap_group(channel: int, base: str = JAP_VIDEO_BASE) -> str:
    """Multicast group for a JAP channel: base address + channel.

    The last two octets encode the channel (channel 256 = x.y.1.0).
    Bench-verify against real units before relying on it for snooping.
    """
    if (
        isinstance(channel, bool)
        or not isinstance(channel, int)
        or not 1 <= channel <= 65535
    ):
        raise DriverError(f"jap channel out of range: {channel}")
    try:
        group = IPv4Address(int(IPv4Address(base)) + channel)
    except ValueError as exc:
        raise DriverError(f"jap invalid multicast base: {base!r}") from exc
    if not group.is_multicast:
        raise DriverError(f"jap group is not multicast: {group}")
    return str(group)


class JapLink(Protocol):
    """One command/response exchange with a JAP device's line API."""

    def send_command(self, command: str) -> str:
        """Send one line, return the device's reply text (stripped)."""

    def close(self) -> None: ...


class TelnetLink:
    """Bounded line-oriented client for the JAP Telnet CLI.

    Refuse unsupported Telnet options (RFC 854), consume subnegotiations,
    and preserve parser state across arbitrary TCP fragmentation. Return
    every complete response line already received, completing any known
    trailing fragment so contradictory channel lines cannot be discarded.
    A later, separate line has no known end-of-response marker: firmware
    framing and the single-response-line assumption remain bench-unverified.
    """

    def __init__(self, host: str, port: int = 23, timeout: float = 5.0) -> None:
        self._timeout = positive_timeout(timeout, "jap timeout")
        if (
            isinstance(port, bool)
            or not isinstance(port, int)
            or not 1 <= port <= 65535
        ):
            raise DriverError("jap port must be 1 through 65535")
        try:
            self._sock = socket.create_connection((host, port), timeout=self._timeout)
        except OSError as exc:
            raise DriverError(f"jap connect {host}:{port}: {exc}") from exc
        self._state = "data"
        self._option_command = 0
        self._buffer = bytearray()

    def _refuse_option(self, response: int, option: int, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DriverError("jap reply deadline exceeded")
        self._sock.settimeout(remaining)
        self._sock.sendall(bytes((255, response, option)))

    def _text(self, data: bytes, deadline: float) -> bytes:
        text = bytearray()
        for byte in data:
            if self._state == "data":
                if byte == 255:  # IAC
                    self._state = "iac"
                else:
                    text.append(byte)
            elif self._state == "iac":
                if byte == 255:  # escaped data byte
                    text.append(byte)
                    self._state = "data"
                elif byte in (251, 252, 253, 254):  # WILL/WONT/DO/DONT
                    self._option_command = byte
                    self._state = "option"
                elif byte == 250:  # SB: consume through IAC SE
                    self._state = "subnegotiation"
                else:
                    self._state = "data"  # standalone Telnet command
            elif self._state == "option":
                if self._option_command == 251:  # WILL -> DONT
                    self._refuse_option(254, byte, deadline)
                elif self._option_command == 253:  # DO -> WONT
                    self._refuse_option(252, byte, deadline)
                self._state = "data"
            elif self._state == "subnegotiation":
                if byte == 255:
                    self._state = "subnegotiation-iac"
            elif self._state == "subnegotiation-iac":
                self._state = "data" if byte == 240 else "subnegotiation"
        return bytes(text)

    def send_command(self, command: str) -> str:
        if not command or any(c in command for c in "\r\n\x00"):
            raise DriverError("jap command must be one nonempty ASCII line")
        try:
            encoded = command.encode("ascii")
        except UnicodeEncodeError as exc:
            raise DriverError("jap command must be ASCII") from exc
        deadline = time.monotonic() + self._timeout
        received = 0
        lines: list[str] = []
        try:
            self._sock.settimeout(self._timeout)
            self._sock.sendall(encoded + b"\r\n")
            while True:
                while b"\n" in self._buffer:
                    raw, _, rest = self._buffer.partition(b"\n")
                    self._buffer = bytearray(rest)
                    line = raw.decode("ascii", errors="strict").strip()
                    if line and line != command:
                        lines.append(line)
                if lines and not self._buffer and self._state == "data":
                    return "\n".join(lines)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DriverError("jap reply deadline exceeded")
                self._sock.settimeout(remaining)
                data = self._sock.recv(min(1024, _MAX_REPLY + 1 - received))
                if not data:
                    raise DriverError("jap connection closed before complete reply")
                received += len(data)
                if received > _MAX_REPLY:
                    raise DriverError("jap reply exceeds 4096 bytes")
                self._buffer.extend(self._text(data, deadline))
        except (OSError, UnicodeDecodeError) as exc:
            raise DriverError(f"jap command {command!r}: {exc}") from exc

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
        # The dedicated decoder can outlive this Python process. Only a
        # confirmed channel-zero readback establishes physical detachment.
        self._route_unknown = True

    def _channel_of(self, media: Body) -> int:
        channel = media.get("channel")
        if not isinstance(channel, int) or isinstance(channel, bool):
            raise DriverError(f"jap media needs integer channel, got {media!r}")
        if not 1 <= channel <= 65535:
            raise DriverError(f"jap channel out of range: {channel}")
        return channel

    def _command(self, command: str) -> str:
        try:
            link = self._link_factory(self._decoder_host)
            try:
                return link.send_command(command)
            finally:
                link.close()
        except OSError as exc:
            raise DriverError(f"jap command {command!r}: {exc}") from exc

    def attach(self, producer: str, media: Body) -> None:
        channel = self._channel_of(media)
        self.detach()
        # Once sent, a lost reply does not prove the appliance did nothing.
        self._want_channel = channel
        try:
            reply = self._command(_CMD_SET_CHANNEL.format(channel=channel))
            _check_set_reply(reply, channel)
        except DriverError as exc:
            self._last_error = str(exc)
            raise
        self._last_error = None

    def detach(self) -> None:
        # Bench-unverified: channel zero must actually disable the route.
        # Firmware that refuses or ignores it cannot claim safe detachment.
        if self._want_channel is None and not self._route_unknown:
            self._last_error = None
            return
        try:
            _check_set_reply(self._command(_CMD_SET_CHANNEL.format(channel=0)), 0)
            actual = _channel_reply(self._command(_CMD_GET_CHANNEL))
            if actual != 0:
                raise DriverError(
                    f"jap teardown unconfirmed: decoder on {actual}, want 0"
                )
        except DriverError as exc:
            self._last_error = str(exc)
            raise
        self._want_channel = None
        self._route_unknown = False
        self._last_error = None

    def poll(self) -> DriverStatus:
        if self._last_error:
            return DriverStatus(NodeState.ERROR, self._last_error)
        if self._want_channel is None and self._route_unknown:
            return DriverStatus(
                NodeState.PENDING, "decoder route unknown; teardown required"
            )
        try:
            actual = _channel_reply(self._command(_CMD_GET_CHANNEL))
        except DriverError as exc:
            if self._want_channel is None:
                self._route_unknown = True
                self._last_error = str(exc)
            return DriverStatus(NodeState.ERROR, str(exc))
        if self._want_channel is None:
            if actual == 0:
                return DriverStatus(
                    NodeState.IDLE, "decoder on channel 0; physical disable unverified"
                )
            self._route_unknown = True
            self._last_error = f"decoder on {actual}, want 0; teardown required"
            return DriverStatus(NodeState.ERROR, self._last_error)
        if actual == self._want_channel:
            return DriverStatus(
                NodeState.ACTIVE, f"decoder on channel {actual}; media flow unverified"
            )
        return DriverStatus(
            NodeState.ERROR,
            f"decoder on {actual}, want {self._want_channel}; retry required",
        )

    def describe(self) -> Body | None:
        return None


class JapEncoderConfig:
    """Producer side: JAP encoders need no runtime driving — they always
    transmit on their channel. This helper only produces the ``media``
    dict a yoke producer agent (warden) advertises for one."""

    def __init__(self, channel: int) -> None:
        jap_group(channel)  # same strict validation as the advertised mapping
        self.channel = channel

    def media(self) -> Body:
        return {"channel": self.channel, "group": jap_group(self.channel)}
