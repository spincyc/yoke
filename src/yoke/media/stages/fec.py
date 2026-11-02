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

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from yoke.media.pipeline import StageBase
from yoke.media.types import (
    MAX_BUFFER_BYTES,
    MAX_FEC_GROUP_SIZE,
    MAX_UDP_BYTES,
    Body,
    EventKind,
    MediaUnit,
    StageEvent,
    StageOutcome,
    UnitKind,
    is_digest,
    require_uint,
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
        if require_uint(group, "group", MAX_FEC_GROUP_SIZE) == 0:
            raise ValueError("group must be positive")
        self._n = group
        self._buf: list[MediaUnit] = []
        self._group = 0

    def push(self, unit: MediaUnit) -> list[MediaUnit]:
        if len(unit.payload) > MAX_UDP_BYTES:
            raise ValueError("FEC packet exceeds UDP byte budget")
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
    size: int
    parity: MediaUnit | None = None
    members: list[Body] | None = None
    data: dict[int, MediaUnit] = field(default_factory=dict)
    bytes: int = 0


class XorParityDecoder:
    """Bounded XOR recovery; validate repair metadata before retaining it."""

    name = "xor"

    def __init__(
        self,
        *,
        max_groups: int = 256,
        max_bytes: int = MAX_BUFFER_BYTES,
        done_window: int = 1024,
    ) -> None:
        for name, value in (
            ("max_groups", max_groups),
            ("max_bytes", max_bytes),
            ("done_window", done_window),
        ):
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        self._max_groups = max_groups
        self._max_bytes = max_bytes
        self._done_window = done_window
        self._groups: OrderedDict[int, _Group] = OrderedDict()
        self._done: OrderedDict[int, None] = OrderedDict()
        self._bytes = 0

    def push(self, unit: MediaUnit) -> FecFeed:
        feed = FecFeed()
        if unit.kind is not UnitKind.PARITY and not (
            unit.kind is UnitKind.PACKET and "fec_group" in unit.meta
        ):
            feed.emit.append(unit)
            return feed
        members: list[Body] | None = None
        try:
            parity = unit.kind is UnitKind.PARITY
            group_id = require_uint(
                unit.meta["group" if parity else "fec_group"],
                "group",
                0xFFFF_FFFF_FFFF_FFFF,
            )
            size = require_uint(
                unit.meta["size" if parity else "fec_size"], "size", MAX_FEC_GROUP_SIZE
            )
            if size < 1 or len(unit.payload) > min(MAX_UDP_BYTES, self._max_bytes):
                raise ValueError("invalid size or payload budget")
            if parity:
                candidate = unit.meta["members"]
                if not isinstance(candidate, list) or len(candidate) != size:
                    raise ValueError("parity member count disagrees with size")
                members = []
                for member in candidate:
                    if not isinstance(member, dict) or not isinstance(
                        member.get("meta"), dict
                    ):
                        raise ValueError("invalid parity member")
                    require_uint(member["seq"], "seq", 65_535)
                    require_uint(member["pts"], "pts", 0xFFFF_FFFF_FFFF_FFFF)
                    require_uint(member["length"], "length", len(unit.payload))
                    if not is_digest(member["source_digest"]):
                        raise ValueError("invalid member digest")
                    members.append(dict(member))
                index = -1
            else:
                index = require_uint(unit.meta["fec_index"], "index", size - 1)
        except (KeyError, TypeError, ValueError):
            feed.reports.append(
                {
                    "fec": "xor",
                    "error": "bad-parity"
                    if unit.kind is UnitKind.PARITY
                    else "bad-data",
                }
            )
            return feed
        if group_id in self._done:
            feed.deduped.append(unit)
            return feed
        group = self._groups.get(group_id)
        if group is None:
            while len(self._groups) >= self._max_groups:
                self._retire(next(iter(self._groups)), feed, "capacity")
            group = _Group(size)
            self._groups[group_id] = group
        if parity:
            assert members is not None
            if (
                (
                    group.parity is not None
                    and (
                        group.parity.payload != unit.payload or group.members != members
                    )
                )
                or size > group.size
                or any(i >= size for i in group.data)
            ):
                self._retire(group_id, feed, "conflicting-parity")
                return feed
            if group.parity is not None:
                feed.deduped.append(unit)
                return feed
            for i, data in group.data.items():
                if not self._matches(data, members[i]):
                    self._retire(group_id, feed, "conflicting-member")
                    return feed
        else:
            if (group.parity is None and size != group.size) or (
                group.parity is not None and (size < group.size or index >= group.size)
            ):
                self._retire(group_id, feed, "conflicting-size")
                return feed
            if index in group.data:
                if group.data[index] != unit:
                    self._retire(group_id, feed, "conflicting-data")
                else:
                    feed.deduped.append(unit)
                return feed
            if group.members is not None and not self._matches(
                unit, group.members[index]
            ):
                self._retire(group_id, feed, "conflicting-member")
                return feed
        while self._bytes + len(unit.payload) > self._max_bytes:
            victim = next(iter(self._groups))
            self._retire(victim, feed, "byte-capacity")
            if victim == group_id:
                return feed
        self._bytes += len(unit.payload)
        group.bytes += len(unit.payload)
        if parity:
            group.parity, group.members, group.size = unit, members, size
        else:
            group.data[index] = unit
            feed.emit.append(unit)
        missing = group.size - len(group.data)
        if missing == 1 and group.parity is not None and group.members is not None:
            lost = next(i for i in range(group.size) if i not in group.data)
            member = group.members[lost]
            payload = xor_bytes(
                [group.parity.payload, *(u.payload for u in group.data.values())]
            )[: member["length"]]
            rebuilt = MediaUnit(
                kind=UnitKind.PACKET,
                seq=member["seq"],
                pts=member["pts"],
                payload=payload,
                source_digest=member["source_digest"],
                meta=dict(member["meta"]),
            )
            feed.emit.append(rebuilt)
            feed.recovered.append(rebuilt)
            self._remove(group_id)
        elif missing == 0:
            self._remove(group_id)
        return feed

    @staticmethod
    def _matches(unit: MediaUnit, member: Body) -> bool:
        return bool(
            unit.seq == member["seq"]
            and unit.pts == member["pts"]
            and len(unit.payload) == member["length"]
            and unit.source_digest == member["source_digest"]
            and unit.meta == member["meta"]
        )

    def _remove(self, group_id: int) -> None:
        self._bytes -= self._groups.pop(group_id).bytes
        self._done[group_id] = None
        while len(self._done) > self._done_window:
            self._done.popitem(last=False)

    def _retire(self, group_id: int, feed: FecFeed, reason: str) -> None:
        group = self._groups[group_id]
        missing = [i for i in range(group.size) if i not in group.data]
        if missing or reason not in {"insufficient", "no-parity"}:
            feed.reports.append(
                {
                    "fec": "xor",
                    "group": group_id,
                    "missing": missing,
                    "recovered": False,
                    "reason": reason,
                }
            )
        self._remove(group_id)

    def flush(self) -> FecFeed:
        feed = FecFeed()
        while self._groups:
            group_id = next(iter(self._groups))
            self._retire(
                group_id,
                feed,
                "insufficient"
                if self._groups[group_id].parity is not None
                else "no-parity",
            )
        return feed


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
    """First copy wins within bounded identities, including frame and SSRC.

    The sequence number alone repeats every 65536 packets. Full frame
    identity distinguishes those cycles. A path must differ to constitute
    a surviving partner; repeated copies from path A alone stay recoveries.
    """

    name = "dup"

    def __init__(
        self, *, max_packets: int = 32_768, max_bytes: int = MAX_BUFFER_BYTES
    ) -> None:
        if (
            type(max_packets) is not int
            or max_packets < 1
            or type(max_bytes) is not int
            or max_bytes < 1
        ):
            raise ValueError("merger budgets must be positive integers")
        self._max_packets = max_packets
        self._max_bytes = max_bytes
        self._bytes = 0
        self._first: OrderedDict[
            tuple[int, int, int, int], tuple[MediaUnit, set[str]]
        ] = OrderedDict()

    def push(self, unit: MediaUnit) -> FecFeed:
        if unit.kind is not UnitKind.PACKET:
            return FecFeed(emit=[unit])
        feed = FecFeed()
        try:
            key = (
                require_uint(unit.meta.get("rtp_ssrc", 0), "ssrc", 0xFFFF_FFFF),
                require_uint(unit.seq, "seq", 65_535),
                require_uint(unit.pts, "pts", 0xFFFF_FFFF_FFFF_FFFF),
                require_uint(
                    unit.meta.get("frame_seq", unit.seq),
                    "frame_seq",
                    0xFFFF_FFFF_FFFF_FFFF,
                ),
            )
            path = unit.meta.get("path", "")
            if path not in {"", "A", "B"}:
                raise ValueError("invalid duplicate path")
        except (TypeError, ValueError):
            feed.reports.append({"fec": "dup", "error": "bad-identity"})
            return feed
        previous = self._first.get(key)
        if previous is not None:
            first, paths = previous
            first_meta = {k: v for k, v in first.meta.items() if k != "path"}
            meta = {k: v for k, v in unit.meta.items() if k != "path"}
            if (first.payload, first.source_digest, first_meta) != (
                unit.payload,
                unit.source_digest,
                meta,
            ):
                feed.reports.append({"fec": "dup", "error": "conflicting-copy"})
            else:
                paths.add(path)
            feed.deduped.append(unit)
            return feed
        if len(unit.payload) > self._max_bytes:
            feed.reports.append({"fec": "dup", "error": "byte-capacity"})
            return feed
        while self._first and (
            len(self._first) >= self._max_packets
            or self._bytes + len(unit.payload) > self._max_bytes
        ):
            self._retire(feed)
        self._first[key] = (unit, {path})
        self._bytes += len(unit.payload)
        feed.emit.append(unit)
        return feed

    def _retire(self, feed: FecFeed) -> None:
        _, (unit, paths) = self._first.popitem(last=False)
        self._bytes -= len(unit.payload)
        if not {"A", "B"}.issubset(paths):
            feed.recovered.append(unit)

    def flush(self) -> FecFeed:
        feed = FecFeed()
        while self._first:
            self._retire(feed)
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
