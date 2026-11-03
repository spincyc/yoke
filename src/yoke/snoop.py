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
import math
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal

from yoke.core import frames
from yoke.core.types import (
    INTENT_CACHE_TTL,
    MAX_SEQUENCE,
    MAX_TRACKED,
    NODE_ID_RE,
    NS_PER_S,
    PEER_FORGET,
    PEER_TIMEOUT,
    SEQ_FUTURE_MARGIN_S,
    VERSION_ZERO,
    Body,
    DropReason,
    Frame,
    FrameType,
    Kind,
    NodeState,
    Role,
    Version,
    version_key,
)
from yoke.terminal import escape_terminal as escape_terminal

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
    driver_status: Body | None = None


@dataclass
class IntentView:
    """Latest candidate at the highest observed version, with ACK evidence.

    Conflicting bodies at one version are ambiguous: ``ack_applied`` keeps
    the version-only ACK result while ``applied`` remains unknown.
    """

    op: str
    producer: str | None
    version: Version
    acked: bool = False
    applied: bool | None = None
    reason: str | None = None
    ambiguous: bool = False
    ack_applied: bool | None = None


def _as_str(value: object, default: str) -> str:
    return value if isinstance(value, str) else default


def _as_version(value: object) -> Version | None:
    if (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], int)
        and not isinstance(value[0], bool)
        and 0 <= value[0] <= MAX_SEQUENCE
        and isinstance(value[1], str)
        and (
            (value[0] == 0 and value[1] == "")
            or (value[0] > 0 and NODE_ID_RE.fullmatch(value[1]))
        )
    ):
        return (value[0], value[1])
    return None


def _valid_body(frame: Frame) -> bool:
    body = frame.body
    if frame.type is FrameType.ANN:
        if (
            body.get("role") not in (Role.PRODUCER.value, Role.CONSUMER.value)
            or body.get("kind") not in (Kind.HDMI.value, Kind.USB.value)
            or body.get("state") not in tuple(state.value for state in NodeState)
            or not isinstance(body.get("name"), str)
            or len(body["name"]) > 64
            or (body.get("err") is not None and not isinstance(body["err"], str))
        ):
            return False
        if body["role"] == Role.PRODUCER.value:
            return body.get("media") is None or isinstance(body["media"], dict)
        pairing = body.get("pairing")
        if not isinstance(pairing, dict):
            return False
        version = _as_version(pairing.get("version"))
        producer = pairing.get("producer")
        return version is not None and (
            producer is None
            or (
                isinstance(producer, str)
                and NODE_ID_RE.fullmatch(producer) is not None
                and version != VERSION_ZERO
            )
        )
    if frame.type in (FrameType.INT, FrameType.ACK):
        version = _as_version(body.get("version"))
        if version is None or version == VERSION_ZERO:
            return False
        if frame.type is FrameType.INT:
            consumer = body.get("consumer")
            return (
                body.get("op") in ("pair", "unpair")
                and isinstance(consumer, str)
                and NODE_ID_RE.fullmatch(consumer) is not None
            )
        return isinstance(body.get("applied"), bool) and body.get("reason") in (
            "applied",
            "superseded",
            "unknown-producer",
            "driver-error",
            "kind-mismatch",
            "self-pair",
        )
    if frame.type is FrameType.EVT:
        return isinstance(body.get("event"), str) and isinstance(
            body.get("detail"), dict
        )
    return True


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
    driver_status = body.get("driver_status")
    if isinstance(driver_status, dict):
        parts.append(f"driver_status={_compact(driver_status)}")
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
    line = (
        f"+{record.rel_time:8.3f}s {origin:<16} {frame_type:<4} {tag}{record.summary}"
    )
    return escape_terminal(line)


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
    driver_status = body.get("driver_status")
    return NodeView(
        node_id=origin,
        role=_as_str(body.get("role"), "?"),
        kind=_as_str(body.get("kind"), "?"),
        name=_as_str(body.get("name"), ""),
        state=_as_str(body.get("state"), "?"),
        err=err if isinstance(err, str) else None,
        pairing_producer=producer,
        pairing_version=version,
        media=deepcopy(media) if isinstance(media, dict) else None,
        last_seen=now,
        driver_status=deepcopy(driver_status)
        if isinstance(driver_status, dict)
        else None,
    )


class Snooper:
    """Stateful observer: decode, judge, cache, reconstruct.

    Deliberately has no socket, no send method, and no clock — every
    input arrives through :meth:`observe` or an injected :meth:`topology`
    timestamp. Construction takes paired monotonic ``now`` and startup
    ``wall_time`` origins for the same future-sequence bound as an agent;
    omitting ``wall_time`` uses ``now`` as both clock origins.
    """

    def __init__(
        self,
        psk: bytes = b"",
        *,
        now: float = 0.0,
        wall_time: float | None = None,
    ) -> None:
        wall_time = now if wall_time is None else wall_time
        if not math.isfinite(now) or not math.isfinite(wall_time) or wall_time < 0:
            raise ValueError(
                "observer clocks must be finite; wall time must be nonnegative"
            )
        self._psk = psk
        self._wall_offset = wall_time - now
        self._start: float | None = None
        self._high_seq: dict[str, int] = {}
        self._high_canon: dict[str, bytes] = {}
        self._replay_seen: dict[str, float] = {}
        self._intent_seen: dict[str, float] = {}
        self._acks: dict[tuple[str, Version], tuple[Body, float]] = {}
        self._now = now
        self.nodes: dict[str, NodeView] = {}
        self.intents: dict[str, IntentView] = {}
        self.counts: dict[str, int] = dict.fromkeys(VERDICTS, 0)

    def observe(self, raw: bytes, now: float) -> list[SnoopRecord]:
        """Judge one datagram received at ``now``; returns one record."""
        if self._start is None:
            self._start = now
        rel = now - self._start
        self._now = now
        self._prune(now)
        frame, verdict, detail = self._decode(raw)
        if frame is None:
            self.counts[verdict] += 1
            summary = f"drop: {verdict}" + (f" ({detail})" if detail else "")
            return [SnoopRecord(verdict, "", "", summary, None, len(raw), rel)]
        if verdict == "ok":
            verdict = self._replay(frame, raw, now)
        if verdict == "ok":
            self._replay_seen[frame.origin] = now
            if _valid_body(frame):
                self._ingest(frame, now)
            else:
                verdict = "malformed"
        self._cap_caches()
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

    def topology(self, now: float | None = None) -> dict[str, Any]:
        """Reconstruct retained observations, evaluated at injected ``now``.

        Supply current monotonic time to age liveness and expire caches
        during packet silence. Without it, use the most recently observed
        or explicitly evaluated time. This method never reads a clock.
        """
        if now is not None:
            self._now = now
            self._prune(now)
        producers: dict[str, Any] = {}
        consumers: dict[str, Any] = {}
        for node_id, view in sorted(self.nodes.items()):
            entry: dict[str, Any] = {
                "kind": view.kind,
                "name": view.name,
                "state": view.state,
                "last_seen": view.last_seen,
                "live": self._now - view.last_seen <= PEER_TIMEOUT,
            }
            if view.err:
                entry["err"] = view.err
            if view.driver_status is not None:
                entry["driver_status"] = deepcopy(view.driver_status)
            if view.role == "producer":
                entry["media"] = deepcopy(view.media)
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
                **(
                    {"ambiguous": True, "ack_applied": view.ack_applied}
                    if view.ambiguous
                    else {}
                ),
            }
            for consumer, view in sorted(self.intents.items())
        }
        return {
            "producers": producers,
            "consumers": consumers,
            "intents": intents,
            "counts": dict(self.counts),
            "authentication": "site-psk" if self._psk else "open-unauthenticated",
            "retention": {
                "max_tracked": MAX_TRACKED,
                "peer_forget": PEER_FORGET,
                "intent_cache_ttl": INTENT_CACHE_TTL,
                "scope": "accepted retained observations only",
            },
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

    def _replay(self, frame: Frame, raw: bytes, now: float) -> Verdict:
        # Match node admission before accepting any replay high-water mark.
        if frame.seq > (int(now + self._wall_offset) + SEQ_FUTURE_MARGIN_S) * NS_PER_S:
            return "stale"
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
            self._ingest_intent(frame.body, now)
        elif frame.type is FrameType.ACK:
            version = _as_version(frame.body.get("version"))
            assert version is not None
            self._acks[(frame.origin, version)] = (dict(frame.body), now)
            self._ingest_ack(frame.origin, frame.body)

    def _ingest_intent(self, body: Body, now: float) -> None:
        consumer = body.get("consumer")
        op = body.get("op")
        version = _as_version(body.get("version"))
        if not isinstance(consumer, str) or not isinstance(op, str) or version is None:
            return
        current = self.intents.get(consumer)
        producer = body.get("producer")
        producer_id = producer if op == "pair" and isinstance(producer, str) else None
        ambiguous = False
        if current is not None:
            if version_key(version) < version_key(current.version):
                return
            if version == current.version:
                if (op, producer_id) == (current.op, current.producer):
                    return  # Identical anti-entropy re-multicast.
                # A version-only ACK cannot identify which conflicting body
                # committed, even when a rejected operation was corrected.
                ambiguous = True
                self._acks.pop((consumer, version), None)
        self.intents[consumer] = IntentView(
            op=op,
            producer=producer_id,
            version=version,
            ambiguous=ambiguous,
        )
        self._intent_seen[consumer] = now
        ack = self._acks.get((consumer, version))
        if ack is not None:
            self._ingest_ack(consumer, ack[0])

    def _ingest_ack(self, origin: str, body: Body) -> None:
        version = _as_version(body.get("version"))
        intent = self.intents.get(origin)
        if intent is None or version != intent.version:
            return
        intent.acked = True
        applied = body.get("applied")
        intent.ack_applied = applied if isinstance(applied, bool) else None
        intent.applied = None if intent.ambiguous else intent.ack_applied
        reason = body.get("reason")
        intent.reason = reason if isinstance(reason, str) else None

    def _prune(self, now: float) -> None:
        for origin, seen in list(self._replay_seen.items()):
            if now - seen > PEER_FORGET:
                self._replay_seen.pop(origin, None)
                self._high_seq.pop(origin, None)
                self._high_canon.pop(origin, None)
        for origin, node in list(self.nodes.items()):
            if now - node.last_seen > PEER_FORGET:
                del self.nodes[origin]
        for consumer, seen in list(self._intent_seen.items()):
            if now - seen > INTENT_CACHE_TTL:
                del self._intent_seen[consumer]
                self.intents.pop(consumer, None)
        for key, (_, seen) in list(self._acks.items()):
            if now - seen > INTENT_CACHE_TTL:
                del self._acks[key]

    def _cap_caches(self) -> None:
        while len(self._high_seq) > MAX_TRACKED:
            oldest = next(iter(self._high_seq))
            del self._high_seq[oldest]
            self._high_canon.pop(oldest, None)
            self._replay_seen.pop(oldest, None)
        while len(self.nodes) > MAX_TRACKED:
            del self.nodes[next(iter(self.nodes))]
        while len(self.intents) > MAX_TRACKED:
            oldest = next(iter(self.intents))
            del self.intents[oldest]
            self._intent_seen.pop(oldest, None)
        while len(self._acks) > MAX_TRACKED:
            del self._acks[next(iter(self._acks))]
