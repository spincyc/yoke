"""``yoke-media`` — debug CLI for the synthetic reference media pipeline.

This is the media-plane twin of :mod:`yoke.cli`: every decision lives in a
pure, string-returning helper driven by explicit data, and ``main`` is the
only place that reads a clock, opens a socket, or prints. That split is
what makes the debug tools *first-class* — the same code paths the
operator runs are exercised offline, deterministically, with no hardware
(MEDIA.md, "Verification / debugging tools (first-class)").

Subcommands
-----------
``simulate``  Run source → sender → seeded link → receiver → sink over an
              offline fault script and report survival (frames ok/bad/lost,
              FEC recoveries, effective ratio). The primary offline tool.
``probe``     Simulate over a *clean* link by default and gate on
              end-to-end integrity: nonzero exit if any sourced frame did
              not arrive intact. A pass/fail check for CI and bring-up.
``trace``     Dump every :class:`~yoke.media.types.StageEvent` of a few
              frames as one JSON line each — the journey through every stage.
``snoop``     Passive verifier. Offline: reconstruct per-stage + wire health
              from a simulated stream via :meth:`MediaSnooper.topology`.
              Live (``--live``, ``main`` only): join the media group and
              render packets passively, transmitting nothing.
``run``       The real two-host/loopback runner: ``--role sender`` /
              ``--role receiver`` over UDP (multicast or ``--unicast``).

All the sim/rendering logic is sans-I/O; only ``run`` /
``snoop --live`` touch sockets, and only inside :func:`main`.
"""

from __future__ import annotations

import argparse
import json
import math
from collections.abc import Callable
from dataclasses import dataclass

from yoke.media.assembly import (
    MAX_PATH_MTU,
    MIN_PATH_MTU,
    MediaConfig,
    Receiver,
    build_receiver,
    build_sender,
)
from yoke.media.metrics import StageRoles
from yoke.media.simulation import LINK_DELAY, Faults, SimLink
from yoke.media.snoop import MediaSnooper
from yoke.media.sockets import (
    MulticastOptions,
    ipv4_address,
    open_receiver_socket,
    open_sender_socket,
    valid_port,
)
from yoke.media.types import (
    Body,
    EventKind,
    StageEvent,
)
from yoke.media.wire import rtp_header_body

# Media plane defaults (distinct from the control group in core.types).
DEFAULT_MEDIA_GROUP = "239.255.42.42"
DEFAULT_MEDIA_PORT = 5004
# How many frames the offline tools push unless overridden.
SIM_FRAMES = 30
TRACE_FRAMES = 3
# Real-runner defaults for ``run`` when neither --frames nor --duration set.
DEFAULT_RUN_FRAMES = 300
DEFAULT_RUN_DURATION = 10.0

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2

ENCODINGS = ("raw", "jpegxs", "vc2")
FECS = ("none", "xor", "dup")
PATTERNS = ("counter", "bars", "noise")

# A stable pipeline order used only to sort trace lines into a readable
# journey; unknown stages sort last but keep their relative time order.
_STAGE_ORDER = (
    "source",
    "ratecontrol",
    "codec",
    "packetize",
    "xor",
    "dup",
    "fec",
    "jitter",
    "fec-decode",
    "depacketize",
    "codec-decode",
    "sink",
)


# -- config building ---------------------------------------------------------


def build_config(args: argparse.Namespace) -> MediaConfig:
    """Build a :class:`MediaConfig` from parsed global options. Raises
    ``ValueError`` for an unknown encoding/fec (argparse ``choices`` catches
    these first; this is the belt-and-suspenders guard)."""
    for name in ("width", "height", "fps"):
        if int(getattr(args, name)) < 1:
            raise ValueError(f"{name} must be positive")
    if not MIN_PATH_MTU <= int(args.mtu) <= MAX_PATH_MTU:
        raise ValueError(f"path mtu must be in [{MIN_PATH_MTU}, {MAX_PATH_MTU}]")
    if hasattr(args, "frames") and args.frames is not None and int(args.frames) < 1:
        raise ValueError("frames must be positive")
    if hasattr(args, "duration") and args.duration is not None:
        duration = float(args.duration)
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("duration must be finite and positive")
    valid_port(int(args.port))
    MulticastOptions(
        str(args.group), str(args.interface), int(args.ttl), bool(args.loopback)
    )
    return MediaConfig(
        width=int(args.width),
        height=int(args.height),
        fps=int(args.fps),
        encoding=str(args.encoding),
        pattern=str(args.pattern),
        mtu=int(args.mtu),
        fec=str(args.fec),
        seed=int(args.seed),
    )


def build_faults(args: argparse.Namespace) -> Faults:
    return Faults(
        drop=float(args.drop),
        dup=float(args.dup),
        reorder=float(args.reorder),
        jitter=float(args.jitter),
        corrupt=float(args.corrupt),
    )


def _roles_for(config: MediaConfig) -> StageRoles:
    """Matched wire taps surround the link, before receiver FEC and jitter."""
    return StageRoles(wire_out="wire", wire_in="wire")


# -- offline drive core ------------------------------------------------------


@dataclass
class SimResult:
    """Everything the renderers need from one offline drive."""

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
    snooper = MediaSnooper(rtp_header_body, roles=_roles_for(config))

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


# -- rendering (pure, string-returning) --------------------------------------


def _fmt_ratio(value: float | None) -> str:
    return f"{value:.2f}:1" if value is not None else "n/a"


def _fmt_latency(latency: Body | None) -> str:
    if not latency:
        return "n/a"
    mean = latency.get("mean")
    peak = latency.get("max")
    if isinstance(mean, (int, float)) and isinstance(peak, (int, float)):
        return f"mean={mean * 1e3:.3f}ms max={peak * 1e3:.3f}ms"
    return "n/a"


def _config_line(config: MediaConfig, frames: int) -> str:
    return (
        f"{config.encoding}/{config.fec} {config.width}x{config.height}"
        f"@{config.fps} mtu={config.mtu} seed={config.seed} frames={frames}"
    )


def _fault_line(faults: Faults) -> str:
    if faults.is_clean():
        return "faults:   clean link"
    return (
        f"faults:   drop={faults.drop:g} dup={faults.dup:g} "
        f"reorder={faults.reorder:g} jitter={faults.jitter:g} "
        f"corrupt={faults.corrupt:g}"
    )


def _health_lines(r: SimResult) -> list[str]:
    return [
        f"frames:   sourced={r.frames_sourced} ok={r.frames_ok} "
        f"bad={r.frames_bad} lost={r.frames_lost} "
        f"dup={r.duplicated} reordered={r.reordered}",
        f"packets:  sent={r.packets_sent} recv={r.packets_recv} "
        f"dropped={r.packets_dropped} network-dup={r.packets_duplicated} "
        f"loss={r.loss_rate * 100:.2f}%",
        f"fec:      recovered={r.fec_recovered}",
        f"delivery: post-fec-data={r.data_packets_recv}",
        f"feedback: reports={r.feedback_reports} delivered={r.feedback_delivered} "
        "reliable-model",
        f"ratio:    model-effective={_fmt_ratio(r.ratio)} "
        f"target={_fmt_ratio(r.target_ratio)}",
        f"latency:  {_fmt_latency(r.latency)}",
    ]


def render_simulate_text(r: SimResult) -> str:
    verdict = "OK" if r.integrity_ok else "DEGRADED"
    lines = [
        f"simulate  {_config_line(r.config, r.frames)}",
        _fault_line(r.faults),
        *_health_lines(r),
        f"verdict:  {verdict}",
    ]
    return "\n".join(lines)


def summary_dict(r: SimResult) -> Body:
    """The compact machine summary embedded in ``--json`` output."""
    return {
        "measurement_scope": "synthetic-reference",
        "frames_sourced": r.frames_sourced,
        "frames_ok": r.frames_ok,
        "frames_bad": r.frames_bad,
        "frames_lost": r.frames_lost,
        "packets_sent": r.packets_sent,
        "packets_recv": r.packets_recv,
        "packets_dropped": r.packets_dropped,
        "packets_duplicated": r.packets_duplicated,
        "data_packets_recv": r.data_packets_recv,
        "feedback_reports": r.feedback_reports,
        "feedback_delivered": r.feedback_delivered,
        "packet_loss_rate": round(r.loss_rate, 6),
        "fec_recovered": r.fec_recovered,
        "ratio": r.ratio,
        "target_ratio": r.target_ratio,
        "latency": r.latency,
        "integrity_ok": r.integrity_ok,
    }


def render_simulate_json(r: SimResult) -> str:
    """Emit the metrics snapshot (``stages``/``streams``/``totals``) plus a
    ``summary`` block, on one sorted line."""
    obj: Body = dict(r.snapshot)
    obj["summary"] = summary_dict(r)
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def render_probe_text(r: SimResult) -> str:
    verdict = "PASS" if r.integrity_ok else "FAIL"
    reason = ""
    if not r.integrity_ok:
        if r.frames_lost:
            reason = f" ({r.frames_lost} frame(s) never arrived)"
        elif r.frames_bad:
            reason = f" ({r.frames_bad} frame(s) failed integrity)"
        elif r.frames_sourced == 0:
            reason = " (no frames sourced)"
    lines = [
        f"probe     {_config_line(r.config, r.frames)}",
        _fault_line(r.faults),
        *_health_lines(r),
        f"integrity: {verdict}{reason}",
    ]
    return "\n".join(lines)


def render_event_json(event: StageEvent) -> str:
    """One :class:`StageEvent` as a single sorted-key JSON line."""
    obj: Body = {
        "stage": event.stage,
        "kind": event.kind.value,
        "seq": event.seq,
        "pts": event.pts,
        "now": round(event.now, 6),
        "detail": event.detail,
    }
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _trace_key(event: StageEvent) -> tuple[float, int]:
    try:
        rank = _STAGE_ORDER.index(event.stage)
    except ValueError:
        rank = len(_STAGE_ORDER)
    return event.now, rank


def render_trace(r: SimResult) -> str:
    """Every event of the drive, one JSON line each, ordered as a journey
    (by time, then pipeline position)."""
    ordered = sorted(r.events, key=_trace_key)
    return "\n".join(render_event_json(event) for event in ordered)


def render_topology(topology: Body) -> str:
    return json.dumps(topology, sort_keys=True, separators=(",", ":"))


# -- run plan (offline dry-run) ----------------------------------------------


@dataclass(frozen=True)
class RunPlan:
    role: str
    transport: str
    endpoint: tuple[str, int]
    config: MediaConfig
    frames: int | None
    duration: float | None
    snoop: bool
    interface: str = "0.0.0.0"
    ttl: int = 1
    loopback: bool = True
    feedback_dest: tuple[str, int] | None = None

    @property
    def multicast(self) -> MulticastOptions | None:
        if self.transport == "unicast":
            return None
        return MulticastOptions(
            self.endpoint[0], self.interface, self.ttl, self.loopback
        )


def resolve_run_plan(
    args: argparse.Namespace, config: MediaConfig
) -> tuple[RunPlan | None, str | None]:
    """Turn parsed ``run`` options into a validated :class:`RunPlan`, or an
    error message (for a 2 exit). No sockets, no clock."""
    role = str(args.role)
    _validate_live_config(config)
    transport = "unicast" if args.unicast else "multicast"
    group = str(args.group)
    port = int(args.port)
    peer = args.peer
    if args.frames is not None and args.duration is not None:
        return None, "choose --frames or --duration, not both"
    if role == "receiver" and args.frames is not None:
        return None, "--frames applies to the sender; use --duration for a receiver"
    if role == "receiver" and peer:
        return None, "--peer applies to the unicast sender only"
    if role == "sender" and args.snoop:
        return None, "--snoop applies to the receiver; use snoop --live independently"
    interface = str(args.interface)
    ttl = int(args.ttl)
    loopback = bool(args.loopback)
    feedback_host, feedback_port = args.feedback_host, args.feedback_port
    if (feedback_host is None) != (feedback_port is None):
        return None, "--feedback-host and --feedback-port must be supplied together"
    feedback_dest = None
    if feedback_host is not None:
        ipv4_address(str(feedback_host))
        if feedback_host == "0.0.0.0":
            return None, "feedback destination must be a unicast host"
        feedback_dest = (str(feedback_host), valid_port(int(feedback_port)))

    if role == "sender":
        if feedback_dest is not None:
            return None, "feedback options apply to the receiver only"
        if transport == "unicast" and not peer:
            return None, "run --role sender --unicast requires --peer HOST"
        if transport == "multicast" and peer:
            return None, "--peer requires --unicast; multicast sends to --group"
        dest = (str(peer) if transport == "unicast" else group, port)
        frames, duration = _sender_limits(args, config)
        return RunPlan(
            role,
            transport,
            dest,
            config,
            frames,
            duration,
            False,
            interface,
            ttl,
            loopback,
        ), None

    # receiver
    bind_host = group if transport == "multicast" else "0.0.0.0"
    duration = (
        float(args.duration) if args.duration is not None else DEFAULT_RUN_DURATION
    )
    plan = RunPlan(
        role="receiver",
        transport=transport,
        endpoint=(bind_host, port),
        config=config,
        frames=None,
        duration=duration,
        snoop=bool(args.snoop),
        interface=interface,
        ttl=ttl,
        loopback=loopback,
        feedback_dest=feedback_dest,
    )
    return plan, None


def _validate_live_config(config: MediaConfig) -> None:
    if config.fec == "xor":
        raise ValueError("XOR FEC is offline-only; use --fec none or dup for live UDP")


def _sender_limits(
    args: argparse.Namespace, config: MediaConfig
) -> tuple[int, float | None]:
    if args.frames is not None:
        return int(args.frames), None
    if args.duration is not None:
        duration = float(args.duration)
        count = duration * config.fps
        if not math.isfinite(count):
            raise ValueError("duration times fps must be finite")
        return max(1, int(count)), duration
    return DEFAULT_RUN_FRAMES, None


def render_run_plan(plan: RunPlan) -> str:
    host, port = plan.endpoint
    c = plan.config
    lines = [
        f"plan:      {plan.role}",
        f"transport: {plan.transport}",
    ]
    if plan.role == "sender":
        lines.append(f"dest:      {host}:{port}")
    else:
        lines.append(f"bind:      {host}:{port}")
    if plan.transport == "multicast":
        lines.append(
            f"multicast: interface={plan.interface} ttl={plan.ttl} "
            f"loopback={'on' if plan.loopback else 'off'}"
        )
    lines.append(
        f"media:     {c.encoding}/{c.fec} {c.width}x{c.height}@{c.fps} mtu={c.mtu}"
    )
    if plan.role == "sender":
        if plan.frames is not None:
            extra = f" ({plan.duration:g}s)" if plan.duration is not None else ""
            lines.append(f"limit:     frames={plan.frames}{extra}")
    else:
        lines.append(f"limit:     duration={plan.duration:g}s")
        lines.append(f"snoop:     {'on' if plan.snoop else 'off'}")
        feedback = (
            f"{plan.feedback_dest[0]}:{plan.feedback_dest[1]}"
            if plan.feedback_dest is not None
            else "off"
        )
        lines.append(f"feedback:  {feedback}")
    return "\n".join(lines)


# -- argument parsing --------------------------------------------------------


def _config_parent() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--group", default=DEFAULT_MEDIA_GROUP, help="media multicast group")
    p.add_argument("--port", type=int, default=DEFAULT_MEDIA_PORT, help="media port")
    p.add_argument(
        "--interface", default="0.0.0.0", help="local IPv4 multicast interface"
    )
    p.add_argument("--ttl", type=int, default=1, help="multicast hop limit (0..255)")
    p.add_argument(
        "--loopback",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="deliver multicast to listeners on this host",
    )
    p.add_argument("--encoding", choices=ENCODINGS, default="raw", help="codec model")
    p.add_argument("--fec", choices=FECS, default="none", help="FEC scheme")
    p.add_argument("--fps", type=int, default=60, help="frames per second")
    p.add_argument("--width", type=int, default=1920, help="frame width")
    p.add_argument("--height", type=int, default=1080, help="frame height")
    p.add_argument("--mtu", type=int, default=9000, help="path MTU for packetization")
    p.add_argument(
        "--pattern", choices=PATTERNS, default="counter", help="source pattern"
    )
    p.add_argument("--seed", type=int, default=0, help="deterministic sim seed")
    return p


def _fault_parent() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument(
        "--drop", type=float, default=0.0, help="per-packet drop probability"
    )
    p.add_argument("--dup", type=float, default=0.0, help="per-packet dup probability")
    p.add_argument("--reorder", type=float, default=0.0, help="reorder probability")
    p.add_argument(
        "--jitter", type=float, default=0.0, help="delay jitter magnitude (s)"
    )
    p.add_argument(
        "--corrupt", type=float, default=0.0, help="bit-corruption probability"
    )
    return p


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="yoke-media", description="synthetic reference media debug tools"
    )
    config_parent = _config_parent()
    fault_parent = _fault_parent()
    sub = parser.add_subparsers(dest="command", required=True)

    sim = sub.add_parser(
        "simulate",
        parents=[config_parent, fault_parent],
        help="run the pipeline over a seeded offline link and report survival",
    )
    sim.add_argument("--frames", type=int, default=SIM_FRAMES, help="frames to push")
    sim.add_argument("--json", action="store_true", help="emit the metrics snapshot")

    probe = sub.add_parser(
        "probe",
        parents=[config_parent, fault_parent],
        help="integrity gate: nonzero exit if any frame did not arrive intact",
    )
    probe.add_argument("--frames", type=int, default=SIM_FRAMES, help="frames to push")
    probe.add_argument("--json", action="store_true", help="emit the metrics snapshot")

    trace = sub.add_parser(
        "trace",
        parents=[config_parent],
        help="dump each StageEvent as one JSON line (unit journey)",
    )
    trace.add_argument(
        "--frames", type=int, default=TRACE_FRAMES, help="frames to trace"
    )

    snoop = sub.add_parser(
        "snoop",
        parents=[config_parent, fault_parent],
        help="passively reconstruct per-stage + wire health (topology)",
    )
    snoop.add_argument(
        "--frames", type=int, default=SIM_FRAMES, help="frames to observe"
    )
    snoop.add_argument(
        "--live", action="store_true", help="join the real media group (main only)"
    )
    snoop.add_argument(
        "--duration", type=float, default=None, help="live observation seconds"
    )

    run = sub.add_parser(
        "run",
        parents=[config_parent],
        help="the real sender/receiver runner over UDP",
    )
    run.add_argument("--role", choices=("sender", "receiver"), required=True)
    run.add_argument("--peer", default=None, help="unicast sender destination host")
    run.add_argument("--unicast", action="store_true", help="plain UDP, not multicast")
    run.add_argument("--frames", type=int, default=None, help="sender: frames to send")
    run.add_argument("--duration", type=float, default=None, help="run duration (s)")
    run.add_argument("--snoop", action="store_true", help="receiver: dump topology")
    run.add_argument(
        "--feedback-host", help="designated receiver: unicast feedback IPv4"
    )
    run.add_argument(
        "--feedback-port", type=int, help="designated receiver: feedback UDP port"
    )
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="build and print the plan; open no socket",
    )
    return parser


# -- offline command handlers (pure enough to unit-test) ---------------------


def cmd_simulate(args: argparse.Namespace, emit: Callable[[str], None]) -> int:
    result = drive(build_config(args), build_faults(args), int(args.frames))
    emit(render_simulate_json(result) if args.json else render_simulate_text(result))
    return EXIT_OK


def cmd_probe(args: argparse.Namespace, emit: Callable[[str], None]) -> int:
    result = drive(build_config(args), build_faults(args), int(args.frames))
    emit(render_simulate_json(result) if args.json else render_probe_text(result))
    return EXIT_OK if result.integrity_ok else EXIT_FAIL


def cmd_trace(args: argparse.Namespace, emit: Callable[[str], None]) -> int:
    result = drive(build_config(args), Faults(), int(args.frames))
    emit(render_trace(result))
    return EXIT_OK


def cmd_snoop_offline(args: argparse.Namespace, emit: Callable[[str], None]) -> int:
    result = drive(build_config(args), build_faults(args), int(args.frames))
    emit(render_topology(result.topology))
    return EXIT_OK


# -- main (the only place with I/O) ------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command: str = args.command

    def out(line: str) -> None:
        print(line, flush=True)

    def err(line: str) -> None:
        import sys

        print(line, file=sys.stderr)

    try:
        if command == "simulate":
            return cmd_simulate(args, out)
        if command == "probe":
            return cmd_probe(args, out)
        if command == "trace":
            return cmd_trace(args, out)
        if command == "snoop":
            if not args.live:
                return cmd_snoop_offline(args, out)
            return _run_live_snoop(args, out, err)
        if command == "run":
            return _run_command(args, out, err)
    except ValueError as exc:  # bad config the choices did not catch
        err(str(exc))
        return EXIT_USAGE
    except OSError as exc:
        err(f"media socket error: {exc}")
        return EXIT_FAIL
    except KeyboardInterrupt:
        return EXIT_OK
    raise AssertionError(f"unhandled command: {command!r}")


def _run_command(
    args: argparse.Namespace,
    out: Callable[[str], None],
    err: Callable[[str], None],
) -> int:
    config = build_config(args)
    plan, problem = resolve_run_plan(args, config)
    if problem is not None or plan is None:
        err(problem or "invalid run options")
        return EXIT_USAGE
    if args.dry_run:
        out(render_run_plan(plan))
        return EXIT_OK
    return _run_live(plan, config, out, err)


def _run_live(
    plan: RunPlan,
    config: MediaConfig,
    out: Callable[[str], None],
    err: Callable[[str], None],
) -> int:
    """Open real sockets and run the asyncio datapath. Not exercised by the
    offline tests (the loopback lane covers it); kept as thin as possible."""
    if plan.role == "sender":
        return _run_live_sender(plan, config, out)
    return _run_live_receiver(plan, config, out)


def _run_live_sender(
    plan: RunPlan, config: MediaConfig, out: Callable[[str], None]
) -> int:
    import asyncio

    from yoke.media.runner import SenderRunner

    host, port = plan.endpoint
    runner = SenderRunner(build_sender(config), dest=(host, port))
    sock = open_sender_socket(multicast=plan.multicast)
    frames = plan.frames if plan.frames is not None else DEFAULT_RUN_FRAMES
    try:
        stats = asyncio.run(runner.run(sock, frames=frames))
    finally:
        sock.close()
    out(f"sent frames={stats.frames} packets={stats.packets}")
    return EXIT_OK


def _run_live_receiver(
    plan: RunPlan, config: MediaConfig, out: Callable[[str], None]
) -> int:
    import asyncio

    from yoke.media.runner import ReceiverRunner

    _host, port = plan.endpoint
    snooper = (
        MediaSnooper(rtp_header_body, roles=_roles_for(config)) if plan.snoop else None
    )
    runner = ReceiverRunner(
        build_receiver(config), snooper=snooper, feedback_dest=plan.feedback_dest
    )
    sock = open_receiver_socket(port, multicast=plan.multicast)
    duration = plan.duration if plan.duration is not None else DEFAULT_RUN_DURATION
    try:
        metrics = asyncio.run(runner.run(sock, duration=duration))
    finally:
        sock.close()
    out(json.dumps(metrics, sort_keys=True, separators=(",", ":")))
    if snooper is not None:
        out(render_topology(snooper.topology()))
    return EXIT_OK


def _run_live_snoop(
    args: argparse.Namespace,
    out: Callable[[str], None],
    err: Callable[[str], None],
) -> int:
    """Join the real group and observe through the tested receive-only shell."""

    import asyncio

    from yoke.media.runner import PassiveRunner
    from yoke.media.snoop import render_text

    config = build_config(args)
    _validate_live_config(config)
    snooper = MediaSnooper(rtp_header_body, roles=_roles_for(config))
    runner = PassiveRunner(snooper, on_record=lambda record: out(render_text(record)))
    sock = open_receiver_socket(
        int(args.port),
        multicast=MulticastOptions(
            str(args.group), str(args.interface), int(args.ttl), bool(args.loopback)
        ),
    )
    try:
        asyncio.run(runner.run(sock, duration=args.duration))
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
    err(render_topology(runner.snapshot()))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
