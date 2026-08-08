"""Shared vocabulary of the yoke control plane.

Everything here is pure data. The wire representations live in
``frames.py``; the behavior lives in ``node.py``. See
doc/design/PROTOCOL.md for the normative definitions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

PROTOCOL_VERSION = 1
CONTROL_GROUP = "239.121.111.107"
CONTROL_PORT = 9753
MAX_FRAME_BYTES = 1200

ANN_INTERVAL = 2.0
ANN_JITTER = 0.25  # +/- fraction of ANN_INTERVAL
PEER_TIMEOUT = 6.5
RETRY_BACKOFF_BASE = 1.0
RETRY_BACKOFF_CAP = 30.0
CLI_RETRIES = 5
CLI_DEADLINE = 10.0
INTENT_CACHE_TTL = 24 * 3600.0
SUPPRESSION_MIN = 0.2
SUPPRESSION_MAX = 1.2
# A well-behaved origin resumes seq at max(persisted+1, int(unix_time))
# (ADR 0007), so a legitimate seq never runs far ahead of the receiver's
# clock. Frames claiming a seq beyond this margin are rejected so a
# spoofed high-water cannot permanently mute a real origin (ADR 0010).
NS_PER_S = 1_000_000_000
# Sequence numbers are Unix nanoseconds (ADR 0007): fine enough that a
# burst of frames never collides within one origin, and consistent
# between agents (which derive the floor from the seconds clock via
# integer math) and the CLI (time.time_ns). A frame claiming a seq more
# than SEQ_FUTURE_MARGIN_S seconds ahead of the receiver's clock is
# rejected so a spoofed high-water cannot permanently mute a real origin
# (ADR 0010).
SEQ_FUTURE_MARGIN_S = 3600
# Bounds on per-origin bookkeeping so a device cycling through distinct
# node ids cannot exhaust memory (ADR 0010). A peer lost or departed for
# longer than PEER_FORGET is dropped; the replay/rejection/intent maps
# are FIFO-capped at MAX_TRACKED entries (the currently paired producer
# is never evicted).
PEER_FORGET = 300.0
MAX_TRACKED = 4096

NODE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")

Version = tuple[int, str]
VERSION_ZERO: Version = (0, "")

Body = dict[str, Any]


class Role(StrEnum):
    PRODUCER = "producer"
    CONSUMER = "consumer"


class Kind(StrEnum):
    HDMI = "hdmi"
    USB = "usb"


class NodeState(StrEnum):
    IDLE = "idle"
    PENDING = "pending"
    ACTIVE = "active"
    ERROR = "error"


class FrameType(StrEnum):
    ANN = "ann"
    INT = "int"
    ACK = "ack"
    EVT = "evt"
    BYE = "bye"


class DropReason(StrEnum):
    """Counter slugs for dropped inbound frames (PROTOCOL.md)."""

    MALFORMED = "malformed"
    VERSION = "version"
    AUTH = "auth"
    STALE = "stale"
    DUP = "dup"
    OVERSIZE = "oversize"


@dataclass(frozen=True)
class Frame:
    """A decoded, authenticated control frame."""

    type: FrameType
    origin: str
    seq: int
    ts: float
    body: Body


@dataclass(frozen=True)
class Pairing:
    """A consumer's applied pairing. ``producer`` None means unpaired."""

    producer: str | None
    version: Version


@dataclass(frozen=True)
class DriverStatus:
    """What the media plane reports back to the core."""

    state: NodeState
    detail: str = ""


@dataclass(frozen=True)
class NodeConfig:
    node_id: str
    role: Role
    kind: Kind
    name: str = ""

    def __post_init__(self) -> None:
        if not NODE_ID_RE.match(self.node_id):
            raise ValueError(f"invalid node id: {self.node_id!r}")


@dataclass
class PersistedState:
    """The only durable state an agent owns (PROTOCOL.md: persist before ack)."""

    seq: int = 0
    pairing: Pairing = field(default_factory=lambda: Pairing(None, VERSION_ZERO))

    def to_json(self) -> Body:
        return {
            "seq": self.seq,
            "pairing": {
                "producer": self.pairing.producer,
                "version": list(self.pairing.version),
            },
        }

    @classmethod
    def from_json(cls, data: Body) -> PersistedState:
        pairing = data["pairing"]
        version = pairing["version"]
        return cls(
            seq=int(data["seq"]),
            pairing=Pairing(
                producer=pairing["producer"],
                version=(int(version[0]), str(version[1])),
            ),
        )


def version_key(version: Version) -> tuple[int, str]:
    """Total order for pairing versions: counter, then origin id."""
    return (version[0], version[1])


# --- Effects: what the sans-I/O core asks its shell to do -------------------


@dataclass(frozen=True)
class SendFrame:
    """Sign and multicast a frame authored by this node.

    The core assigns ``seq``; the shell encodes with the site PSK.
    """

    type: FrameType
    seq: int
    body: Body


@dataclass(frozen=True)
class SendRaw:
    """Re-multicast a previously received datagram verbatim.

    No longer emitted by the core: verbatim anti-entropy re-sends
    livelock against replay defense (ADR 0009) — cached intents are
    re-issued as fresh ``SendFrame`` ints instead. Retained because the
    shell supports it and replay tooling may use it deliberately."""

    raw: bytes


@dataclass(frozen=True)
class AttachMedia:
    """Tell the driver to consume the given producer's media parameters."""

    producer: str
    media: Body


@dataclass(frozen=True)
class DetachMedia:
    pass


@dataclass(frozen=True)
class Persist:
    """Durably store this state before emitting any later effect."""

    state: PersistedState


@dataclass(frozen=True)
class Log:
    message: str


Effect = SendFrame | SendRaw | AttachMedia | DetachMedia | Persist | Log
