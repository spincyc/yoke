"""Deterministic synthetic media experiments and source-known measurements.

Compose the reference sender, seeded fault link, receiver and passive
observer without command parsing, rendering, clock reads or live I/O.
The result carries every source identity and injected drop, so its complete
integrity accounting is stronger than a passive wire observer's inference.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from yoke.media.assembly import MediaConfig, Receiver, build_receiver, build_sender
from yoke.media.metrics import StageRoles
from yoke.media.simulation import LINK_DELAY, Faults, SimLink
from yoke.media.snoop import MediaSnooper
from yoke.media.types import Body, EventKind, StageEvent
from yoke.media.wire import rtp_header_body


@dataclass
class SimResult:
    """Source-known measurements and event taps from one offline experiment."""

    config: MediaConfig
    faults: Faults
    frames: int
    frames_sourced: int
    frames_ok: int
    frames_bad: int
    frames_lost: int
    duplicated: int
    reordered: int
    packets_sent: int
    packets_dropped: int
    packets_recv: int
    packets_duplicated: int
    data_packets_recv: int
    feedback_reports: int
    feedback_delivered: int
    fec_recovered: int
    ratio: float | None
    target_ratio: float | None
    latency: Body | None
    events: list[StageEvent]
    snapshot: Body
    topology: Body

    @property
    def loss_rate(self) -> float:
        return self.packets_dropped / self.packets_sent if self.packets_sent else 0.0

    @property
    def integrity_ok(self) -> bool:
        """The probe gate: every sourced frame arrived and verified."""
        return (
            self.frames_bad == 0
            and self.frames_lost == 0
            and self.frames_ok == self.frames_sourced
            and self.frames_sourced > 0
        )


def drive(config: MediaConfig, faults: Faults, frames: int) -> SimResult:
    """Push ``frames`` frames through the full offline datapath and fold
    every tap into a :class:`MediaSnooper`. Pure and deterministic: the
    only entropy is ``config.seed`` (source payloads and the link share it),
    so the same arguments always yield the same :class:`SimResult`."""
    if type(frames) is not int or frames < 0:
        raise ValueError("frames must be a nonnegative integer")
    sender = build_sender(config)
    receiver = build_receiver(config)
    link = SimLink(sender.pipeline, receiver.pipeline, seed=config.seed, faults=faults)
    snooper = MediaSnooper(
        rtp_header_body, roles=StageRoles(wire_out="wire", wire_in="wire")
    )

    interval = 1.0 / config.fps
    now = 0.0
    events: list[StageEvent] = []
    for _ in range(frames):
        fo = sender.source.frame(now)
        events.extend(fo.events)
        for frame_unit in fo.emit:
            out = sender.pipeline.push(frame_unit, now)
            events.extend(out.events)
            link.send(out.emitted)
        link.advance(now + interval)
        now += interval
    # Drain the link and flush the jitter buffer's remaining playout window.
    final_sender = sender.pipeline.finish(now)
    events.extend(final_sender.events)
    link.send(final_sender.emitted)
    link.drain()
    link.finish_receiver()
    events.extend(link.events)

    for event in events:
        snooper.observe_event(event, event.now)
    for unit, seen_at in link.delivered_packets:
        snooper.observe_packet(unit, seen_at)

    return _assemble(
        config,
        faults,
        frames,
        receiver,
        link,
        snooper,
        events,
        sender.rate_state.target,
    )


def _assemble(
    config: MediaConfig,
    faults: Faults,
    frames: int,
    receiver: Receiver,
    link: SimLink,
    snooper: MediaSnooper,
    events: list[StageEvent],
    final_target: float,
) -> SimResult:
    sink = receiver.sink.metrics()
    snapshot = snooper.metrics.snapshot()
    stream = _primary_stream(snapshot)
    sourced = {
        (event.seq, event.pts)
        for event in events
        if event.stage == "source" and event.kind is EventKind.OUT
    }
    failed = {
        (event.seq, event.pts)
        for event in events
        if event.stage == "sink"
        and event.kind is EventKind.INTEGRITY
        and event.detail.get("ok") is False
    } & sourced
    passed = {
        (event.seq, event.pts)
        for event in events
        if event.stage == "sink"
        and event.kind is EventKind.INTEGRITY
        and event.detail.get("ok") is True
    } & sourced - failed
    missing = sourced - passed - failed
    data_recv = sum(
        event.stage == "depacketize" and event.kind is EventKind.IN for event in events
    )
    network: Body = {
        "packets_sent": link.sent,
        "packets_recv": len(link.delivered_packets),
        "packet_loss": len(link.dropped),
        "packet_loss_rate": round(len(link.dropped) / link.sent, 6)
        if link.sent
        else 0.0,
        "packets_duplicated": link.duplicates,
        "packet_loss_source": "simulator-injected-drops",
        "data_packets_recv": data_recv,
    }
    counts: Body = {
        "frames_sourced": len(sourced),
        "frames_ok": len(passed),
        "frames_bad": len(failed),
        "frames_lost": len(missing),
    }
    # This simulator knows each source identity and each injected drop;
    # unlike a passive observer it can account for edge loss and copies.
    stream.update(network)
    stream.update(counts)
    stream["target_ratio"] = final_target
    recovered = _as_int(stream.get("fec_recovered"))
    stream["fec_recovery_rate"] = round(recovered / data_recv, 6) if data_recv else 0.0
    stream["fec_recovery_rate_scope"] = "recovered/post-fec-data"
    totals = snapshot.get("totals")
    if isinstance(totals, dict):
        totals.update(network)
        totals.update(counts)
    snapshot["network"] = network
    snapshot["feedback"] = {
        "reports": len(link.feedback),
        "delivered": len(link.feedback_delivered),
        "return_delay": LINK_DELAY,
        "transport": "reliable-model",
    }
    return SimResult(
        config=config,
        faults=faults,
        frames=frames,
        frames_sourced=len(sourced),
        frames_ok=len(passed),
        frames_bad=len(failed),
        frames_lost=len(missing),
        duplicated=_as_int(sink.get("duplicated")),
        reordered=_as_int(sink.get("reordered")),
        packets_sent=link.sent,
        packets_dropped=len(link.dropped),
        packets_recv=len(link.delivered_packets),
        packets_duplicated=link.duplicates,
        data_packets_recv=data_recv,
        feedback_reports=len(link.feedback),
        feedback_delivered=len(link.feedback_delivered),
        fec_recovered=_as_int(stream.get("fec_recovered")),
        ratio=_as_opt_float(stream.get("ratio")),
        target_ratio=_as_opt_float(stream.get("target_ratio")),
        latency=stream.get("latency")
        if isinstance(stream.get("latency"), dict)
        else None,
        events=events,
        snapshot=snapshot,
        topology=_simulation_topology(snooper, snapshot),
    )


def _simulation_topology(snooper: MediaSnooper, snapshot: Body) -> Body:
    topology = snooper.topology()
    # Preserve the independent black-box wire view, while making the
    # white-box simulation view agree with the source-known summary.
    observed = topology.get("streams")
    measured = snapshot.get("streams")
    if isinstance(observed, dict) and isinstance(measured, dict):
        for stream_id, metrics in measured.items():
            entry = observed.get(stream_id)
            if isinstance(entry, dict) and isinstance(metrics, dict):
                entry.update(metrics)
                entry["measurement_scope"] = "source-known-reference-simulation"
    topology["simulation"] = {
        "network": snapshot.get("network"),
        "feedback": snapshot.get("feedback"),
        "source_accounting": "all sourced frame identities",
    }
    return topology


def _primary_stream(snapshot: Body) -> Body:
    streams = snapshot.get("streams")
    if isinstance(streams, dict) and streams:
        first = next(iter(sorted(streams)))
        value = streams[first]
        if isinstance(value, dict):
            return value
    return {}


def _as_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _as_opt_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        result = float(value)
        return result if math.isfinite(result) else None
    return None
