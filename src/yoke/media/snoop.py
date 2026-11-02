"""Passive media verifier: the wire-and-tap analogue of ``yoke.snoop``.

:class:`MediaSnooper` reconstructs the health of a media stream two ways,
both strictly non-transmitting and non-mutating (MEDIA.md, "Two
observation modes"):

* **White-box** — :meth:`MediaSnooper.observe_event` folds in-process
  :class:`~yoke.media.types.StageEvent` taps, feeding an internal
  :class:`~yoke.media.metrics.MetricsCollector` and a per-stream view of
  which stages each frame traversed, its gaps and recoveries.
* **Black-box** — :meth:`MediaSnooper.observe_packet` reconstructs the
  same health from the packets on the wire *alone*: RTP sequence
  continuity (loss / reorder / dup), frames seen (marker bit / timestamp
  changes), and a bitrate estimate — with no tap of any kind. This is the
  true "snoop the wire without interfering," and the conformance oracle
  for the wire format.

The snooper owns no socket and has no send path; the only inputs are
``observe_event`` / ``observe_packet``. It never mutates or returns an
observed unit — feeding it N events or packets yields only
:class:`SnoopRecord` judgments (a testable non-interference invariant).

The RTP header is parsed by an **injected** decoder
(``Callable[[bytes], Body]``) so this module does not hard-depend on the
packetize lane. In production the injected decoder is
``yoke.media.stages.packetize.decode_rtp_header``; tests pass a small fake
decoder. The decoder returns a :data:`~yoke.media.types.Body` with, at
minimum, an integer ``seq`` (the 16-bit RTP sequence number), and
optionally ``timestamp`` (RTP media clock), ``marker`` (bool), ``pt``
(payload type) and ``ssrc`` (stream identifier).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from yoke.media.metrics import (
    DEFAULT_STREAM,
    MAX_PENDING_TIMES,
    MAX_STAGES,
    MAX_STREAMS,
    MetricsCollector,
    StageRoles,
)
from yoke.media.types import Body, EventKind, MediaUnit, StageEvent, UnitKind
from yoke.snoop import escape_terminal

Verdict = Literal[
    "ok",
    "loss",
    "reorder",
    "dup",
    "recovered",
    "integrity-fail",
    "malformed",
]

VERDICTS: tuple[Verdict, ...] = (
    "ok",
    "loss",
    "reorder",
    "dup",
    "recovered",
    "integrity-fail",
    "malformed",
)

# RTP sequence numbers are 16-bit and wrap; compare with a signed window.
_SEQ_MOD = 1 << 16
SEQUENCE_WINDOW = 4096
_SEQUENCE_MASK = (1 << SEQUENCE_WINDOW) - 1


def _seq_diff(a: int, b: int) -> int:
    """Signed distance ``a - b`` over the 16-bit RTP sequence space."""
    return ((a - b + 0x8000) % _SEQ_MOD) - 0x8000


@dataclass(frozen=True)
class SnoopRecord:
    """One observation, judged. ``stage`` is a pipeline stage name for a
    white-box event or ``"wire"`` for a black-box packet. ``detail`` holds
    the observation's numbers (including ``rel``, seconds since the first
    observation, for rendering)."""

    verdict: Verdict
    stream_id: str
    stage: str
    seq: int
    summary: str
    detail: Body
    now: float


@dataclass
class _StreamView:
    """White-box reconstruction of one stream from its tap events."""

    stages: list[str] = field(default_factory=list)
    recovered: int = 0
    integrity_ok: int = 0
    integrity_bad: int = 0

    def touch(self, stage: str) -> None:
        if stage not in self.stages:
            self.stages.append(stage)
            if len(self.stages) > MAX_STAGES:
                del self.stages[0]


@dataclass
class _WireState:
    """Black-box reconstruction of one stream from packets alone."""

    highest: int | None = None
    # Bit n describes highest-n, so both histories have constant storage.
    seen: int = 0
    missing: int = 0
    loss: int = 0
    timestamp_highest: int | None = None
    timestamps: dict[int, None] = field(default_factory=dict)
    markers: dict[int, None] = field(default_factory=dict)
    frames: int = 0
    marker_frames: int = 0
    packets: int = 0
    bytes: int = 0
    reorders: int = 0
    dups: int = 0
    first_now: float | None = None
    last_now: float | None = None

    def bitrate_bps(self) -> float:
        if self.first_now is None or self.last_now is None:
            return 0.0
        elapsed = self.last_now - self.first_now
        if elapsed <= 0.0:
            return 0.0
        return round(self.bytes * 8 / elapsed, 6)


class MediaSnooper:
    """Stateful passive verifier. No socket, no send method, no clock —
    every input arrives through :meth:`observe_event` / :meth:`observe_packet`.
    """

    def __init__(
        self,
        header_decoder: Callable[[bytes], Body] | None = None,
        *,
        roles: StageRoles | None = None,
    ) -> None:
        self._decode = header_decoder
        self.metrics = MetricsCollector(roles)
        self.counts: dict[Verdict, int] = dict.fromkeys(VERDICTS, 0)
        self._start: float | None = None
        self._views: dict[str, _StreamView] = {}
        self._wire: dict[str, _WireState] = {}

    # -- white-box -----------------------------------------------------------

    def observe_event(self, event: StageEvent, now: float) -> SnoopRecord:
        """Fold one in-process tap; return its judgment (never a unit)."""
        rel = self._rel(now)
        self.metrics.observe(event)
        stream_id = _stream_of(event.detail)
        view = self._views.setdefault(stream_id, _StreamView())
        if len(self._views) > MAX_STREAMS:
            del self._views[next(iter(self._views))]
        view.touch(event.stage)

        verdict: Verdict = "ok"
        if event.kind in (EventKind.DROP, EventKind.LATE):
            verdict = "loss"
        elif event.kind is EventKind.RECOVER:
            verdict = "recovered"
            view.recovered += 1
        elif event.kind is EventKind.INTEGRITY:
            if event.detail.get("ok") is False:
                verdict = "integrity-fail"
                view.integrity_bad += 1
            elif event.detail.get("ok") is True:
                view.integrity_ok += 1
            else:
                verdict = "malformed"

        self.counts[verdict] += 1
        detail: Body = dict(event.detail)
        detail["kind"] = event.kind.value
        detail["pts"] = event.pts
        detail["rel"] = round(rel, 6)
        summary = f"{event.stage} {event.kind.value} seq={event.seq} pts={event.pts}"
        return SnoopRecord(
            verdict=verdict,
            stream_id=stream_id,
            stage=event.stage,
            seq=event.seq,
            summary=summary,
            detail=detail,
            now=now,
        )

    # -- black-box -----------------------------------------------------------

    def observe_packet(self, raw_or_unit: bytes | MediaUnit, now: float) -> SnoopRecord:
        """Judge one wire packet from its bytes alone; return the judgment.

        Accepts raw packet ``bytes`` or a ``PACKET``
        :class:`~yoke.media.types.MediaUnit` (only its ``payload`` is read;
        the unit is never mutated). Requires an injected header decoder.
        """
        if self._decode is None:
            raise RuntimeError("black-box mode needs an injected header_decoder")
        rel = self._rel(now)
        raw = raw_or_unit.payload if isinstance(raw_or_unit, MediaUnit) else raw_or_unit
        size = len(raw)

        header = self._safe_decode(raw)
        seq = _as_int(header.get("seq")) if header is not None else None
        is_packet = not isinstance(raw_or_unit, MediaUnit) or raw_or_unit.kind in (
            UnitKind.PACKET,
            UnitKind.PARITY,
        )
        if header is None or seq is None or not is_packet or not _valid_header(header):
            self.counts["malformed"] += 1
            detail: Body = {"bytes": size, "rel": round(rel, 6)}
            return SnoopRecord(
                verdict="malformed",
                stream_id="",
                stage="wire",
                seq=-1,
                summary=f"malformed packet ({size}B)",
                detail=detail,
                now=now,
            )

        stream_id = _wire_stream_of(header)
        state = self._wire.setdefault(stream_id, _WireState())
        if len(self._wire) > MAX_STREAMS:
            del self._wire[next(iter(self._wire))]
        verdict, unique = self._track_seq(state, seq)
        if unique:
            self._track_frame(state, header)
        state.packets += 1
        state.bytes += size
        if state.first_now is None:
            state.first_now = now
        state.last_now = now if state.last_now is None else max(state.last_now, now)

        self.counts[verdict] += 1
        marker = bool(header.get("marker"))
        ts = _as_int(header.get("timestamp"))
        pdetail: Body = {
            "seq": seq,
            "bytes": size,
            "loss": state.loss,
            "frames": state.frames,
            "bitrate_bps": state.bitrate_bps(),
            "marker": marker,
            "rel": round(rel, 6),
        }
        if ts is not None:
            pdetail["ts"] = ts
        summary = f"wire seq={seq} {verdict} loss={state.loss} frames={state.frames}"
        return SnoopRecord(
            verdict=verdict,
            stream_id=stream_id,
            stage="wire",
            seq=seq,
            summary=summary,
            detail=pdetail,
            now=now,
        )

    def _track_seq(self, state: _WireState, seq: int) -> tuple[Verdict, bool]:
        if state.highest is None:
            state.highest = seq
            state.seen = 1
            return "ok", True
        diff = _seq_diff(seq, state.highest % _SEQ_MOD)
        if diff > 0:
            state.loss += diff - 1
            state.highest += diff
            state.seen = ((state.seen << diff) | 1) & _SEQUENCE_MASK
            gaps = (1 << min(diff, SEQUENCE_WINDOW)) - 2
            state.missing = ((state.missing << diff) | gaps) & _SEQUENCE_MASK
            return ("loss" if diff > 1 else "ok"), True
        age = -diff
        if age >= SEQUENCE_WINDOW:
            # No retained evidence distinguishes late arrivals from duplicates.
            state.reorders += 1
            return "reorder", False
        bit = 1 << age
        if state.seen & bit:
            state.dups += 1
            return "dup", False
        if state.missing & bit:
            state.missing &= ~bit
            state.loss -= 1
        state.seen |= bit
        state.reorders += 1
        return "reorder", True

    @staticmethod
    def _track_frame(state: _WireState, header: Body) -> None:
        ts = _as_int(header.get("timestamp"))
        if ts is not None:
            high = state.timestamp_highest
            if high is not None:
                ts = high + ((ts - high + (1 << 31)) % (1 << 32)) - (1 << 31)
            state.timestamp_highest = ts if high is None else max(high, ts)
            if ts not in state.timestamps:
                state.frames += 1
                state.timestamps[ts] = None
                if len(state.timestamps) > SEQUENCE_WINDOW:
                    del state.timestamps[next(iter(state.timestamps))]
        if bool(header.get("marker")) and (ts is None or ts not in state.markers):
            state.marker_frames += 1
            if ts is not None:
                state.markers[ts] = None
                if len(state.markers) > SEQUENCE_WINDOW:
                    del state.markers[next(iter(state.markers))]

    def _safe_decode(self, raw: bytes) -> Body | None:
        assert self._decode is not None
        try:
            header = self._decode(raw)
        except (ValueError, KeyError, IndexError, TypeError):
            return None
        return header if isinstance(header, dict) else None

    # -- reconstruction ------------------------------------------------------

    def topology(self) -> Body:
        """Reconstructed view: active streams, their traversed stages,
        per-stage counters, end-to-end integrity and loss/recovery/ratio/
        latency — purely from what was observed (white-box + black-box)."""
        snap = self.metrics.snapshot()
        wb_streams: Body = snap["streams"]
        ids = set(self._views) | set(self._wire) | set(wb_streams)
        streams: Body = {}
        for sid in sorted(ids):
            streams[sid] = self._stream_topology(sid, wb_streams.get(sid))
        return {
            "streams": streams,
            "stages": snap["stages"],
            "counts": dict(self.counts),
            "inference": {
                "sequence_window": SEQUENCE_WINDOW,
                "stream_limit_per_mode": MAX_STREAMS,
                "stage_limit": MAX_STAGES,
                "pending_time_limit": MAX_PENDING_TIMES,
                "late_gap_recovery": "retained sequence window only",
                "frame_count": "unique timestamps in retained history",
                "ratio": "mean applied model ratio from codec taps",
                "integrity": "sink taps only; not inferred from wire headers",
                "scope": "retained streams and observed stages only",
            },
        }

    def _stream_topology(self, sid: str, wb: Body | None) -> Body:
        view = self._views.get(sid)
        entry: Body = {
            "stages": list(view.stages) if view is not None else [],
        }
        if wb is not None:
            entry.update(
                {
                    "frames_sourced": wb["frames_sourced"],
                    "frames_ok": wb["frames_ok"],
                    "frames_bad": wb["frames_bad"],
                    "packet_loss": wb["packet_loss"],
                    "fec_recovered": wb["fec_recovered"],
                    "ratio": wb["ratio"],
                    "target_ratio": wb["target_ratio"],
                    "latency": wb["latency"],
                }
            )
        state = self._wire.get(sid)
        if state is not None:
            entry["wire"] = {
                "packets": state.packets,
                "frames": state.frames,
                "marker_frames": state.marker_frames,
                "loss": state.loss,
                "reorders": state.reorders,
                "dups": state.dups,
                "bitrate_bps": state.bitrate_bps(),
            }
        return entry

    # -- helpers -------------------------------------------------------------

    def _rel(self, now: float) -> float:
        if self._start is None:
            self._start = now
        return now - self._start


def _stream_of(detail: Body) -> str:
    value = detail.get("stream")
    return value if isinstance(value, str) and value else DEFAULT_STREAM


def _wire_stream_of(header: Body) -> str:
    ssrc = _as_int(header.get("ssrc"))
    if ssrc is not None:
        return f"{ssrc:08x}"
    return "wire"


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _valid_header(header: Body) -> bool:
    for key, maximum in (
        ("seq", 0xFFFF),
        ("timestamp", 0xFFFFFFFF),
        ("ssrc", 0xFFFFFFFF),
        ("pt", 127),
    ):
        if key not in header:
            continue
        value = _as_int(header[key])
        if value is None or not 0 <= value <= maximum:
            return False
    return "marker" not in header or isinstance(header["marker"], bool)


def render_text(record: SnoopRecord) -> str:
    """One-line human rendering: relative time, stream, stage, verdict,
    key numbers."""
    rel = record.detail.get("rel", record.now)
    rel_f = float(rel) if isinstance(rel, (int, float)) else record.now
    stream = record.stream_id or "?"
    line = (
        f"+{rel_f:8.3f}s {stream:<12} {record.stage:<10} "
        f"[{record.verdict}] {record.summary}"
    )
    return escape_terminal(line)


def render_json(record: SnoopRecord) -> str:
    """Single-line JSON with sorted keys (machine-friendly output)."""
    obj: Body = {
        "verdict": record.verdict,
        "stream": record.stream_id,
        "stage": record.stage,
        "seq": record.seq,
        "summary": record.summary,
        "now": round(record.now, 6),
        "detail": record.detail,
    }
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
