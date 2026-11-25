"""yoke command line: pair, unpair, reapply, status, inspect, snoop, agent.

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
import secrets as secrets
import socket as socket_mod
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from yoke import config
from yoke.config import DEFAULT_PSK_PATH as DEFAULT_PSK_PATH
from yoke.config import OPEN_MODE_WARNING as OPEN_MODE_WARNING
from yoke.config import resolve_psk as resolve_psk
from yoke.control import (
    COLLECT_WINDOW as COLLECT_WINDOW,
)
from yoke.control import (
    RETRY_SPACING_BASE as RETRY_SPACING_BASE,
)
from yoke.control import (
    STATUS_SORT_KEYS as STATUS_SORT_KEYS,
)
from yoke.control import (
    BatchIntentResult as BatchIntentResult,
)
from yoke.control import (
    IntentIO as IntentIO,
)
from yoke.control import (
    IntentOutcome as IntentOutcome,
)
from yoke.control import (
    RecvFn as RecvFn,
)
from yoke.control import (
    _read,
)
from yoke.control import (
    collect as _control_collect,
)
from yoke.control import (
    compact_json as _json,
)
from yoke.control import (
    default_origin as default_origin,
)
from yoke.control import (
    driver_detail_text as _driver_detail,
)
from yoke.control import (
    drop_summary as _drop_summary,
)
from yoke.control import (
    id_errors as id_errors,
)
from yoke.control import (
    inspect_node_data as inspect_node_data,
)
from yoke.control import (
    intent_outcome_body as intent_outcome_body,
)
from yoke.control import (
    new_operation_author as new_operation_author,
)
from yoke.control import (
    render_inspect_text as render_inspect_text,
)
from yoke.control import (
    run_intent as run_intent,
)
from yoke.control import (
    run_reapply as run_reapply,
)
from yoke.control import (
    run_unpair_producer as run_unpair_producer,
)
from yoke.control import (
    sort_nodes as sort_nodes,
)
from yoke.core.types import (
    CONTROL_GROUP,
    CONTROL_PORT,
    NODE_ID_RE,
    PEER_TIMEOUT,
    VERSION_ZERO,
)
from yoke.net.mcast import open_multicast_socket, validate_multicast
from yoke.snoop import (
    NodeView,
    Snooper,
    SnoopRecord,
    render_json,
    render_text,
)
from yoke.terminal import escape_terminal


def _display(value: str) -> str:
    """Keep an untrusted label on one printable terminal line."""
    return escape_terminal(value)


def _validate_window(window: float) -> None:
    if not math.isfinite(window) or window <= 0:
        raise ValueError("collection window must be finite and positive")
    if window >= threading.TIMEOUT_MAX:
        raise ValueError("collection window exceeds the platform timeout limit")


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
    return _control_collect(recv, clock, psk, window, wall_clock=wall_clock)


# -- status & inspect --------------------------------------------------------


def render_status_table(
    nodes: dict[str, NodeView],
    now: float,
    *,
    counts: dict[str, int] | None = None,
    sort_key: str = "id",
) -> str:
    """Table of announced nodes; stale/error nodes and drops flagged."""
    headers = ("ID", "ROLE", "KIND", "STATE", "PAIRING", "NAME", "DETAIL")
    rows: list[tuple[str, ...]] = [headers]
    for node_id, view in sort_nodes(nodes, now, sort_key):
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
            (
                node_id,
                view.role,
                view.kind,
                state,
                pairing,
                _display(view.name),
                _driver_detail(view.driver_status),
            )
        )
    widths = [max(len(row[i]) for row in rows) for i in range(len(headers))]
    lines = [
        "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip()
        for row in rows
    ]
    if len(rows) == 1:
        lines.append("(no nodes announced)")
    drops = _drop_summary(counts)
    if drops:
        lines.append(f"dropped frames: {drops}")
    return "\n".join(lines)


def run_status(
    recv: RecvFn,
    clock: Callable[[], float],
    psk: bytes,
    window: float,
    *,
    json_mode: bool = False,
    sort_key: str = "id",
    wall_clock: Callable[[], float] | None = None,
) -> tuple[int, str]:
    snooper = collect(recv, clock, psk, window, wall_clock=wall_clock)
    now = clock()
    if json_mode:
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
                "driver_status": view.driver_status,
                "age_seconds": round(max(0.0, now - view.last_seen), 6),
                "stale": now - view.last_seen > PEER_TIMEOUT,
            }
            for node_id, view in sort_nodes(snooper.nodes, now, sort_key)
        ]
        return 0, _json(
            {
                "schema_version": 1,
                "authentication": "site-psk" if psk else "open-unauthenticated",
                "nodes": nodes,
                "counts": snooper.counts,
            }
        )
    return 0, render_status_table(
        snooper.nodes, now, counts=snooper.counts, sort_key=sort_key
    )


def run_inspect(
    recv: RecvFn,
    clock: Callable[[], float],
    psk: bytes,
    node_id: str,
    window: float,
    *,
    json_mode: bool = False,
    wall_clock: Callable[[], float] | None = None,
) -> tuple[int, str, str | None]:
    """Collect announces and return full debug details for one node."""
    if not NODE_ID_RE.fullmatch(node_id):
        message = f"invalid node id: {node_id!r}"
        if json_mode:
            return (
                2,
                _json({"schema_version": 1, "exit_code": 2, "errors": [message]}),
                message,
            )
        return 2, "", message
    snooper = collect(recv, clock, psk, window, wall_clock=wall_clock)
    data = inspect_node_data(snooper, node_id, clock(), psk=psk)
    if data is None:
        drops = _drop_summary(snooper.counts)
        drop_note = f" (dropped: {drops})" if drops else ""
        message = f"node {node_id!r} did not announce within {window:g}s{drop_note}"
        if json_mode:
            return (
                1,
                _json(
                    {
                        "schema_version": 1,
                        "authentication": "site-psk" if psk else "open-unauthenticated",
                        "exit_code": 1,
                        "node": None,
                        "counts": snooper.counts,
                        "errors": [message],
                    }
                ),
                message,
            )
        return 1, "", message
    if json_mode:
        return 0, _json(data), None
    return 0, render_inspect_text(data), None


# -- snoop -------------------------------------------------------------------


def render_topology(snooper: Snooper, *, now: float | None = None) -> str:
    return json.dumps(snooper.topology(now=now), sort_keys=True)


def run_snoop(
    recv: Callable[[], tuple[bytes, float] | None],
    psk: bytes,
    json_mode: bool,
    emit: Callable[[str], None],
    emit_err: Callable[[str], None],
    *,
    clock: Callable[[], float],
    wall_clock: Callable[[], float] | None = None,
    max_records: int | None = None,
) -> int:
    """Print every observed frame until ``recv`` ends, ``max_records`` is
    reached, or SIGINT arrives, then summarize topology at the injected
    monotonic clock's current time.

    Receive timestamps and ``clock`` must use the same time domain. No
    datagram is needed to expire liveness after a completely silent group.
    """
    if max_records is not None and (type(max_records) is not int or max_records <= 0):
        raise ValueError("snoop count must be a positive integer")
    start = clock()
    snooper = Snooper(
        psk, now=start, wall_time=wall_clock() if wall_clock is not None else start
    )
    render: Callable[[SnoopRecord], str] = render_json if json_mode else render_text
    emitted = 0
    try:
        while max_records is None or emitted < max_records:
            item = recv()
            if item is None:
                break
            for record in snooper.observe(*item, wall=_read(wall_clock)):
                emit(render(record))
                emitted += 1
                if max_records is not None and emitted >= max_records:
                    break
    except KeyboardInterrupt:
        pass
    emit_err(render_topology(snooper, now=clock()))
    return 0


# -- wiring ------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    from yoke import simulation
    from yoke.telemetry.cli import configure_parsers

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
    parser.add_argument(
        "--open",
        action="store_true",
        help="use UNAUTHENTICATED open mode (ignore default PSK; isolated labs only)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    pair = sub.add_parser("pair", help="pair CONSUMER to PRODUCER")
    pair.add_argument("producer")
    pair.add_argument("consumer")
    pair.add_argument("--json", action="store_true", help="emit a JSON result")
    unpair = sub.add_parser(
        "unpair",
        help="detach CONSUMER (or all consumers paired to --producer PRODUCER)",
    )
    unpair.add_argument("consumer", nargs="?", default=None)
    unpair.add_argument(
        "--producer",
        default=None,
        help="unpair all observed consumers currently paired to PRODUCER",
    )
    unpair.add_argument("--json", action="store_true", help="emit a JSON result")
    reapply = sub.add_parser(
        "reapply",
        help="re-assert CONSUMER's currently announced pairing at the next version",
    )
    reapply.add_argument("consumer")
    reapply.add_argument("--json", action="store_true", help="emit a JSON result")
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
    status.add_argument(
        "--sort",
        choices=STATUS_SORT_KEYS,
        default="id",
        help="sort order (default: id)",
    )
    inspect_cmd = sub.add_parser(
        "inspect", help="show full debug details for one announced endpoint"
    )
    inspect_cmd.add_argument("node", help="endpoint node id to inspect")
    inspect_cmd.add_argument(
        "--json", action="store_true", help="emit a JSON inspection record"
    )
    inspect_cmd.add_argument(
        "--watch",
        type=float,
        default=None,
        metavar="SECONDS",
        help=f"collection window (default {COLLECT_WINDOW}s)",
    )
    snoop = sub.add_parser("snoop", help="passively render the control group")
    snoop.add_argument("--json", action="store_true", help="one JSON line per frame")
    snoop.add_argument(
        "--count",
        type=int,
        default=None,
        metavar="N",
        help="stop cleanly after observing N frames",
    )
    snoop.add_argument(
        "--window",
        type=float,
        default=None,
        metavar="SECONDS",
        help="stop cleanly after SECONDS",
    )
    agent = sub.add_parser("agent", help="run an endpoint agent")
    agent.add_argument("--config", required=True, help="agent TOML config path")
    agent_mode = agent.add_mutually_exclusive_group()
    agent_mode.add_argument(
        "--commission",
        action="store_true",
        help="one-time permission to create missing state; never resets existing state",
    )
    agent_mode.add_argument(
        "--rebase-sequence",
        action="store_true",
        help="with the agent stopped and the clock confirmed correct, recover "
        "from a future sequence reservation (ADR 0023), then exit",
    )
    admission = sub.add_parser(
        "usbip-admission",
        help="root helper: apply the USB/IP data-plane allowlist (ADR 0024)",
    )
    admission.add_argument("action", choices=["apply"])
    admission.add_argument(
        "--port", type=int, required=True, help="usbipd TCP port to filter"
    )
    admission.add_argument("--request", default="/run/yoke/usbip-admission.json")
    admission.add_argument("--status", default="/run/yoke-admission/status.json")
    admission.add_argument("--nft", default="nft", help="nft executable")
    simulate = sub.add_parser(
        "simulate", help="run deterministic offline failure scenarios"
    )
    simulation.configure_parser(simulate)
    configure_parsers(sub)
    return parser


def _err(line: str) -> None:
    print(escape_terminal(line), file=sys.stderr)


def _out(line: str) -> None:
    print(line, flush=True)


def _cmd_agent(
    config_path: str, *, commission: bool = False, rebase: bool = False
) -> int:
    try:
        cfg = config.load(Path(config_path))
    except config.ConfigError as exc:
        _err(str(exc))
        return 2
    if rebase:
        from yoke import statefile
        from yoke.net.runner import rebase_sequence

        try:
            _out(rebase_sequence(cfg))
        except statefile.StateError as exc:
            _err(f"yoke agent: {exc}")
            return 4
        except OSError as exc:
            _err(f"agent I/O failed: {exc}")
            return 3
        return 0
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
        return _cmd_agent(
            str(args.config),
            commission=bool(args.commission),
            rebase=bool(args.rebase_sequence),
        )
    if command == "usbip-admission":
        from yoke.drivers.shellcmd import SubprocessRunner
        from yoke.drivers.usbip import apply_admission

        if not 1 <= args.port <= 65535:
            return _command_error(args, 2, "--port must be 1 through 65535")
        return apply_admission(
            request=Path(args.request),
            status=Path(args.status),
            port=int(args.port),
            runner=SubprocessRunner(),
            nft=str(args.nft),
        )
    if command == "simulate":
        from yoke import simulation

        return simulation.cmd_simulate(args, _out)

    if command in ("metrics", "top"):
        from yoke.telemetry.cli import run_command

        return run_command(args)

    if args.open and args.psk_file is not None:
        return _command_error(args, 2, "choose either --psk-file or --open")
    warning: str | None
    if args.open:
        psk, warning = b"", OPEN_MODE_WARNING
    else:
        try:
            psk, warning = resolve_psk(args.psk_file)
        except (OSError, ValueError) as exc:
            return _command_error(args, 2, f"cannot read PSK file: {exc}")
    origin: str = (
        args.origin
        if args.origin is not None
        else default_origin(socket_mod.gethostname())
    )

    producer: str | None = None
    unpair_by_producer: str | None = None
    if command == "inspect" and not NODE_ID_RE.fullmatch(str(args.node)):
        return _command_error(args, 2, f"invalid node id: {args.node!r}")
    if command == "snoop" and args.count is not None and args.count <= 0:
        return _command_error(args, 2, "--count must be a positive integer")
    if command in ("pair", "unpair", "reapply"):
        problems: list[str] = []
        targets: tuple[str, ...] = ()
        if command == "pair":
            producer = str(args.producer)
            problems = id_errors("pair", str(args.consumer), producer)
            targets = (str(args.consumer), producer)
        elif command == "unpair":
            if bool(args.consumer) == bool(args.producer):
                return _command_error(
                    args,
                    2,
                    "specify either CONSUMER or --producer PRODUCER (not both)",
                )
            if args.producer is not None:
                unpair_by_producer = str(args.producer)
                if not NODE_ID_RE.fullmatch(unpair_by_producer):
                    problems.append(f"invalid producer id: {unpair_by_producer!r}")
                targets = (unpair_by_producer,)
            else:
                problems = id_errors("unpair", str(args.consumer), None)
                targets = (str(args.consumer),)
        else:
            if not NODE_ID_RE.fullmatch(str(args.consumer)):
                problems.append(f"invalid consumer id: {args.consumer!r}")
            targets = (str(args.consumer),)
        if not NODE_ID_RE.fullmatch(origin):
            problems.append(f"invalid origin id: {origin!r}")
        elif origin in targets:
            problems.append("CLI origin must differ from endpoint ids")
        if problems:
            return _command_error(args, 2, *problems)
        if not psk and not args.open:
            # Unsigned intents only reach explicit open-mode labs (ADR 0023).
            return _command_error(
                args,
                2,
                f"no PSK: pass --psk-file FILE (default {DEFAULT_PSK_PATH}), or "
                "--open to send UNAUTHENTICATED lab intents",
            )
    if warning is not None:
        _err(warning)

    group: str = args.group
    port: int = args.port
    window = COLLECT_WINDOW
    if command in ("status", "inspect") and args.watch is not None:
        window = float(args.watch)
    elif command == "snoop" and args.window is not None:
        window = float(args.window)
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

        def make_intent_io() -> IntentIO:
            last_seq = 0

            def next_seq() -> int:
                nonlocal last_seq
                last_seq = max(last_seq + 1, time.time_ns())
                return last_seq

            return IntentIO(
                send=send,
                recv=recv_until,
                clock=time.monotonic,
                seq=next_seq,
                psk=psk,
                origin=origin,
                wall_clock=time.time,
            )

        if command in ("pair", "unpair") and unpair_by_producer is None:
            outcome = run_intent(
                make_intent_io(), command, str(args.consumer), producer
            )
            if args.json:
                _out(
                    _json(
                        intent_outcome_body(
                            op=command,
                            consumer=str(args.consumer),
                            producer=producer,
                            psk=psk,
                            outcome=outcome,
                        )
                    )
                )
            else:
                for line in outcome.out:
                    _out(escape_terminal(line))
            for line in outcome.err:
                _err(line)
            return outcome.exit_code
        if command == "unpair" and unpair_by_producer is not None:
            batch = run_unpair_producer(make_intent_io(), unpair_by_producer)
            if args.json:
                _out(
                    _json(
                        {
                            "schema_version": 1,
                            "op": "unpair-producer",
                            "producer": batch.producer,
                            "authentication": "site-psk"
                            if psk
                            else "open-unauthenticated",
                            "exit_code": batch.exit_code,
                            "results": [
                                intent_outcome_body(
                                    op="unpair",
                                    consumer=consumer_id,
                                    producer=None,
                                    psk=psk,
                                    outcome=item,
                                )
                                for consumer_id, item in batch.results
                            ],
                            "errors": list(batch.err),
                        }
                    )
                )
            else:
                for line in batch.out:
                    _out(escape_terminal(line))
            for line in batch.err:
                _err(line)
            return batch.exit_code
        if command == "reapply":
            resolved_producer, outcome = run_reapply(
                make_intent_io(), str(args.consumer)
            )
            if args.json:
                body = intent_outcome_body(
                    op="reapply",
                    consumer=str(args.consumer),
                    producer=resolved_producer,
                    psk=psk,
                    outcome=outcome,
                )
                body["resolved_op"] = (
                    "pair" if resolved_producer is not None else "unpair"
                )
                _out(_json(body))
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
                sort_key=str(args.sort),
                wall_clock=time.time,
            )
            _out(table)
            return code
        if command == "inspect":
            code, output, err_msg = run_inspect(
                recv_until,
                time.monotonic,
                psk,
                str(args.node),
                window,
                json_mode=bool(args.json),
                wall_clock=time.time,
            )
            if output:
                _out(output)
            if err_msg:
                _err(err_msg)
            return code
        if command == "snoop":
            snoop_recv = recv_blocking
            if args.window is not None:
                deadline = time.monotonic() + window

                def recv_window() -> tuple[bytes, float] | None:
                    return recv_until(deadline)

                snoop_recv = recv_window
            return run_snoop(
                snoop_recv,
                psk,
                bool(args.json),
                _out,
                _err,
                clock=time.monotonic,
                wall_clock=time.time,
                max_records=args.count,
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
