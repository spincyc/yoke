"""Bounded JSON client for an explicitly configured telemetry endpoint."""

from __future__ import annotations

import io
import json
import math
import socket
import ssl
import time
from collections.abc import Buffer
from http.client import HTTPConnection, HTTPException
from typing import cast
from urllib.parse import urlsplit, urlunsplit

from yoke.telemetry.model import MAX_SNAPSHOT_BYTES, Snapshot


def _remaining(expires: float) -> float:
    remaining = expires - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("telemetry HTTP response deadline exceeded")
    return remaining


def _connect(host: str, port: int, expires: float) -> socket.socket:
    # DNS itself follows the OS resolver policy. Every resulting TCP attempt
    # shares the remaining deadline rather than restarting a per-address timer.
    _remaining(expires)
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    last_error: OSError | None = None
    for family, kind, protocol, _, address in addresses:
        timeout = _remaining(expires)
        connection = socket.socket(family, kind, protocol)
        try:
            connection.settimeout(timeout)
            connection.connect(address)
            return connection
        except OSError as error:
            connection.close()
            last_error = error
    if last_error is not None:
        raise last_error
    raise OSError("telemetry host resolved to no TCP addresses")


class _DeadlineReader(io.RawIOBase):
    def __init__(self, connection: socket.socket, expires: float) -> None:
        self._connection = connection
        self._expires = expires
        # The real file retains the socket until the response closes, even if
        # HTTPConnection releases its own socket on a Connection: close header.
        self._raw = connection.makefile("rb", buffering=0)

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Buffer, /) -> int:
        self._connection.settimeout(_remaining(self._expires))
        count = self._raw.readinto(buffer)
        if count is None:
            raise OSError("telemetry response socket is unexpectedly nonblocking")
        return count

    def close(self) -> None:
        self._raw.close()
        super().close()


class _DeadlineSocket:
    """The socket operations HTTPConnection/HTTPResponse use, with one deadline."""

    def __init__(self, connection: socket.socket, expires: float) -> None:
        self._connection = connection
        self._expires = expires

    def sendall(self, data: Buffer) -> None:
        self._connection.settimeout(_remaining(self._expires))
        self._connection.sendall(data)

    def makefile(self, mode: str) -> io.BufferedReader:
        if mode != "rb":
            raise ValueError("telemetry response requires a binary reader")
        return io.BufferedReader(_DeadlineReader(self._connection, self._expires))

    def close(self) -> None:
        self._connection.close()


class _Connection(HTTPConnection):
    def __init__(
        self,
        host: str,
        port: int,
        expires: float,
        tls_context: ssl.SSLContext | None,
    ) -> None:
        super().__init__(host, port, timeout=_remaining(expires))
        self._expires = expires
        self._tls_context = tls_context

    def connect(self) -> None:
        connection = _connect(self.host, self.port, self._expires)
        try:
            if self._tls_context is not None:
                connection.settimeout(_remaining(self._expires))
                connection = self._tls_context.wrap_socket(
                    connection, server_hostname=self.host
                )
            # http.client deliberately accepts a socket-like object here. The
            # adapter implements its sendall/makefile/close transport contract
            # and preserves verified TLS rather than reading its raw fd.
            self.sock = cast(socket.socket, _DeadlineSocket(connection, self._expires))
        except BaseException:
            connection.close()
            raise


def _endpoint(url: str) -> str:
    if not isinstance(url, str) or any(
        ord(char) <= 32 or ord(char) == 127 for char in url
    ):
        raise ValueError(
            "telemetry URL must not contain whitespace or control characters"
        )
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("telemetry URL requires an explicit http or https host")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("telemetry URL must not contain credentials")
        if "?" in url or "#" in url:
            raise ValueError("telemetry URL must not contain a query or fragment")
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError("telemetry URL port must be between 1 and 65535")
        if parsed.path not in ("", "/", "/v1/snapshot"):
            raise ValueError("telemetry URL must name a base URL or /v1/snapshot")
    except ValueError as error:
        raise ValueError(f"invalid telemetry URL: {error}") from error
    return urlunsplit((parsed.scheme, parsed.netloc, "/v1/snapshot", "", ""))


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON object key")
        value[key] = item
    return value


def _nonfinite(value: str) -> None:
    raise ValueError(f"nonfinite JSON value: {value}")


class SnapshotClient:
    """Fetch snapshots without discovery, proxies, redirects, or stale fallback.

    HTTPS uses Python's default certificate and hostname verification. The
    endpoint is diagnostic metadata; the client never uses control PSKs or
    accepts HTTP credentials. ``timeout`` bounds TCP connection, TLS handshake,
    headers and body under one absolute deadline. DNS follows the operating
    system resolver's timeout policy.
    """

    def __init__(self, url: str, *, timeout: float = 2.0) -> None:
        self.url = _endpoint(url)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("telemetry timeout must be finite and positive")
        self.timeout = float(timeout)
        parsed = urlsplit(self.url)
        self._host = cast(str, parsed.hostname)
        secure = parsed.scheme == "https"
        self._port = parsed.port or (443 if secure else 80)
        self._tls_context = ssl.create_default_context() if secure else None

    def get(self) -> Snapshot:
        """Return a validated fresh response or raise ``ValueError``/``OSError``."""
        connection = _Connection(
            self._host,
            self._port,
            time.monotonic() + self.timeout,
            self._tls_context,
        )
        try:
            connection.request(
                "GET",
                "/v1/snapshot",
                headers={"Accept": "application/json", "Connection": "close"},
            )
            with connection.getresponse() as response:
                if response.status != 200:
                    raise OSError(f"telemetry HTTP response status {response.status}")
                if response.headers.get_content_type() != "application/json":
                    raise ValueError("telemetry response must be application/json")
                lengths = response.headers.get_all("Content-Length", [])
                declared: int | None = None
                if lengths:
                    length = lengths[0].strip()
                    if (
                        len(lengths) != 1
                        or not length.isascii()
                        or not length.isdecimal()
                    ):
                        raise ValueError("invalid telemetry Content-Length")
                    declared = int(length)
                    if declared > MAX_SNAPSHOT_BYTES:
                        raise ValueError("telemetry response exceeds size limit")
                if lengths and response.headers.get("Transfer-Encoding") is not None:
                    raise ValueError("ambiguous telemetry response framing")
                encoded = response.read(MAX_SNAPSHOT_BYTES + 1)
                if len(encoded) > MAX_SNAPSHOT_BYTES:
                    raise ValueError("telemetry response exceeds size limit")
                if declared is not None and len(encoded) != declared:
                    raise ValueError("telemetry response Content-Length mismatch")
        except HTTPException as error:
            raise OSError(f"invalid telemetry HTTP response: {error}") from error
        finally:
            connection.close()
        try:
            value = json.loads(
                encoded.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_nonfinite,
            )
        except (UnicodeError, ValueError, RecursionError) as error:
            raise ValueError(f"invalid telemetry snapshot JSON: {error}") from error
        return Snapshot.from_body(value)
