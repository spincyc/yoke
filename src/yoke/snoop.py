"""Passive conformance oracle for the yoke control group.

The snooper is fed raw datagrams plus a timestamp and judges each one the
way an agent would (envelope validity, authentication, per-origin replay)
— but it *reports* every judgment instead of silently dropping, and it
reconstructs the control-plane topology from the multicast stream alone
(PROTOCOL.md "Snooper"). Signature failures are not discarded: when the
keyed decode fails the frame is re-decoded without the key so its content
can still be rendered, marked ``bad-sig``.

The snooper never transmits. It owns no socket and has no send path of
any kind; the wiring in :mod:`yoke.cli` reads datagrams and calls
:meth:`Snooper.observe`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from yoke.core import frames
from yoke.core.types import (
    VERSION_ZERO,
    Body,
    DropReason,
    Frame,
    FrameType,
    Version,
    version_key,
)

Verdict = Literal["ok", "bad-sig", "stale", "dup", "malformed", "version", "oversize"]

VERDICTS: tuple[Verdict, ...] = (
    "ok",
    "bad-sig",
    "stale",
    "dup",
    "malformed",
    "version",
    "oversize",
)

_DROP_VERDICTS: dict[DropReason, Verdict] = {
    DropReason.MALFORMED: "malformed",
    DropReason.VERSION: "version",
    DropReason.OVERSIZE: "oversize",
}


@dataclass(frozen=True)
class SnoopRecord:
    """One observed datagram, judged.

    ``frame`` is present whenever any content could be extracted — for
    ``bad-sig`` it holds the unauthenticated content. ``rel_time`` is
    seconds since the snooper's first observation (for rendering).
    """

    verdict: Verdict
    origin: str
    frame_type: str
    summary: str
    frame: Frame | None
    raw_len: int
    rel_time: float


@dataclass
class NodeView:
    """Announce-cache entry: everything the last accepted ``ann`` said."""

    node_id: str
    role: str
    kind: str
    name: str
    state: str
    err: str | None
    pairing_producer: str | None
    pairing_version: Version | None
    media: Body | None
    last_seen: float


@dataclass
class IntentView:
    """Highest-version intent observed per consumer, with its ack fate."""

    op: str
    producer: str | None
    version: Version
    acked: bool = False
    applied: bool | None = None
    reason: str | None = None


def _as_str(value: object, default: str) -> str:
    return value if isinstance(value, str) else default


def _as_version(value: object) -> Version | None:
    if (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], int)
        and not isinstance(value[0], bool)
        and isinstance(value[1], str)
    ):
        return (value[0], value[1])
    return None


def _fmt_version(version: Version | None) -> str:
    n, origin = version or VERSION_ZERO
    return f"[{n},{origin}]"


def _compact(obj: Body) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _summarize_ann(body: Body) -> str:
    parts = [
        f"{_as_str(body.get('role'), '?')}/{_as_str(body.get('kind'), '?')}",
        f"state={_as_str(body.get('state'), '?')}",
    ]
    err = body.get("err")
    if isinstance(err, str) and err:
        parts.append(f"err={err}")
    pairing = body.get("pairing")
    if isinstance(pairing, dict):
        producer = pairing.get("producer")
        if isinstance(producer, str):
            version = _fmt_version(_as_version(pairing.get("version")))
            parts.append(f"pairing={producer}@{version}")
        else:
            parts.append("unpaired")
    media = body.get("media")
    if isinstance(media, dict):
        parts.append(f"media={_compact(media)}")
    name = _as_str(body.get("name"), "")
    if name:
        parts.append(f"name={name!r}")
    return " ".join(parts)


def summarize(frame: Frame) -> str:
    """One human line for the interesting bits of a frame body."""
    body = frame.body
    if frame.type is FrameType.ANN:
        return _summarize_ann(body)
    if frame.type is FrameType.INT:
        op = _as_str(body.get("op"), "?")
        consumer = _as_str(body.get("consumer"), "?")
        version = _fmt_version(_as_version(body.get("version")))
        if op == "pair":
            producer = _as_str(body.get("producer"), "?")
            return f"pair {consumer}<-{producer}@{version}"
        return f"{op} {consumer}@{version}"
    if frame.type is FrameType.ACK:
        applied = body.get("applied")
        reason = _as_str(body.get("reason"), "?")
        version = _fmt_version(_as_version(body.get("version")))
        return f"applied={str(applied).lower()} reason={reason} version={version}"
    if frame.type is FrameType.EVT:
        event = _as_str(body.get("event"), "?")
        detail = body.get("detail")
        if isinstance(detail, dict) and detail:
            return f"{event} {_compact(detail)}"
        return event
    return "bye"


def render_text(record: SnoopRecord) -> str:
    """One-line human rendering: relative time, origin, type, body bits.

    Non-``ok`` verdicts carry an upper-case marker (``[BAD-SIG]`` etc.).
    """
    tag = "" if record.verdict == "ok" else f"[{record.verdict.upper()}] "
    origin = record.origin or "?"
    frame_type = record.frame_type or "-"
    return (
        f"+{record.rel_time:8.3f}s {origin:<16} {frame_type:<4} {tag}{record.summary}"
    )


def render_json(record: SnoopRecord) -> str:
    """Single-line JSON with sorted keys (machine-friendly snoop output)."""
    obj: dict[str, Any] = {
        "verdict": record.verdict,
        "origin": record.origin,
        "type": record.frame_type,
        "summary": record.summary,
        "raw_len": record.raw_len,
        "rel_time": round(record.rel_time, 6),
        "seq": record.frame.seq if record.frame else None,
        "ts": record.frame.ts if record.frame else None,
        "body": record.frame.body if record.frame else None,
    }
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _node_view(origin: str, body: Body, now: float) -> NodeView:
    producer: str | None = None
    version: Version | None = None
    pairing = body.get("pairing")
    if isinstance(pairing, dict):
        raw_producer = pairing.get("producer")
        if isinstance(raw_producer, str):
            producer = raw_producer
        version = _as_version(pairing.get("version")) or VERSION_ZERO
    media = body.get("media")
    err = body.get("err")
    return NodeView(
        node_id=origin,
        role=_as_str(body.get("role"), "?"),
        kind=_as_str(body.get("kind"), "?"),
        name=_as_str(body.get("name"), ""),
        state=_as_str(body.get("state"), "?"),
        err=err if isinstance(err, str) else None,
        pairing_producer=producer,
        pairing_version=version,
        media=media if isinstance(media, dict) else None,
        last_seen=now,
    )


class Snooper:
    """Stateful observer: decode, judge, cache, reconstruct.

    Deliberately has no socket, no send method, and no clock — every
    input arrives through :meth:`observe`.
    """

    def __init__(self, psk: bytes = b"") -> None:
        self._psk = psk
        self._start: float | None = None
        self._high_seq: dict[str, int] = {}
        self._high_canon: dict[str, bytes] = {}
        self.nodes: dict[str, NodeView] = {}
        self.intents: dict[str, IntentView] = {}
        self.counts: dict[str, int] = dict.fromkeys(VERDICTS, 0)

    def observe(self, raw: bytes, now: float) -> list[SnoopRecord]:
        """Judge one datagram received at ``now``; returns one record."""
        if self._start is None:
            self._start = now
        rel = now - self._start
        frame, verdict, detail = self._decode(raw)
        if frame is None:
            self.counts[verdict] += 1
            summary = f"drop: {verdict}" + (f" ({detail})" if detail else "")
            return [SnoopRecord(verdict, "", "", summary, None, len(raw), rel)]
        if verdict == "ok":
            verdict = self._replay(frame, raw)
        if verdict == "ok":
            self._ingest(frame, now)
        self.counts[verdict] += 1
        record = SnoopRecord(
            verdict=verdict,
            origin=frame.origin,
            frame_type=frame.type.value,
            summary=summarize(frame),
            frame=frame,
            raw_len=len(raw),
            rel_time=rel,
        )
        return [record]

    def topology(self) -> dict[str, Any]:
        """Current global view, reconstructed purely from observed frames."""
        producers: dict[str, Any] = {}
        consumers: dict[str, Any] = {}
        for node_id, view in sorted(self.nodes.items()):
            entry: dict[str, Any] = {
                "kind": view.kind,
                "name": view.name,
                "state": view.state,
                "last_seen": view.last_seen,
            }
            if view.err:
                entry["err"] = view.err
            if view.role == "producer":
                entry["media"] = view.media
                producers[node_id] = entry
            elif view.role == "consumer":
                entry["pairing"] = {
                    "producer": view.pairing_producer,
                    "version": list(view.pairing_version or VERSION_ZERO),
                }
                consumers[node_id] = entry
        intents = {
            consumer: {
                "op": view.op,
                "producer": view.producer,
                "version": list(view.version),
                "acked": view.acked,
                "applied": view.applied,
                "reason": view.reason,
            }
            for consumer, view in sorted(self.intents.items())
        }
        return {
            "producers": producers,
            "consumers": consumers,
            "intents": intents,
            "counts": dict(self.counts),
        }

    # -- decoding ------------------------------------------------------------

    def _decode(self, raw: bytes) -> tuple[Frame | None, Verdict, str]:
        try:
            return frames.decode(raw, self._psk), "ok", ""
        except frames.FrameDrop as drop:
            if drop.reason is not DropReason.AUTH:
                return None, _DROP_VERDICTS[drop.reason], drop.detail
        # Signature failure: extract what we can, keep the bad-sig verdict.
        if self._psk:
            try:
                # An open-mode frame (sig "") on a keyed network.
                return frames.decode(raw, b""), "bad-sig", ""
            except frames.FrameDrop:
                pass
        try:
            obj = json.loads(raw.decode("utf-8"))
            if isinstance(obj, dict):
                # A keyed frame with a wrong or tampered signature: strip
                # the sig so the (unauthenticated) content still renders.
                stripped = dict(obj)
                stripped["sig"] = ""
                return (
                    frames.decode(frames.canonical_bytes(stripped), b""),
                    ("bad-sig"),
                    "",
                )
        except (frames.FrameDrop, ValueError):
            pass
        return None, "bad-sig", "content unrecoverable"

    # -- replay judgment (same rule as an agent; PROTOCOL.md) ----------------

    def _replay(self, frame: Frame, raw: bytes) -> Verdict:
        canon = frames.canonical_bytes(json.loads(raw.decode("utf-8")))
        high = self._high_seq.get(frame.origin)
        if high is not None and frame.seq <= high:
            if frame.seq == high and canon == self._high_canon.get(frame.origin):
                return "dup"
            return "stale"
        self._high_seq[frame.origin] = frame.seq
        self._high_canon[frame.origin] = canon
        return "ok"

    # -- caches --------------------------------------------------------------

    def _ingest(self, frame: Frame, now: float) -> None:
        if frame.type is FrameType.ANN:
            self.nodes[frame.origin] = _node_view(frame.origin, frame.body, now)
        elif frame.type is FrameType.BYE:
            self.nodes.pop(frame.origin, None)
        elif frame.type is FrameType.INT:
            self._ingest_intent(frame.body)
        elif frame.type is FrameType.ACK:
            self._ingest_ack(frame.origin, frame.body)

    def _ingest_intent(self, body: Body) -> None:
        consumer = body.get("consumer")
        op = body.get("op")
        version = _as_version(body.get("version"))
        if not isinstance(consumer, str) or not isinstance(op, str) or version is None:
            return
        current = self.intents.get(consumer)
        if current is not None and version_key(version) <= version_key(current.version):
            return  # anti-entropy re-multicast or an older intent
        producer = body.get("producer")
        self.intents[consumer] = IntentView(
            op=op,
            producer=producer if isinstance(producer, str) else None,
            version=version,
        )

    def _ingest_ack(self, origin: str, body: Body) -> None:
        version = _as_version(body.get("version"))
        intent = self.intents.get(origin)
        if intent is None or version != intent.version:
            return
        intent.acked = True
        applied = body.get("applied")
        intent.applied = applied if isinstance(applied, bool) else None
        reason = body.get("reason")
        intent.reason = reason if isinstance(reason, str) else None
