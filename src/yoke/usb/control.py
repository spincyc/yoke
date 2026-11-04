"""Metadata-only control channel between the agent and the gadget worker.

The ``usb-gadget`` driver (in the agent) and the worker (a separate,
separately privileged process) exchange one JSON object per line over a
Unix stream socket (ADR 0022). Requests carry a route: a monotonically
increasing generation and either a USB/IP target or ``null``. Replies carry
the worker's status. Neither direction can carry USB payload: every field
is validated against a closed schema with bounded sizes.

The worker acknowledges a route generation only after it has confirmed
teardown of every older session, so an acknowledged generation is a
replacement fence. The agent remains the durable authority; the worker keeps
routes in memory only, and a restarted worker reports generation 0.
"""

from __future__ import annotations

import ipaddress
import json
import math
import re
import socket
from dataclasses import dataclass, field

SCHEMA = 1
MAX_MESSAGE = 16384
DEFAULT_SOCKET = "/run/yoke-gadget/control.sock"
STATES = frozenset(
    {
        "idle",
        "connecting",
        "presenting",
        "attached",
        "default",
        "addressed",
        "configured",
        "suspended",
        "disconnected",
        "rejected",
        "error",
    }
)

_BUSID = re.compile(r"[0-9]+-[0-9]+(?:\.[0-9]+)*\Z")
_HOST = re.compile(r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?\.?\Z")
_NAME = re.compile(r"[A-Za-z0-9_.:-]{1,64}\Z")


class ControlError(ValueError):
    """A malformed control message or an unreachable worker."""


def validate_host(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 253:
        raise ControlError(f"bad host: {value!r}")
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        labels = value.rstrip(".").split(".")
        if not _HOST.fullmatch(value) or any(
            not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
            for label in labels
        ):
            raise ControlError(f"bad host: {value!r}") from None
        return value.lower().rstrip(".")


def validate_port(value: object) -> int:
    if type(value) is not int or not 0 < value < 65536:
        raise ControlError(f"bad port: {value!r}")
    return value


def validate_busid(value: object) -> str:
    if not isinstance(value, str) or len(value) > 31 or not _BUSID.fullmatch(value):
        raise ControlError(f"bad busid: {value!r}")
    return value


def validate_source(value: object) -> str | None:
    """Optional local IPv4 the worker binds before connecting (admission)."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ControlError(f"bad source IPv4 address: {value!r}")
    try:
        return str(ipaddress.IPv4Address(value))
    except ValueError as exc:
        raise ControlError(f"bad source IPv4 address: {value!r}") from exc


@dataclass(frozen=True)
class Target:
    """Where the worker connects: producer ``host:port``/``busid``.

    ``source`` is the computer-side box's control-VLAN IPv4. The producer
    admits USB/IP connections only from the IPv4 address its consumer
    announces from (⚠ nftables admission, lane/ctl), so the worker binds it.
    """

    host: str
    port: int
    busid: str
    source: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "host", validate_host(self.host))
        validate_port(self.port)
        validate_busid(self.busid)
        object.__setattr__(self, "source", validate_source(self.source))

    def to_json(self) -> dict[str, object]:
        return {
            "host": self.host,
            "port": self.port,
            "busid": self.busid,
            "source": self.source,
        }

    @classmethod
    def from_json(cls, value: object) -> Target:
        if not isinstance(value, dict) or set(value) != {
            "host",
            "port",
            "busid",
            "source",
        }:
            raise ControlError("target needs exactly host, port, busid and source")
        return cls(value["host"], value["port"], value["busid"], value["source"])

    def __str__(self) -> str:
        origin = f" from {self.source}" if self.source else ""
        return f"{self.host}:{self.port}/{self.busid}{origin}"


@dataclass(frozen=True)
class Route:
    generation: int
    target: Target | None

    def __post_init__(self) -> None:
        if type(self.generation) is not int or not 0 < self.generation < 2**63:
            raise ControlError("route generation must be a positive integer")


@dataclass(frozen=True)
class WorkerStatus:
    generation: int
    state: str
    detail: str = ""
    target: Target | None = None
    udc: str = ""
    link_speed: str = ""
    device: dict[str, str] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)
    reconnects: int = 0
    updated: float = 0.0

    def to_json(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "generation": self.generation,
            "state": self.state,
            "detail": self.detail,
            "target": None if self.target is None else self.target.to_json(),
            "udc": self.udc,
            "link_speed": self.link_speed,
            "device": dict(self.device),
            "counters": dict(self.counters),
            "reconnects": self.reconnects,
            "updated": self.updated,
        }

    @classmethod
    def from_json(cls, value: object) -> WorkerStatus:
        if not isinstance(value, dict) or value.get("schema") != SCHEMA:
            raise ControlError("unknown worker status schema")
        expected = {
            "schema",
            "generation",
            "state",
            "detail",
            "target",
            "udc",
            "link_speed",
            "device",
            "counters",
            "reconnects",
            "updated",
        }
        if set(value) != expected:
            raise ControlError("worker status has unexpected fields")
        generation, state = value["generation"], value["state"]
        if type(generation) is not int or generation < 0:
            raise ControlError("bad status generation")
        if state not in STATES:
            raise ControlError(f"bad worker state: {state!r}")
        detail, udc, link = value["detail"], value["udc"], value["link_speed"]
        if not all(isinstance(t, str) and len(t) <= 512 for t in (detail, udc, link)):
            raise ControlError("status text fields must be short strings")
        device, counters = value["device"], value["counters"]
        if not isinstance(device, dict) or not all(
            isinstance(k, str)
            and _NAME.fullmatch(k)
            and isinstance(v, str)
            and len(v) <= 64
            for k, v in device.items()
        ):
            raise ControlError("bad device summary")
        if not isinstance(counters, dict) or not all(
            isinstance(k, str) and _NAME.fullmatch(k) and type(v) is int and v >= 0
            for k, v in counters.items()
        ):
            raise ControlError("bad counters")
        reconnects, updated = value["reconnects"], value["updated"]
        if type(reconnects) is not int or reconnects < 0:
            raise ControlError("bad reconnect count")
        if not isinstance(updated, (int, float)) or not math.isfinite(updated):
            raise ControlError("bad status timestamp")
        target = value["target"]
        return cls(
            generation=generation,
            state=state,
            detail=detail,
            target=None if target is None else Target.from_json(target),
            udc=udc,
            link_speed=link,
            device=dict(device),
            counters=dict(counters),
            reconnects=reconnects,
            updated=float(updated),
        )


def encode(message: dict[str, object]) -> bytes:
    raw = json.dumps(message, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    if len(raw) > MAX_MESSAGE:
        raise ControlError("control message too large")
    return raw


def decode(raw: bytes) -> dict[str, object]:
    if len(raw) > MAX_MESSAGE or not raw.endswith(b"\n"):
        raise ControlError("control message is not one bounded line")
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise ControlError(f"control message is not JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ControlError("control message must be an object")
    return value


def route_request(route: Route) -> dict[str, object]:
    return {
        "schema": SCHEMA,
        "op": "route",
        "generation": route.generation,
        "target": None if route.target is None else route.target.to_json(),
    }


def parse_request(message: dict[str, object]) -> Route | None:
    """Return the route of a ``route`` request, or None for ``status``."""
    if message.get("schema") != SCHEMA:
        raise ControlError("unknown request schema")
    op = message.get("op")
    if op == "status" and set(message) == {"schema", "op"}:
        return None
    if op == "route" and set(message) == {"schema", "op", "generation", "target"}:
        generation = message["generation"]
        if type(generation) is not int:
            raise ControlError("route generation must be an integer")
        target = message["target"]
        return Route(generation, None if target is None else Target.from_json(target))
    raise ControlError("unknown control request")


def read_line(sock: socket.socket) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            raise ControlError("control peer closed before a full line")
        chunks.append(chunk)
        size += len(chunk)
        if b"\n" in chunk:
            break
        if size > MAX_MESSAGE:
            raise ControlError("control message too large")
    raw = b"".join(chunks)
    return raw[: raw.index(b"\n") + 1]


class ControlClient:
    """One short-lived connection per request (used by the agent's driver)."""

    def __init__(self, path: str, *, timeout: float) -> None:
        self.path = path
        self.timeout = timeout

    def request(self, message: dict[str, object]) -> WorkerStatus:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.timeout)
                sock.connect(self.path)
                sock.sendall(encode(message))
                reply = decode(read_line(sock))
        except OSError as exc:
            raise ControlError(
                f"gadget worker unreachable at {self.path!r}: {exc}"
            ) from exc
        if reply.get("ok") is not True:
            error = reply.get("error")
            raise ControlError(f"gadget worker refused: {error!s:.200}")
        return WorkerStatus.from_json(reply.get("status"))

    def status(self) -> WorkerStatus:
        return self.request({"schema": SCHEMA, "op": "status"})

    def route(self, route: Route) -> WorkerStatus:
        return self.request(route_request(route))
