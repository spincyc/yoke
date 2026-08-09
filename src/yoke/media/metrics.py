"""Aggregate media-pipeline health, computed only from ``StageEvent`` taps.

The :class:`MetricsCollector` is the white-box half of media observability:
it consumes the per-stage :class:`~yoke.media.types.StageEvent` stream and
folds it into two orthogonal views —

* **per stage** (keyed by stage name): raw ``in``/``out``/``drop``/
  ``recover``/``late``/``hold``/``adjust`` and integrity tallies, plus the
  latest ``STAT`` snapshot a stage published; and
* **end-to-end per stream**: frames sourced vs delivered ok/bad, packet
  loss across the wire, FEC recovery count/rate, the effective compression
  ratio, the rate controller's current target, and glass-to-glass latency.

Nothing here reads a clock, a socket, or the units themselves — every
number is derived from events, so observation never perturbs the stream
(MEDIA.md, "Metrics are computed only from ``StageEvent``s ... never by
instrumenting the transform"). Everything is pure and deterministic:
:meth:`MetricsCollector.snapshot` returns a stable, sorted, JSON-ready
:data:`~yoke.media.types.Body` that other tools (``snoop``, ``probe``)
render.

Event ``detail`` schema this collector reads (all keys optional):

* ``stream`` — stream id grouping the end-to-end view; absent → ``"-"``.
* ``frame_bytes`` / ``bytes`` — a unit's byte size, for the ratio and any
  external bitrate math. ``source`` OUT carries the *uncompressed* frame
  size; ``codec`` OUT carries the *compressed* size (summed over slices).
* ``ok`` — on a sink ``INTEGRITY`` event, whether the digest verified.
* ``latency`` — on an ``INTEGRITY`` event, a precomputed glass-to-glass
  latency (seconds); when absent it is derived from the matching
  ``source`` OUT ``now`` for the same ``pts``.
* ``target`` — on an ``ADJUST`` event, the rate controller's new ratio.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from yoke.media.types import Body, EventKind, StageEvent

# Stream id used when an event's detail does not name one.
DEFAULT_STREAM = "-"

# Per-stage counter slots. ``in`` is a plain dict key (not the keyword).
_STAGE_SLOTS: tuple[str, ...] = (
    "in",
    "out",
    "drop",
    "recover",
    "late",
    "hold",
    "adjust",
    "integrity_ok",
    "integrity_bad",
)

# EventKind -> per-stage counter slot for the simple pass-through kinds.
_KIND_SLOT: dict[EventKind, str] = {
    EventKind.IN: "in",
    EventKind.OUT: "out",
    EventKind.DROP: "drop",
    EventKind.RECOVER: "recover",
    EventKind.LATE: "late",
    EventKind.HOLD: "hold",
    EventKind.ADJUST: "adjust",
}


@dataclass(frozen=True)
class StageRoles:
    """Names of the stages that mark the end-to-end measurement points.

    Defaults match the canonical pipeline in MEDIA.md (``source → codec →
    packetize → fec → [wire] → jitter → … → sink``): the wire is crossed
    between ``fec`` (last sender stage, ``OUT`` = packets launched) and
    ``jitter`` (first receiver stage, ``IN`` = packets that arrived).
    Override for pipelines shaped differently.
    """

    source: str = "source"
    codec: str = "codec"
    ratecontrol: str = "ratecontrol"
    wire_out: str = "fec"
    wire_in: str = "jitter"
    sink: str = "sink"


@dataclass
class _StreamAgg:
    """Mutable end-to-end accumulator for a single stream."""

    frames_sourced: int = 0
    frames_ok: int = 0
    frames_bad: int = 0
    packets_sent: int = 0
    packets_recv: int = 0
    fec_recovered: int = 0
    src_bytes: int = 0
    codec_bytes: int = 0
    target_ratio: float | None = None
    latencies: list[float] = field(default_factory=list)


def _new_stage() -> dict[str, int]:
    return dict.fromkeys(_STAGE_SLOTS, 0)


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _as_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _unit_bytes(detail: Body) -> int:
    """A unit's byte size from ``frame_bytes`` (preferred) or ``bytes``."""
    for key in ("frame_bytes", "bytes"):
        n = _as_int(detail.get(key))
        if n is not None:
            return n
    return 0


class MetricsCollector:
    """Fold a ``StageEvent`` stream into per-stage and per-stream health.

    Deliberately has no clock and no I/O: the only input is
    :meth:`observe`. All outputs (:meth:`snapshot`, :meth:`stage_report`)
    are freshly built, sorted, JSON-serializable dicts.
    """

    def __init__(self, roles: StageRoles | None = None) -> None:
        self._roles = roles or StageRoles()
        self._stages: dict[str, dict[str, int]] = {}
        self._stats: dict[str, Body] = {}
        self._streams: dict[str, _StreamAgg] = {}
        # (stream, pts) -> source-OUT ``now``, for deriving latency.
        self._source_now: dict[tuple[str, int], float] = {}

    # -- intake --------------------------------------------------------------

    def observe(self, event: StageEvent) -> None:
        """Consume one tap. Idempotent-free: call once per emitted event."""
        self._count_stage(event)
        self._update_streams(event)

    def _count_stage(self, event: StageEvent) -> None:
        stage = self._stages.setdefault(event.stage, _new_stage())
        slot = _KIND_SLOT.get(event.kind)
        if slot is not None:
            stage[slot] += 1
        elif event.kind is EventKind.INTEGRITY:
            if event.detail.get("ok") is False:
                stage["integrity_bad"] += 1
            else:
                stage["integrity_ok"] += 1
        elif event.kind is EventKind.STAT:
            self._stats[event.stage] = dict(event.detail)

    def _update_streams(self, event: StageEvent) -> None:
        roles = self._roles
        stream_id = self._stream_of(event.detail)
        agg = self._streams.setdefault(stream_id, _StreamAgg())

        if event.stage == roles.source and event.kind is EventKind.OUT:
            agg.frames_sourced += 1
            agg.src_bytes += _unit_bytes(event.detail)
            self._source_now[(stream_id, event.pts)] = event.now
        elif event.stage == roles.codec and event.kind is EventKind.OUT:
            agg.codec_bytes += _unit_bytes(event.detail)
        elif event.stage == roles.wire_out and event.kind is EventKind.OUT:
            agg.packets_sent += 1
        elif event.stage == roles.wire_in and event.kind is EventKind.IN:
            agg.packets_recv += 1
        elif event.stage == roles.sink and event.kind is EventKind.INTEGRITY:
            self._record_integrity(agg, stream_id, event)

        if event.kind is EventKind.RECOVER:
            agg.fec_recovered += 1
        elif event.kind is EventKind.ADJUST:
            target = _as_float(event.detail.get("target"))
            if target is not None:
                agg.target_ratio = target

    def _record_integrity(
        self, agg: _StreamAgg, stream_id: str, event: StageEvent
    ) -> None:
        if event.detail.get("ok") is False:
            agg.frames_bad += 1
        else:
            agg.frames_ok += 1
        latency = _as_float(event.detail.get("latency"))
        if latency is None:
            sent = self._source_now.get((stream_id, event.pts))
            if sent is not None:
                latency = event.now - sent
        if latency is not None:
            agg.latencies.append(latency)

    @staticmethod
    def _stream_of(detail: Body) -> str:
        value = detail.get("stream")
        return value if isinstance(value, str) and value else DEFAULT_STREAM

    # -- reporting -----------------------------------------------------------

    def snapshot(self) -> Body:
        """A stable, sorted, JSON-serializable view of all health."""
        streams = {
            sid: self._stream_body(agg) for sid, agg in sorted(self._streams.items())
        }
        stages = {name: self._stage_body(name) for name in sorted(self._stages)}
        return {
            "stages": stages,
            "streams": streams,
            "totals": self._totals(),
        }

    def stage_report(self, name: str) -> Body:
        """Per-stage counters (plus any latest ``STAT``) for one stage."""
        return self._stage_body(name)

    def _stage_body(self, name: str) -> Body:
        counters = self._stages.get(name)
        body: Body = dict(counters) if counters is not None else _new_stage()
        stat = self._stats.get(name)
        if stat is not None:
            body["stat"] = dict(stat)
        return body

    @staticmethod
    def _stream_body(agg: _StreamAgg) -> Body:
        loss = max(agg.packets_sent - agg.packets_recv, 0)
        loss_rate = loss / agg.packets_sent if agg.packets_sent else 0.0
        recover_denom = agg.fec_recovered + loss
        recovery_rate = agg.fec_recovered / recover_denom if recover_denom else 0.0
        ratio = agg.src_bytes / agg.codec_bytes if agg.codec_bytes else None
        return {
            "frames_sourced": agg.frames_sourced,
            "frames_ok": agg.frames_ok,
            "frames_bad": agg.frames_bad,
            "packets_sent": agg.packets_sent,
            "packets_recv": agg.packets_recv,
            "packet_loss": loss,
            "packet_loss_rate": round(loss_rate, 6),
            "fec_recovered": agg.fec_recovered,
            "fec_recovery_rate": round(recovery_rate, 6),
            "ratio": round(ratio, 6) if ratio is not None else None,
            "target_ratio": agg.target_ratio,
            "latency": _latency_body(agg.latencies),
        }

    def _totals(self) -> Body:
        loss = 0
        sent = 0
        recv = 0
        recovered = 0
        sourced = 0
        ok = 0
        bad = 0
        for agg in self._streams.values():
            sent += agg.packets_sent
            recv += agg.packets_recv
            loss += max(agg.packets_sent - agg.packets_recv, 0)
            recovered += agg.fec_recovered
            sourced += agg.frames_sourced
            ok += agg.frames_ok
            bad += agg.frames_bad
        return {
            "frames_sourced": sourced,
            "frames_ok": ok,
            "frames_bad": bad,
            "packets_sent": sent,
            "packets_recv": recv,
            "packet_loss": loss,
            "fec_recovered": recovered,
        }


def _latency_body(samples: list[float]) -> Body | None:
    if not samples:
        return None
    return {
        "samples": len(samples),
        "last": round(samples[-1], 6),
        "mean": round(sum(samples) / len(samples), 6),
        "max": round(max(samples), 6),
    }
