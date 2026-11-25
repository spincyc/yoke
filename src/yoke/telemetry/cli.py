"""Thin command adapters for the reusable telemetry and TUI control components."""

from __future__ import annotations

import argparse
import json
import math
import socket
import sys
import threading

from yoke.terminal import escape_terminal


def configure_parsers(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    metrics = sub.add_parser("metrics", help="serve cached read-only endpoint metrics")
    metrics.add_argument(
        "--listen", default="127.0.0.1", help="literal IPv4 bind address"
    )
    metrics.add_argument("--http-port", type=int, default=9754)
    metrics.add_argument(
        "--expose", action="store_true", help="allow a non-loopback bind"
    )
    metrics.add_argument(
        "--once", action="store_true", help="print a snapshot and exit"
    )
    metrics.add_argument(
        "--format", choices=("prometheus", "json"), default="prometheus"
    )
    metrics.add_argument(
        "--window", type=float, default=2.5, help="one-shot collection seconds"
    )
    top = sub.add_parser(
        "top", help="interactive terminal endpoint monitor and control console"
    )
    top.add_argument("--url", help="read an explicit collector HTTP(S) URL (read-only)")
    top.add_argument(
        "--once", action="store_true", help="print a plain dashboard and exit"
    )
    top.add_argument(
        "--read-only",
        action="store_true",
        help="observe only; disable interactive pair/unpair/reapply writes",
    )
    top.add_argument("--width", type=int, default=120, help="one-shot terminal columns")
    top.add_argument("--height", type=int, default=36, help="one-shot terminal rows")
    top.add_argument(
        "--window", type=float, default=2.5, help="one-shot collection seconds"
    )
    for parser in (metrics, top):
        parser.add_argument(
            "--interval", type=float, default=1.0, help="sample seconds (0.2-60)"
        )
        parser.add_argument(
            "--local-node", help="explicit collector-host node association"
        )
        parser.add_argument(
            "--demo", action="store_true", help="synthetic offline data"
        )


def _build_control_callbacks(
    *,
    group: str,
    port: int,
    interface: str,
    psk: bytes,
    origin: str,
    writable: bool,
    readonly_reason: str | None,
) -> object:
    from yoke.control import (
        execute_multicast_intent,
        execute_multicast_reapply,
        execute_multicast_unpair_producer,
    )
    from yoke.telemetry.tui import ActionRequest, ActionResult, ControlCallbacks

    if not writable:
        return ControlCallbacks(writable=False, readonly_reason=readonly_reason)

    def execute(request: ActionRequest) -> ActionResult:
        if request.op in ("pair", "unpair"):
            outcome = execute_multicast_intent(
                group=group,
                port=port,
                interface=interface,
                psk=psk,
                origin=origin,
                op=request.op,
                consumer=request.target,
                producer=request.producer,
            )
            ok = outcome.exit_code == 0
            text = (
                outcome.out[0]
                if ok and outcome.out
                else (outcome.err[-1] if outcome.err else f"{request.label()} failed")
            )
            return ActionResult(ok, text)
        if request.op == "reapply":
            _producer, outcome = execute_multicast_reapply(
                group=group,
                port=port,
                interface=interface,
                psk=psk,
                origin=origin,
                consumer=request.target,
            )
            ok = outcome.exit_code == 0
            text = (
                outcome.out[0]
                if ok and outcome.out
                else (outcome.err[-1] if outcome.err else f"{request.label()} failed")
            )
            return ActionResult(ok, text)
        batch = execute_multicast_unpair_producer(
            group=group,
            port=port,
            interface=interface,
            psk=psk,
            origin=origin,
            producer=request.target,
        )
        ok = batch.exit_code == 0
        if ok:
            summary = (
                batch.out[0]
                if len(batch.out) == 1
                else f"unpaired {len(batch.results)} consumer(s) from {batch.producer}"
            )
        else:
            summary = batch.err[-1] if batch.err else f"{request.label()} failed"
        return ActionResult(ok, summary)

    return ControlCallbacks(writable=True, readonly_reason=None, execute=execute)


def run_command(args: argparse.Namespace) -> int:
    from yoke.config import DEFAULT_PSK_PATH, OPEN_MODE_WARNING, resolve_psk
    from yoke.control import default_origin
    from yoke.core.types import NODE_ID_RE
    from yoke.telemetry.client import SnapshotClient
    from yoke.telemetry.http import MetricsServer
    from yoke.telemetry.model import Snapshot
    from yoke.telemetry.prometheus import render as render_prometheus
    from yoke.telemetry.runtime import Monitor, MonitorConfig, validate_interval
    from yoke.telemetry.tui import ControlCallbacks, ViewState, render, run_top

    try:
        validate_interval(args.interval)
        if not math.isfinite(args.window) or not 0 <= args.window <= 60:
            raise ValueError("collection window must be finite and between 0 and 60s")
        if args.command == "top":
            if not 20 <= args.width <= 500 or not 6 <= args.height <= 200:
                raise ValueError(
                    "one-shot dimensions must be 20-500 columns, 6-200 rows"
                )
            if args.url and args.demo:
                raise ValueError("choose either --url or --demo")
            if args.url and args.local_node:
                raise ValueError("--local-node applies to local collection only")

        def display(sample: Snapshot, *, writable: bool = False) -> int:
            if args.command == "metrics":
                if args.format == "json":
                    print(json.dumps(sample.to_body(), sort_keys=True, allow_nan=False))
                else:
                    sys.stdout.write(render_prometheus(sample).decode("utf-8"))
            else:
                state = ViewState(writable=writable)
                state.observe(sample)
                print(
                    "\n".join(
                        render(
                            sample, width=args.width, height=args.height, state=state
                        )
                    )
                )
            return 0

        if args.command == "top" and args.url:
            client = SnapshotClient(args.url)
            ro = ControlCallbacks(
                writable=False, readonly_reason="remote --url mode is read-only"
            )
            return (
                display(client.get(), writable=False)
                if args.once
                else run_top(client.get, args.interval, controls=ro)
            )

        open_mode = bool(getattr(args, "open", False))
        if open_mode and args.psk_file is not None:
            raise ValueError("choose either --psk-file or --open")
        if args.demo:
            psk, warning = b"", None
        elif open_mode:
            psk, warning = b"", OPEN_MODE_WARNING
        else:
            psk, warning = resolve_psk(args.psk_file)
        if warning is not None:
            print(warning, file=sys.stderr)

        origin_arg = getattr(args, "origin", None)
        origin = (
            str(origin_arg)
            if origin_arg is not None
            else default_origin(socket.gethostname())
        )
        read_only_flag = bool(getattr(args, "read_only", False))
        writable = False
        readonly_reason: str | None = None
        if args.command == "top":
            if args.demo:
                readonly_reason = "demo mode is read-only (synthetic data)"
            elif read_only_flag:
                readonly_reason = "read-only mode (--read-only)"
            elif not psk and not open_mode:
                readonly_reason = (
                    f"no PSK: pass --psk-file (default {DEFAULT_PSK_PATH}) or --open"
                )
            else:
                if not NODE_ID_RE.fullmatch(origin):
                    raise ValueError(f"invalid origin id: {origin!r}")
                writable = True

        cfg = MonitorConfig(
            group=args.group,
            port=args.port,
            interface=args.interface,
            psk=psk,
            interval=args.interval,
            local_node=args.local_node,
            demo=args.demo,
        )
        with Monitor(cfg) as monitor:
            if args.once:
                if not args.demo:
                    threading.Event().wait(args.window)
                return display(monitor.finish(), writable=writable)
            if args.command == "top":
                controls = _build_control_callbacks(
                    group=args.group,
                    port=args.port,
                    interface=args.interface,
                    psk=psk,
                    origin=origin,
                    writable=writable,
                    readonly_reason=readonly_reason,
                )
                assert isinstance(controls, ControlCallbacks)
                return run_top(
                    monitor.snapshot,
                    args.interval,
                    get_debug=monitor.debug_context,
                    controls=controls,
                )
            with MetricsServer(
                monitor.cache,
                listen=args.listen,
                port=args.http_port,
                expose=args.expose,
            ) as server:
                print(
                    f"Yoke read-only telemetry: {server.url}/metrics",
                    file=sys.stderr,
                    flush=True,
                )
                while True:
                    threading.Event().wait(min(args.interval, 1.0))
                    monitor.snapshot()  # Fail closed if the collector stops.
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"yoke {args.command}: {escape_terminal(str(exc))}", file=sys.stderr)
        return 2
