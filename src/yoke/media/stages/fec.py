"""Forward error correction: XOR block parity and ST 2022-7 duplicate paths.

A little compression buys headroom (COMPRESSION.md); FEC spends it on
resilience. Two schemes share one shape -- an encoder that fans a stream out
(adding redundancy) and a decoder that reconstructs from whatever survives.
Everything is sans-I/O and deterministic: the ACTUAL loss/reorder is injected
by the network sim (another lane); these decoders only rebuild from what they
are handed.

Scheme 1 -- ``XorParityFec(group=N)`` / ``XorParityDecoder`` (RFC 5109 style):
after every N media units the encoder emits one ``PARITY`` unit = XOR of the N
payloads. If exactly one of the N data packets is lost (parity present), the
decoder reconstructs it exactly (RECOVER); two or more lost is unrecoverable
(reported); the parity itself being lost needs no recovery. The parity carries
each member's identity/meta and byte length in its own ``meta`` so a recovered
packet is byte-and-metadata identical to the original and can be depacketized.

Scheme 2 -- ``DuplicatePathFec`` / ``SeamlessMerger`` (SMPTE ST 2022-7):
the encoder emits every packet twice tagged ``meta["path"] = "A"|"B"``; the
merger dedups by RTP sequence, forwarding whichever copy arrives first and
dropping the later duplicate. Losing an entire path loses nothing; a sequence
seen on only one path is flagged RECOVER (that copy was the only survivor).

``FecEncodeStage`` / ``FecDecodeStage`` wrap a scheme for the pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from yoke.media.pipeline import StageBase
from yoke.media.types import (
    Body,
    EventKind,
    MediaUnit,
    StageEvent,
    StageOutcome,
    UnitKind,
)


def xor_bytes(chunks: list[bytes]) -> bytes:
    """XOR a list of byte strings, zero-padding shorter ones to the longest
    (RFC 5109 parity over unequal-length payloads)."""
    width = max((len(c) for c in chunks), default=0)
    acc = bytearray(width)
    for chunk in chunks:
        for i, byte in enumerate(chunk):
            acc[i] ^= byte
    return bytes(acc)


@dataclass
class FecFeed:
    """A decoder's output for one pushed (or flushed) unit.

    ``emit`` are units to forward downstream (passed-through data plus any
    reconstructed packets). ``recovered`` are units to annotate with a RECOVER
    event (a subset of ``emit`` for XOR; for the merger these were forwarded
    earlier, so they are event-only). ``deduped`` are dropped duplicates
    (DROP events). ``reports`` are STAT payloads (e.g. unrecoverable groups).
    """

    emit: list[MediaUnit] = field(default_factory=list)
    recovered: list[MediaUnit] = field(default_factory=list)
    deduped: list[MediaUnit] = field(default_factory=list)
    reports: list[Body] = field(default_factory=list)


@runtime_checkable
class FecEncoder(Protocol):
    """Fans a stream out, adding redundancy. ``push`` returns the units to
    emit for one input; ``flush`` emits redundancy for a trailing partial
    group."""

    name: str

    def push(self, unit: MediaUnit) -> list[MediaUnit]: ...

    def flush(self) -> list[MediaUnit]: ...


@runtime_checkable
class FecDecoder(Protocol):
    """Reconstructs from surviving units. ``push`` handles one arrival;
    ``flush`` closes outstanding groups and reports what could not be
    recovered."""

    name: str

    def push(self, unit: MediaUnit) -> FecFeed: ...

    def flush(self) -> FecFeed: ...


# --------------------------------------------------------------------------
# XOR block parity (RFC 5109 style)
# --------------------------------------------------------------------------


class XorParityFec:
    """Encoder: after every ``group`` units emit one XOR ``PARITY`` unit."""

    name = "xor"

    def __init__(self, group: int) -> None:
        if group < 1:
            raise ValueError(f"group must be >= 1, got {group}")
        self._n = group
        self._buf: list[MediaUnit] = []
        self._group = 0

    def push(self, unit: MediaUnit) -> list[MediaUnit]:
        index = len(self._buf)
        tagged = unit.with_payload(
            unit.payload,
            fec_group=self._group,
            fec_index=index,
            fec_size=self._n,
        )
        self._buf.append(tagged)
        out = [tagged]
        if len(self._buf) >= self._n:
            out.append(self._make_parity())
            self._buf = []
            self._group += 1
        return out

    def flush(self) -> list[MediaUnit]:
        """Emit parity protecting a trailing partial group (< N units)."""
        if not self._buf:
            return []
        parity = self._make_parity()
        self._buf = []
        self._group += 1
        return [parity]

    def _make_parity(self) -> MediaUnit:
        members: list[Body] = [
            {
                "seq": u.seq,
                "pts": u.pts,
                "source_digest": u.source_digest,
                "meta": dict(u.meta),
                "length": len(u.payload),
            }
            for u in self._buf
        ]
        payload = xor_bytes([u.payload for u in self._buf])
        first = self._buf[0]
        return MediaUnit(
            kind=UnitKind.PARITY,
            seq=first.seq,  # parity seq mirrors the group's first member
            pts=first.pts,
            payload=payload,
            source_digest=first.source_digest,
            meta={
                "fec": "xor",
                "group": self._group,
                "size": len(self._buf),
                "members": members,
            },
        )


@dataclass
class _Group:
    size: int | None = None
    parity: MediaUnit | None = None
    members: list[Body] | None = None
    data: dict[int, MediaUnit] = field(default_factory=dict)
    recovered: bool = False


class XorParityDecoder:
    """Decoder: reconstruct the one missing data packet of a group from the
    parity, or report the group as unrecoverable."""

    name = "xor"

    def __init__(self) -> None:
        self._groups: dict[int, _Group] = {}

    def push(self, unit: MediaUnit) -> FecFeed:
        feed = FecFeed()
        if unit.kind is UnitKind.PARITY:
            try:
                group_id = int(unit.meta["group"])
                group = self._groups.setdefault(group_id, _Group())
                group.parity = unit
                group.members = list(unit.meta["members"])
                group.size = int(unit.meta["size"])
            except (KeyError, TypeError, ValueError):
                feed.reports.append({"fec": "xor", "error": "bad-parity"})
                return feed
            # parity itself is repair-only; it is not forwarded downstream.
        elif unit.kind is UnitKind.PACKET and "fec_group" in unit.meta:
            group_id = int(unit.meta["fec_group"])
            group = self._groups.setdefault(group_id, _Group())
            group.size = int(unit.meta["fec_size"])
            group.data[int(unit.meta["fec_index"])] = unit
            feed.emit.append(unit)  # pass data through immediately
        else:
            feed.emit.append(unit)  # not FEC-tagged: forward untouched
            return feed

        recovered = self._try_recover(group_id)
        if recovered is not None:
            feed.emit.append(recovered)
            feed.recovered.append(recovered)
        return feed

    def flush(self) -> FecFeed:
        feed = FecFeed()
        for group_id, group in self._groups.items():
            if group.size is None:
                continue
            missing = [i for i in range(group.size) if i not in group.data]
            if missing:
                feed.reports.append(
                    {
                        "fec": "xor",
                        "group": group_id,
                        "missing": missing,
                        "recovered": False,
                        "reason": "insufficient"
                        if group.parity is not None
                        else "no-parity",
                    }
                )
        self._groups = {}
        return feed

    def _try_recover(self, group_id: int) -> MediaUnit | None:
        group = self._groups[group_id]
        if (
            group.recovered
            or group.parity is None
            or group.size is None
            or group.members is None
        ):
            return None
        missing = [i for i in range(group.size) if i not in group.data]
        if len(missing) != 1:
            return None
        lost = missing[0]
        present = [group.data[i].payload for i in range(group.size) if i in group.data]
        rebuilt = xor_bytes([*present, group.parity.payload])
        member = group.members[lost]
        payload = rebuilt[: int(member["length"])]
        unit = MediaUnit(
            kind=UnitKind.PACKET,
            seq=int(member["seq"]),
            pts=int(member["pts"]),
            payload=payload,
            source_digest=str(member["source_digest"]),
            meta=dict(member["meta"]),
        )
        group.recovered = True
        group.data[lost] = unit  # group now complete; no further recovery
        return unit


# --------------------------------------------------------------------------
# ST 2022-7 duplicate paths
# --------------------------------------------------------------------------


class DuplicatePathFec:
    """Encoder: emit every unit twice, tagged path A and path B."""

    name = "dup"

    def push(self, unit: MediaUnit) -> list[MediaUnit]:
        return [
            unit.with_payload(unit.payload, path="A"),
            unit.with_payload(unit.payload, path="B"),
        ]

    def flush(self) -> list[MediaUnit]:
        return []


class SeamlessMerger:
    """Decoder: dedup duplicate-path copies by RTP sequence, first wins."""

    name = "dup"

    def __init__(self) -> None:
        self._count: dict[int, int] = {}
        self._first: dict[int, MediaUnit] = {}

    def push(self, unit: MediaUnit) -> FecFeed:
        feed = FecFeed()
        key = unit.seq  # RTP sequence number (PACKET.seq is the rtp_seq)
        if key in self._count:
            self._count[key] += 1
            feed.deduped.append(unit)  # later copy: drop as duplicate
        else:
            self._count[key] = 1
            self._first[key] = unit
            feed.emit.append(unit)  # first copy: forward
        return feed

    def flush(self) -> FecFeed:
        """Any sequence seen on only one path had its partner lost; that lone
        copy was the stream's only survivor -> flag RECOVER."""
        feed = FecFeed()
        for key, count in self._count.items():
            if count == 1:
                feed.recovered.append(self._first[key])
        self._count = {}
        self._first = {}
        return feed


# --------------------------------------------------------------------------
# Pipeline stage wrappers
# --------------------------------------------------------------------------


class FecEncodeStage(StageBase):
    """Wrap an :class:`FecEncoder` as a pipeline stage (fans out)."""

    name = "fec"

    def __init__(self, encoder: FecEncoder) -> None:
        self._enc = encoder

    def process(self, unit: MediaUnit, now: float) -> StageOutcome:
        out = StageOutcome()
        out.events.append(self._event(EventKind.IN, unit.seq, unit.pts, now))
        for emitted in self._enc.push(unit):
            out.emit.append(emitted)
            out.events.append(
                self._event(
                    EventKind.OUT,
                    emitted.seq,
                    emitted.pts,
                    now,
                    unit_kind=emitted.kind.value,
                )
            )
        return out

    def flush(self, now: float) -> StageOutcome:
        out = StageOutcome()
        for emitted in self._enc.flush():
            out.emit.append(emitted)
            out.events.append(
                self._event(
                    EventKind.OUT,
                    emitted.seq,
                    emitted.pts,
                    now,
                    unit_kind=emitted.kind.value,
                )
            )
        return out

    def _event(
        self, kind: EventKind, seq: int, pts: int, now: float, **detail: object
    ) -> StageEvent:
        return StageEvent(
            stage=self.name, kind=kind, seq=seq, pts=pts, now=now, detail=dict(detail)
        )


class FecDecodeStage(StageBase):
    """Wrap an :class:`FecDecoder` as a pipeline stage (reconstructs/dedups)."""

    name = "fec-decode"

    def __init__(self, decoder: FecDecoder) -> None:
        self._dec = decoder

    def process(self, unit: MediaUnit, now: float) -> StageOutcome:
        out = StageOutcome()
        out.events.append(self._event(EventKind.IN, unit.seq, unit.pts, now))
        self._absorb(out, self._dec.push(unit), now)
        return out

    def flush(self, now: float) -> StageOutcome:
        out = StageOutcome()
        self._absorb(out, self._dec.flush(), now)
        return out

    def _absorb(self, out: StageOutcome, feed: FecFeed, now: float) -> None:
        for unit in feed.emit:
            out.emit.append(unit)
            out.events.append(self._event(EventKind.OUT, unit.seq, unit.pts, now))
        for unit in feed.recovered:
            out.events.append(self._event(EventKind.RECOVER, unit.seq, unit.pts, now))
        for unit in feed.deduped:
            out.events.append(
                self._event(EventKind.DROP, unit.seq, unit.pts, now, why="duplicate")
            )
        for report in feed.reports:
            out.events.append(
                self._event(
                    EventKind.STAT, int(report.get("group", 0)), 0, now, **report
                )
            )

    def _event(
        self, kind: EventKind, seq: int, pts: int, now: float, **detail: object
    ) -> StageEvent:
        return StageEvent(
            stage=self.name, kind=kind, seq=seq, pts=pts, now=now, detail=dict(detail)
        )
