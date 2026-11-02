"""yoke command line: pair, unpair, status, snoop, agent.

Every decision lives in a pure function driven by injected callables
(:class:`IntentIO` for intents, plain ``recv``/``clock`` callables for
collection loops); everything returns strings and exit codes. ``main``
is the only place that parses arguments, opens the real multicast
socket, reads the real clock, and prints.
"""

from __future__ import annotations

import argparse
import importlib
import json
import math
import secrets
import socket as socket_mod
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from yoke import config
from yoke.core import frames
from yoke.core.types import (
    CLI_DEADLINE,
    CLI_RETRIES,
    CONTROL_GROUP,
    CONTROL_PORT,
    MAX_SEQUENCE,
    NODE_ID_RE,
    PEER_TIMEOUT,
    VERSION_ZERO,
    Body,
    FrameType,
    Role,
    Version,
)
from yoke.net.mcast import open_multicast_socket, validate_multicast
from yoke.snoop import (
    NodeView,
    Snooper,
    SnoopRecord,
    escape_terminal,
    render_json,
    render_text,
)

COLLECT_WINDOW = 2.5
RETRY_SPACING_BASE = 0.5
DEFAULT_PSK_PATH = Path("/etc/yoke/psk")
OPEN_MODE_WARNING = "warning: no PSK; open mode is UNAUTHENTICATED (labs only)"

# recv(deadline) -> (raw datagram, receive time) or None once the absolute
# deadline (in the injected clock's domain) has passed with nothing received.
RecvFn = Callable[[float], "tuple[bytes, float] | None"]


def new_operation_author() -> str:
    """A fresh valid YWP/1 author independent of the reusable CLI identity."""
    return "op-" + secrets.token_hex(14)


@dataclass(frozen=True)
class IntentIO:
    """Injectable I/O; ``operation_author`` must be fresh for each operation."""

    send: Callable[[bytes], None]
    recv: RecvFn
    clock: Callable[[], float]
    seq: Callable[[], int]
    psk: bytes
    origin: str
    collect_window: float = COLLECT_WINDOW
    retries: int = CLI_RETRIES
    retry_base: float = RETRY_SPACING_BASE
    deadline: float = CLI_DEADLINE
    wall_clock: Callable[[], float] | None = None
    operation_author: Callable[[], str] = new_operation_author


@dataclass(frozen=True)
class IntentOutcome:
    exit_code: int
    out: tuple[str, ...]
    err: tuple[str, ...]
    sent_version: Version | None
    sends: int
    consumer_announced: bool
    committed: bool = False
    activation: str | None = None


def id_errors(op: str, consumer: str, producer: str | None) -> list[str]:
    """Validate node ids before any I/O happens (PROTOCOL.md id syntax)."""
    errors = []
    if op not in ("pair", "unpair"):
        errors.append(f"invalid operation: {op!r}")
    if not NODE_ID_RE.fullmatch(consumer):
        errors.append(f"invalid consumer id: {consumer!r}")
    if op == "pair" and (producer is None or not NODE_ID_RE.fullmatch(producer)):
        errors.append(f"invalid producer id: {producer!r}")
    if op == "pair" and producer == consumer:
        errors.append("producer and consumer must differ (self-pair)")
    return errors


def _fmt_version(version: Version) -> str:
    return f"[{version[0]},{version[1]}]"


def run_intent(
    io: IntentIO, op: str, consumer: str, producer: str | None = None
) -> IntentOutcome:
    """Send a pair/unpair intent and resolve it against the ack stream.

    Phases per the task/PROTOCOL.md: collect announces (learn the
    consumer's pairing version, sanity-check kinds), then send the intent
    with version ``[n+1, fresh_operation_author]`` on the CLI retry schedule
    until its matching ack arrives or the deadline passes. Retries retain
    this one version; a new operation never reuses the stable CLI identity
    as its version author.
    """
    problems = id_errors(op, consumer, producer)
    if not NODE_ID_RE.fullmatch(io.origin):
        problems.append(f"invalid origin id: {io.origin!r}")
    elif io.origin in (consumer, producer):
        problems.append("CLI origin must differ from endpoint ids")
    for label, value in (
        ("collect_window", io.collect_window),
        ("retry_base", io.retry_base),
        ("deadline", io.deadline),
    ):
        if not math.isfinite(value) or value <= 0:
            problems.append(f"{label} must be finite and positive")
    if type(io.retries) is not int or not 1 <= io.retries <= CLI_RETRIES:
        problems.append(f"retries must be from 1 through {CLI_RETRIES}")
    if problems:
        return IntentOutcome(2, (), tuple(problems), None, 0, False)

    author = io.operation_author()
    if not isinstance(author, str) or not NODE_ID_RE.fullmatch(author):
        return IntentOutcome(2, (), ("invalid operation author",), None, 0, False)

    err: list[str] = []
    start = io.clock()
    snooper = Snooper(
        io.psk,
        now=start,
        wall_time=io.wall_clock() if io.wall_clock is not None else start,
    )
    window_end = start + io.collect_window
    while io.clock() < window_end and consumer not in snooper.nodes:
        item = io.recv(window_end)
        if item is None:
            break
        snooper.observe(*item)

    if io.origin in snooper.nodes:
        return IntentOutcome(
            2, (), ("CLI origin is already announced by an endpoint",), None, 0, False
        )

    consumer_view = snooper.nodes.get(consumer)
    announced = consumer_view is not None
    if consumer_view is None:
        n = 0
        err.append(
            f"warning: consumer {consumer!r} has not announced yet; "
            "proceeding with pairing version 0"
        )
    else:
        if consumer_view.role != Role.CONSUMER.value:
            err.append(
                f"{consumer!r} announces as a {consumer_view.role}, not a "
                "consumer; it will never ack an intent — refusing to send"
            )
            return IntentOutcome(2, (), tuple(err), None, 0, announced)
        n = (consumer_view.pairing_version or VERSION_ZERO)[0]
    if n >= MAX_SEQUENCE:
        return IntentOutcome(
            2, (), ("consumer pairing version is exhausted",), None, 0, announced
        )
    if op == "pair" and producer is not None:
        producer_view = snooper.nodes.get(producer)
        if producer_view is not None and producer_view.role != Role.PRODUCER.value:
            err.append(
                f"{producer!r} announces as a {producer_view.role}, not a "
                "producer; refusing to send"
            )
            return IntentOutcome(2, (), tuple(err), None, 0, announced)
        if (
            consumer_view is not None
            and producer_view is not None
            and consumer_view.kind != producer_view.kind
        ):
            err.append(
                f"kind mismatch: consumer {consumer} is {consumer_view.kind}, "
                f"producer {producer} is {producer_view.kind}; refusing to send"
            )
            return IntentOutcome(2, (), tuple(err), None, 0, announced)

    version: Version = (n + 1, author)
    body: Body = {"op": op, "consumer": consumer, "version": [version[0], version[1]]}
    if op == "pair":
        body["producer"] = producer

    t0 = io.clock()
    hard_deadline = t0 + io.deadline
    offsets = [io.retry_base * (2**k - 1) for k in range(io.retries)]
    sends = 0
    while True:
        now = io.clock()
        if now >= hard_deadline:
            break
        if sends < len(offsets) and now >= t0 + offsets[sends]:
            stamp = io.wall_clock() if io.wall_clock is not None else now
            raw = frames.encode(FrameType.INT, io.origin, io.seq(), stamp, body, io.psk)
            io.send(raw)
            sends += 1
            continue
        if sends < len(offsets):
            next_wake = min(hard_deadline, t0 + offsets[sends])
        else:
            next_wake = hard_deadline
        item = io.recv(next_wake)
        if item is None:
            continue
        for record in snooper.observe(*item):
            frame = record.frame
            if (
                record.verdict == "ok"
                and frame is not None
                and frame.type is FrameType.ACK
                and frame.origin == consumer
                and frame.body.get("version") == [version[0], version[1]]
            ):
                reason = frame.body.get("reason")
                reason_text = reason if isinstance(reason, str) else "?"
                if frame.body.get("applied") is True:
                    target = f" <- {producer}" if op == "pair" else ""
                    view = snooper.nodes.get(consumer)
                    activation = (
                        view.state
                        if view is not None
                        and view.pairing_version == version
                        and view.pairing_producer == producer
                        else "unconfirmed"
                    )
                    line = (
                        f"{op} {consumer}{target} committed at version "
                        f"{_fmt_version(version)} ({reason_text}); "
                        f"activation {activation} "
                        "(yoke status shows current media state)"
                    )
                    return IntentOutcome(
                        0, (line,), tuple(err), version, sends, True, True, activation
                    )
                line = f"{op} rejected by {consumer}: {reason_text}"
                return IntentOutcome(2, (), (*err, line), version, sends, True)
        announced = announced or consumer in snooper.nodes

    diagnosis = (
        "consumer never announced" if not announced else "consumer announced but no ack"
    )
    err.append(
        f"no ack from {consumer!r} within {io.deadline:g}s: "
        f"{diagnosis} ({sends} intents sent)"
    )
    return IntentOutcome(1, (), tuple(err), version, sends, announced)


# -- status ------------------------------------------------------------------


def collect(
    recv: RecvFn,
    clock: Callable[[], float],
    psk: bytes,
    window: float,
    *,
    wall_clock: Callable[[], float] | None = None,
) -> Snooper:
    """Feed everything received within ``window`` seconds into a Snooper."""
    _validate_window(window)
    start = clock()
    snooper = Snooper(
        psk, now=start, wall_time=wall_clock() if wall_clock is not None else start
    )
    end = start + window
    while clock() < end:
        item = recv(end)
        if item is None:
            break
        snooper.observe(*item)
    return snooper


def render_status_table(nodes: dict[str, NodeView], now: float) -> str:
    """Table of announced nodes; stale/error nodes flagged."""
    headers = ("ID", "ROLE", "KIND", "STATE", "PAIRING", "NAME")
    rows: list[tuple[str, ...]] = [headers]
    for node_id, view in sorted(nodes.items()):
        pairing = "-"
        if view.role == "consumer":
            version = view.pairing_version or VERSION_ZERO
            if view.pairing_producer:
                pairing = f"{node_id}->{view.pairing_producer}@{version[0]}"
            else:
                pairing = "unpaired"
        flags = []
        if now - view.last_seen > PEER_TIMEOUT:
            flags.append("stale")
        if view.state == "error":
            flags.append(f"err:{_display(view.err or '?')}")
        state = view.state + (f" [{','.join(flags)}]" if flags else "")
        rows.append(
            (node_id, view.role, view.kind, state, pairing, _display(view.name))
        )
    widths = [max(len(row[i]) for row in rows) for i in range(len(headers))]
    lines = [
        "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip()
        for row in rows
    ]
    if len(rows) == 1:
        lines.append("(no nodes announced)")
    return "\n".join(lines)


def _display(value: str) -> str:
    """Keep an untrusted label on one printable terminal line."""
    return escape_terminal(value)


def _validate_window(window: float) -> None:
    if not math.isfinite(window) or window <= 0:
        raise ValueError("collection window must be finite and positive")
    if window >= threading.TIMEOUT_MAX:
        raise ValueError("collection window exceeds the platform timeout limit")


def run_status(
    recv: RecvFn,
    clock: Callable[[], float],
    psk: bytes,
    window: float,
    *,
    json_mode: bool = False,
    wall_clock: Callable[[], float] | None = None,
) -> tuple[int, str]:
    snooper = collect(recv, clock, psk, window, wall_clock=wall_clock)
    if json_mode:
        now = clock()
        nodes = [
            {
                "id": node_id,
                "role": view.role,
                "kind": view.kind,
                "name": view.name,
                "state": view.state,
                "err": view.err,
                "pairing": (
                    {
                        "producer": view.pairing_producer,
                        "version": list(view.pairing_version or VERSION_ZERO),
                    }
                    if view.role == "consumer"
                    else None
                ),
                "media": view.media,
                "age_seconds": round(max(0.0, now - view.last_seen), 6),
                "stale": now - view.last_seen > PEER_TIMEOUT,
            }
            for node_id, view in sorted(snooper.nodes.items())
        ]
        return 0, _json(
            {
                "schema_version": 1,
                "authentication": "site-psk" if psk else "open-unauthenticated",
                "nodes": nodes,
                "counts": snooper.counts,
            }
        )
    return 0, render_status_table(snooper.nodes, clock())


# -- snoop -------------------------------------------------------------------


def render_topology(snooper: Snooper, *, now: float | None = None) -> str:
    return json.dumps(snooper.topology(now=now), sort_keys=True)


def _json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def run_snoop(
    recv: Callable[[], tuple[bytes, float] | None],
    psk: bytes,
    json_mode: bool,
    emit: Callable[[str], None],
    emit_err: Callable[[str], None],
    *,
    clock: Callable[[], float],
    wall_clock: Callable[[], float] | None = None,
) -> int:
    """Print every observed frame until ``recv`` ends or SIGINT arrives,
    then summarize topology at the injected monotonic clock's current time.

    Receive timestamps and ``clock`` must use the same time domain. No
    datagram is needed to expire liveness after a completely silent group.
    """
    start = clock()
    snooper = Snooper(
        psk, now=start, wall_time=wall_clock() if wall_clock is not None else start
    )
    render: Callable[[SnoopRecord], str] = render_json if json_mode else render_text
    try:
        while True:
            item = recv()
            if item is None:
                break
            for record in snooper.observe(*item):
                emit(render(record))
    except KeyboardInterrupt:
        pass
    emit_err(render_topology(snooper, now=clock()))
    return 0


# -- defaults ----------------------------------------------------------------


def default_origin(hostname: str) -> str:
    """``cli-`` + sanitized hostname, guaranteed to satisfy NODE_ID_RE."""
    sanitized = "".join(
        ch if ch.isascii() and (ch.islower() or ch.isdigit() or ch == "-") else "-"
        for ch in hostname.lower()
    )
    candidate = ("cli-" + sanitized)[:32]
    return candidate if NODE_ID_RE.fullmatch(candidate) else "cli-unknown"


def resolve_psk(
    explicit: str | None, default_path: Path = DEFAULT_PSK_PATH
) -> tuple[bytes, str | None]:
    """The PSK plus an optional stderr warning. OSError propagates for an
    explicitly named file; the default path is simply skipped if absent."""
    if explicit is not None:
        psk = _read_psk(Path(explicit))
    else:
        try:
            psk = _read_psk(default_path)
        except FileNotFoundError:
            psk = b""
    warning = None if psk else OPEN_MODE_WARNING
    return psk, warning


def _read_psk(path: Path) -> bytes:
    """Read a PSK the same way the agent does: strip only one trailing
    newline (preserve raw key bytes)."""
    return config.read_psk(path)


# -- wiring ------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    from yoke import simulation

    parser = argparse.ArgumentParser(
        prog="yoke", description="controllerless HDMI/USB pairing control plane"
    )
    parser.add_argument("--group", default=CONTROL_GROUP, help="control group address")
    parser.add_argument("--port", type=int, default=CONTROL_PORT, help="control port")
    parser.add_argument(
        "--interface", default="0.0.0.0", help="local IPv4 address on the control VLAN"
    )
    parser.add_argument(
        "--psk-file",
        default=None,
        help=f"PSK file (default: {DEFAULT_PSK_PATH} if it exists, else open mode)",
    )
    parser.add_argument("--origin", default=None, help="node id for sent frames")
    sub = parser.add_subparsers(dest="command", required=True)
    pair = sub.add_parser("pair", help="pair CONSUMER to PRODUCER")
    pair.add_argument("producer")
    pair.add_argument("consumer")
    pair.add_argument("--json", action="store_true", help="emit a JSON result")
    unpair = sub.add_parser("unpair", help="detach CONSUMER from its producer")
    unpair.add_argument("consumer")
    unpair.add_argument("--json", action="store_true", help="emit a JSON result")
    status = sub.add_parser("status", help="collect announces and show a table")
    status.add_argument(
        "--json", action="store_true", help="emit a JSON status snapshot"
    )
    status.add_argument(
        "--watch",
        type=float,
        default=None,
        metavar="SECONDS",
        help=f"collection window (default {COLLECT_WINDOW}s)",
    )
    snoop = sub.add_parser("snoop", help="passively render the control group")
    snoop.add_argument("--json", action="store_true", help="one JSON line per frame")
    agent = sub.add_parser("agent", help="run an endpoint agent")
    agent.add_argument("--config", required=True, help="agent TOML config path")
    agent.add_argument(
        "--commission",
        action="store_true",
        help="one-time permission to create missing state; never resets existing state",
    )
    simulate = sub.add_parser(
        "simulate", help="run deterministic offline failure scenarios"
    )
    simulation.configure_parser(simulate)
    return parser


def _err(line: str) -> None:
    print(escape_terminal(line), file=sys.stderr)


def _out(line: str) -> None:
    print(line, flush=True)


def _cmd_agent(config_path: str, *, commission: bool = False) -> int:
    try:
        cfg = config.load(Path(config_path))
    except config.ConfigError as exc:
        _err(str(exc))
        return 2
    if commission:
        cfg = replace(cfg, allow_new_state=True)
    if not cfg.psk:
        _err(OPEN_MODE_WARNING)
    try:
        runner = importlib.import_module("yoke.net.runner")
    except ImportError:
        _err("agent runner not yet available")
        return 3
    try:
        result = runner.run_agent(cfg)
    except config.ConfigError as exc:
        _err(str(exc))
        return 2
    except OSError as exc:
        _err(f"agent I/O failed: {exc}")
        return 3
    return 0 if result is None else int(result)


def _command_error(args: argparse.Namespace, code: int, *errors: str) -> int:
    for message in errors:
        _err(message)
    if getattr(args, "json", False):
        _out(_json({"schema_version": 1, "exit_code": code, "errors": list(errors)}))
    return code


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command: str = args.command
    if command == "agent":
        return _cmd_agent(str(args.config), commission=bool(args.commission))
    if command == "simulate":
        from yoke import simulation

        return simulation.cmd_simulate(args, _out)

    try:
        psk, warning = resolve_psk(args.psk_file)
    except (OSError, ValueError) as exc:
        return _command_error(args, 2, f"cannot read PSK file: {exc}")
    if warning is not None:
        _err(warning)
    origin: str = (
        args.origin
        if args.origin is not None
        else default_origin(socket_mod.gethostname())
    )

    producer: str | None = None
    if command in ("pair", "unpair"):
        producer = str(args.producer) if command == "pair" else None
        problems = id_errors(command, str(args.consumer), producer)
        if not NODE_ID_RE.fullmatch(origin):
            problems.append(f"invalid origin id: {origin!r}")
        elif origin in (str(args.consumer), producer):
            problems.append("CLI origin must differ from endpoint ids")
        if problems:
            return _command_error(args, 2, *problems)

    group: str = args.group
    port: int = args.port
    window = (
        float(args.watch)
        if command == "status" and args.watch is not None
        else COLLECT_WINDOW
    )
    try:
        validate_multicast(group, port, interface=args.interface)
        _validate_window(window)
    except ValueError as exc:
        return _command_error(args, 2, str(exc))
    try:
        sock = open_multicast_socket(group, port, interface=args.interface)
    except OSError as exc:
        return _command_error(args, 3, f"cannot open control multicast socket: {exc}")
    try:

        def send(raw: bytes) -> None:
            sock.sendto(raw, (group, port))

        def recv_until(deadline: float) -> tuple[bytes, float] | None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            sock.settimeout(remaining)
            try:
                raw, _addr = sock.recvfrom(65535)
            except TimeoutError:
                return None
            return raw, time.monotonic()

        def recv_blocking() -> tuple[bytes, float] | None:
            sock.settimeout(None)
            raw, _addr = sock.recvfrom(65535)
            return raw, time.monotonic()

        if command in ("pair", "unpair"):
            io = IntentIO(
                send=send,
                recv=recv_until,
                clock=time.monotonic,
                seq=time.time_ns,
                psk=psk,
                origin=origin,
                wall_clock=time.time,
            )
            outcome = run_intent(io, command, str(args.consumer), producer)
            if args.json:
                _out(
                    _json(
                        {
                            "schema_version": 1,
                            "op": command,
                            "consumer": str(args.consumer),
                            "producer": producer,
                            "authentication": "site-psk"
                            if psk
                            else "open-unauthenticated",
                            "exit_code": outcome.exit_code,
                            "version": list(outcome.sent_version)
                            if outcome.sent_version
                            else None,
                            "committed": outcome.committed,
                            "activation": outcome.activation,
                            "sends": outcome.sends,
                            "errors": list(outcome.err),
                        }
                    )
                )
            else:
                for line in outcome.out:
                    _out(escape_terminal(line))
            for line in outcome.err:
                _err(line)
            return outcome.exit_code
        if command == "status":
            code, table = run_status(
                recv_until,
                time.monotonic,
                psk,
                window,
                json_mode=bool(args.json),
                wall_clock=time.time,
            )
            _out(table)
            return code
        if command == "snoop":
            return run_snoop(
                recv_blocking,
                psk,
                bool(args.json),
                _out,
                _err,
                clock=time.monotonic,
                wall_clock=time.time,
            )
        raise AssertionError(f"unhandled command: {command!r}")
    except OSError as exc:
        return _command_error(args, 3, f"control multicast I/O failed: {exc}")
    except KeyboardInterrupt:
        return 130
    finally:
        sock.close()


if __name__ == "__main__":
    raise SystemExit(main())
