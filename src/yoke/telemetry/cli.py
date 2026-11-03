"""Thin command adapters for the reusable telemetry components."""

from __future__ import annotations

import argparse
import json
import math
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
    top = sub.add_parser("top", help="interactive terminal endpoint and host monitor")
    top.add_argument("--url", help="read an explicit collector HTTP(S) URL")
    top.add_argument(
        "--once", action="store_true", help="print a plain dashboard and exit"
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


def run_command(args: argparse.Namespace) -> int:
    from yoke.config import resolve_psk
    from yoke.telemetry.client import SnapshotClient
    from yoke.telemetry.http import MetricsServer
    from yoke.telemetry.model import Snapshot
    from yoke.telemetry.prometheus import render as render_prometheus
    from yoke.telemetry.runtime import Monitor, MonitorConfig, validate_interval
    from yoke.telemetry.tui import ViewState, render, run_top

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

        def display(sample: Snapshot) -> int:
            if args.command == "metrics":
                if args.format == "json":
                    print(json.dumps(sample.to_body(), sort_keys=True, allow_nan=False))
                else:
                    sys.stdout.write(render_prometheus(sample).decode("utf-8"))
            else:
                state = ViewState()
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
            return (
                display(client.get())
                if args.once
                else run_top(client.get, args.interval)
            )

        psk, warning = (b"", None) if args.demo else resolve_psk(args.psk_file)
        if warning is not None:
            print(warning, file=sys.stderr)
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
                return display(monitor.finish())
            if args.command == "top":
                return run_top(monitor.snapshot, args.interval)
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
