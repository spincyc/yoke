"""``yoke media`` — debug/observability CLI for the media pipeline.

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

All the sim/rendering logic is sans-I/O; only ``run --live`` /
``snoop --live`` touch sockets, and only inside :func:`main`.
"""

from __future__ import annotations

import argparse
import heapq
import itertools
import json
import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from random import Random

from yoke.media.assembly import (
    MediaConfig,
    Receiver,
    build_receiver,
    build_sender,
)
from yoke.media.metrics import StageRoles
from yoke.media.pipeline import Pipeline
from yoke.media.snoop import MediaSnooper
from yoke.media.types import (
    Body,
    MediaUnit,
    NetworkReport,
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
# Base one-way link delay for the offline sim (seconds).
LINK_DELAY = 0.0005

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


# -- fault script + offline link --------------------------------------------


@dataclass(frozen=True)
class Faults:
    """Per-unit fault probabilities for the offline sim (mirrors the media
    simnet's policy). Probabilities are in ``[0, 1]``; ``jitter`` is the
    magnitude (seconds) of uniform delay noise."""

    drop: float = 0.0
    dup: float = 0.0
    reorder: float = 0.0
    jitter: float = 0.0
    corrupt: float = 0.0
    delay: float = LINK_DELAY
    reorder_spike: float = 0.01

    def is_clean(self) -> bool:
        return not (
            self.drop or self.dup or self.reorder or self.jitter or self.corrupt
        )


@dataclass(order=True)
class _Scheduled:
    at: float
    order: int
    unit: MediaUnit = field(compare=False)


class SimLink:
    """A deterministic, seeded, one-way lossy link between a sender and a
    receiver :class:`~yoke.media.pipeline.Pipeline`, for offline debugging.

    It is the in-package analogue of ``tests/media_simnet.MediaLink`` (the
    CLI is shippable package code and must not import the test tree): feed
    it the sender's emitted units with :meth:`send`, then :meth:`advance`
    the virtual clock to drive the receiver. Every delivered packet is
    retained (:attr:`delivered_packets`) so a black-box snooper can verify
    the *wire* the receiver actually saw.
    """

    def __init__(
        self,
        sender: Pipeline,
        receiver: Pipeline,
        *,
        seed: int,
        faults: Faults,
    ) -> None:
        self._sender = sender
        self._receiver = receiver
        self._rng = Random(seed)
        self._faults = faults
        self._queue: list[_Scheduled] = []
        self._counter = itertools.count()
        self.now = 0.0
        self.delivered_packets: list[tuple[MediaUnit, float]] = []
        self.dropped: list[MediaUnit] = []
        self.events: list[StageEvent] = []
        self.feedback: list[NetworkReport] = []
        self.sent = 0

    def send(self, units: list[MediaUnit]) -> None:
        for unit in units:
            self.sent += 1
            self._schedule(unit)

    def _schedule(self, unit: MediaUnit) -> None:
        f = self._faults
        if self._rng.random() < f.drop:
            self.dropped.append(unit)
            return
        copies = 2 if self._rng.random() < f.dup else 1
        for _ in range(copies):
            delay = f.delay
            if f.jitter:
                delay += self._rng.uniform(-f.jitter, f.jitter)
            if self._rng.random() < f.reorder:
                delay += f.reorder_spike
            out = self._corrupt(unit) if self._rng.random() < f.corrupt else unit
            heapq.heappush(
                self._queue,
                _Scheduled(self.now + max(delay, 0.0), next(self._counter), out),
            )

    def _corrupt(self, unit: MediaUnit) -> MediaUnit:
        if not unit.payload:
            return unit.with_payload(b"\x00", corrupted=True)
        data = bytearray(unit.payload)
        index = self._rng.randrange(len(data))
        data[index] ^= 1 << self._rng.randrange(8)
        return unit.with_payload(bytes(data), corrupted=True)

    def advance(self, until: float, *, tick_interval: float = 0.005) -> None:
        next_tick = self.now
        while True:
            due = self._queue[0].at if self._queue else None
            candidates = [t for t in (due, next_tick) if t is not None and t <= until]
            if not candidates:
                break
            step = min(candidates)
            self.now = step
            if due is not None and step == due:
                item = heapq.heappop(self._queue)
                self.delivered_packets.append((item.unit, self.now))
                out = self._receiver.push(item.unit, self.now)
                self.events.extend(out.events)
                self.feedback.extend(out.feedback)
            if step == next_tick:
                rout = self._receiver.tick(self.now)
                self.events.extend(rout.events)
                self.feedback.extend(rout.feedback)
                next_tick = step + tick_interval
        self.now = until

    def quiescent(self) -> bool:
        return not self._queue


# -- config building ---------------------------------------------------------


def build_config(args: argparse.Namespace) -> MediaConfig:
    """Build a :class:`MediaConfig` from parsed global options. Raises
    ``ValueError`` for an unknown encoding/fec (argparse ``choices`` catches
    these first; this is the belt-and-suspenders guard)."""
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
    """End-to-end measurement points for this config. The wire is launched
    at the last sender stage (``fec`` when present, else ``packetize``) and
    re-enters visibly at ``depacketize`` (the first receiver stage that taps
    every packet it accepts)."""
    return StageRoles(
        wire_out="fec" if config.fec != "none" else "packetize",
        wire_in="depacketize",
    )


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
    link.advance(now + config.playout_delay + 0.05)
    events.extend(link.events)

    for event in events:
        snooper.observe_event(event, event.now)
    for unit, seen_at in link.delivered_packets:
        snooper.observe_packet(unit, seen_at)

    return _assemble(config, faults, frames, receiver, link, snooper, events)


def _assemble(
    config: MediaConfig,
    faults: Faults,
    frames: int,
    receiver: Receiver,
    link: SimLink,
    snooper: MediaSnooper,
    events: list[StageEvent],
) -> SimResult:
    sink = receiver.sink.metrics()
    snapshot = snooper.metrics.snapshot()
    stream = _primary_stream(snapshot)
    return SimResult(
        config=config,
        faults=faults,
        frames=frames,
        frames_sourced=frames,
        frames_ok=_as_int(sink.get("frames_ok")),
        frames_bad=_as_int(sink.get("frames_bad")),
        frames_lost=_as_int(sink.get("lost")),
        duplicated=_as_int(sink.get("duplicated")),
        reordered=_as_int(sink.get("reordered")),
        packets_sent=link.sent,
        packets_dropped=len(link.dropped),
        packets_recv=_as_int(stream.get("packets_recv")),
        fec_recovered=_as_int(stream.get("fec_recovered")),
        ratio=_as_opt_float(stream.get("ratio")),
        target_ratio=_as_opt_float(stream.get("target_ratio")),
        latency=stream.get("latency")
        if isinstance(stream.get("latency"), dict)
        else None,
        events=events,
        snapshot=snapshot,
        topology=snooper.topology(),
    )


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
    return float(value) if isinstance(value, (int, float)) else None


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
        f"dropped={r.packets_dropped} loss={r.loss_rate * 100:.2f}%",
        f"fec:      recovered={r.fec_recovered}",
        f"ratio:    effective={_fmt_ratio(r.ratio)} "
        f"target={_fmt_ratio(r.target_ratio)}",
        f"latency:  {_fmt_latency(r.latency)}",
    ]


def render_simulate_text(r: SimResult) -> str:
    verdict = "OK" if r.frames_bad == 0 and r.frames_lost == 0 else "DEGRADED"
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
        "frames_sourced": r.frames_sourced,
        "frames_ok": r.frames_ok,
        "frames_bad": r.frames_bad,
        "frames_lost": r.frames_lost,
        "packets_sent": r.packets_sent,
        "packets_recv": r.packets_recv,
        "packets_dropped": r.packets_dropped,
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


def resolve_run_plan(
    args: argparse.Namespace, config: MediaConfig
) -> tuple[RunPlan | None, str | None]:
    """Turn parsed ``run`` options into a validated :class:`RunPlan`, or an
    error message (for a 2 exit). No sockets, no clock."""
    role = str(args.role)
    transport = "unicast" if args.unicast else "multicast"
    group = str(args.group)
    port = int(args.port)
    peer = args.peer

    if role == "sender":
        if not peer:
            return None, "run --role sender requires --peer HOST"
        dest = (str(peer), port)
        frames, duration = _sender_limits(args, config)
        return RunPlan(role, transport, dest, config, frames, duration, False), None

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
    )
    return plan, None


def _sender_limits(
    args: argparse.Namespace, config: MediaConfig
) -> tuple[int, float | None]:
    if args.frames is not None:
        return int(args.frames), None
    if args.duration is not None:
        duration = float(args.duration)
        return int(duration * config.fps), duration
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
    return "\n".join(lines)


# -- argument parsing --------------------------------------------------------


def _config_parent() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--group", default=DEFAULT_MEDIA_GROUP, help="media multicast group")
    p.add_argument("--port", type=int, default=DEFAULT_MEDIA_PORT, help="media port")
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
        prog="yoke-media", description="media pipeline debug/observability tools"
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

    run = sub.add_parser(
        "run",
        parents=[config_parent],
        help="the real sender/receiver runner over UDP",
    )
    run.add_argument("--role", choices=("sender", "receiver"), required=True)
    run.add_argument("--peer", default=None, help="sender destination host")
    run.add_argument("--unicast", action="store_true", help="plain UDP, not multicast")
    run.add_argument("--frames", type=int, default=None, help="sender: frames to send")
    run.add_argument("--duration", type=float, default=None, help="run duration (s)")
    run.add_argument("--snoop", action="store_true", help="receiver: dump topology")
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


def _open_socket(transport: str, host: str, port: int) -> socket.socket:
    """Receiver socket: a multicast-joined socket (bound to ``port``) or a
    plain UDP socket bound to ``port`` for unicast."""
    from yoke.net.mcast import open_multicast_socket

    if transport == "multicast":
        return open_multicast_socket(host, port)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", port))
    return sock


def _open_sender_socket(transport: str) -> socket.socket:
    """Sender socket: bound to an EPHEMERAL local port — never the
    destination port (binding that would steal the receiver's inbound
    datagrams when both run on one host). Multicast sends need the TTL and
    loopback options but no bind to the group port."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", 0))
    if transport == "multicast":
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
    return sock


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
    sock = _open_sender_socket(plan.transport)
    runner = SenderRunner(build_sender(config), dest=(host, port))
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

    host, port = plan.endpoint
    snooper = (
        MediaSnooper(rtp_header_body, roles=_roles_for(config)) if plan.snoop else None
    )
    sock = _open_socket(plan.transport, host, port)
    runner = ReceiverRunner(build_receiver(config), snooper=snooper)
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
    """Live passive snoop: join the group, render each packet, transmit
    nothing. Kept minimal; the offline path is the tested one."""

    from yoke.media.snoop import render_text
    from yoke.net.mcast import open_multicast_socket

    config = build_config(args)
    snooper = MediaSnooper(rtp_header_body, roles=_roles_for(config))
    sock = open_multicast_socket(str(args.group), int(args.port))
    try:
        while True:
            try:
                raw, _addr = sock.recvfrom(65535)
            except KeyboardInterrupt:
                break
            record = snooper.observe_packet(raw, 0.0)
            out(render_text(record))
    finally:
        sock.close()
    err(render_topology(snooper.topology()))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
