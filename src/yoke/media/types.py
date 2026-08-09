"""Vocabulary of the sans-I/O media pipeline.

Everything here is pure data. A media pipeline is an ordered list of
:class:`~yoke.media.pipeline.Stage` transforms; a unit of media flows in
one end and packets/frames come out the other, while every stage emits
:class:`StageEvent` taps that a passive observer reconstructs health from
without touching the stream. This mirrors the control plane
(``yoke.core``): pure transforms, explicit clock, deterministic tests.

See doc/design/MEDIA.md for the architecture.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

Body = dict[str, Any]

# Media clock: 90 kHz is the RTP video convention (RFC 3551). Timestamps
# (pts) are in these ticks so packetizers/jitter buffers share one unit.
MEDIA_CLOCK_HZ = 90_000
# Default network MTU for packetization; jumbo frames raise this and cut
# packet rate ~6x (see doc/research/prototype/COMPRESSION.md).
DEFAULT_MTU = 1500
JUMBO_MTU = 9000
# RTP payload types we assign (dynamic range 96-127).
PT_RAW = 96  # RFC 4175 uncompressed
PT_JPEGXS = 97
PT_VC2 = 98
PT_GENERIC = 99
# Visually-lossless band the rate controller slides within (ADR: never
# cross into frame-based coding). Ratio = uncompressed/compressed bytes.
RATIO_MIN = 1.0
RATIO_TRANSPARENT = 2.5
RATIO_MAX = 6.0


class UnitKind(StrEnum):
    """What a MediaUnit carries as it moves down the pipeline."""

    FRAME = "frame"  # a whole video frame (source, sink)
    SLICE = "slice"  # a line-based codec slice (a few lines)
    PACKET = "packet"  # an RTP packet on the wire
    PARITY = "parity"  # an FEC repair packet


class EventKind(StrEnum):
    """Observability taps every stage emits (non-interfering)."""

    IN = "in"  # a unit entered the stage
    OUT = "out"  # a unit left the stage
    DROP = "drop"  # a unit was dropped here
    RECOVER = "recover"  # FEC/retransmit reconstructed a unit
    HOLD = "hold"  # jitter buffer is holding a unit for playout
    LATE = "late"  # a unit arrived after its playout deadline
    ADJUST = "adjust"  # rate controller changed the target
    INTEGRITY = "integrity"  # sink verified (or failed) a frame's digest
    STAT = "stat"  # periodic counters snapshot


def digest_of(payload: bytes) -> str:
    """Stable content digest used for end-to-end integrity verification.

    A source stamps the original frame's digest into every derived unit's
    metadata; a bit-exact path (passthrough / lossless codec) lets the
    sink and any snooper confirm the reconstructed frame matches. Lossy
    paths verify structure and the embedded frame id instead.
    """
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class MediaUnit:
    """One item flowing through the pipeline.

    ``seq`` is monotonic per source. ``pts`` is the 90 kHz presentation
    timestamp of the frame this unit belongs to (shared by all slices/
    packets of a frame). ``source_digest`` rides along from the origin so
    any downstream stage or observer can verify end-to-end integrity.
    """

    kind: UnitKind
    seq: int
    pts: int
    payload: bytes
    source_digest: str
    meta: Body = field(default_factory=dict)

    def with_payload(self, payload: bytes, **meta: Any) -> MediaUnit:
        """Derive a unit with a new payload, preserving identity/digest."""
        merged = {**self.meta, **meta}
        return MediaUnit(
            kind=self.kind,
            seq=self.seq,
            pts=self.pts,
            payload=payload,
            source_digest=self.source_digest,
            meta=merged,
        )


@dataclass(frozen=True)
class StageEvent:
    """A non-interfering observation emitted by a stage. Consumed by the
    snooper/metrics; NEVER alters the stream (the pipeline's forward
    output is identical whether or not anyone reads these)."""

    stage: str
    kind: EventKind
    seq: int
    pts: int
    now: float
    detail: Body = field(default_factory=dict)


@dataclass(frozen=True)
class NetworkReport:
    """Receiver → sender feedback that drives the rate controller.

    All fields are observed at the receiver: fraction of packets lost,
    smoothed one-way delay variation (jitter, seconds), and an ECN-style
    congestion signal in [0, 1] (0 = idle, 1 = congested)."""

    now: float
    loss: float
    jitter: float
    congestion: float
    frames_ok: int = 0
    frames_bad: int = 0


@dataclass
class StageOutcome:
    """What a stage returns from ``process``/``tick``/``on_feedback``.

    ``emit`` are units forwarded downstream (0, 1, or many — packetizers
    and FEC fan out). ``events`` are observability taps. ``feedback`` are
    upstream reports (the receiver emits these; the transport carries them
    back to the sender's rate controller)."""

    emit: list[MediaUnit] = field(default_factory=list)
    events: list[StageEvent] = field(default_factory=list)
    feedback: list[NetworkReport] = field(default_factory=list)

    def extend(self, other: StageOutcome) -> None:
        self.emit.extend(other.emit)
        self.events.extend(other.events)
        self.feedback.extend(other.feedback)


@dataclass
class PipelineOutput:
    """Result of pushing a unit (or ticking) through a whole pipeline:
    units that exited the last stage, every tap event, and any feedback."""

    emitted: list[MediaUnit] = field(default_factory=list)
    events: list[StageEvent] = field(default_factory=list)
    feedback: list[NetworkReport] = field(default_factory=list)
