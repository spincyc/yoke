"""Reproducible pure-software media benchmark; prints one JSON document.

Run from the checkout with ``PYTHONPATH=src python benchmarks/media.py``.
Timing measures the Python reference model, not a real codec or hardware
latency. Fingerprinting and optional memory sampling run outside timed samples.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import platform
import statistics
import sys
import time
import tracemalloc
from collections.abc import Callable
from typing import Any

from yoke.media.assembly import MediaConfig, build_receiver, build_sender
from yoke.media.pipeline import Pipeline
from yoke.media.stages.codec import CodecDecodeStage, CodecEncodeStage
from yoke.media.stages.jitter import JitterBuffer
from yoke.media.stages.ratecontrol import RateController, RateState
from yoke.media.stages.sink import IntegritySink
from yoke.media.stages.source import FRAME_HEADER_LEN, MODEL_BODY_BYTES, SyntheticSource
from yoke.media.types import MediaUnit, PipelineOutput, StageOutcome


def run_workload(
    frames: int, config: MediaConfig, workload: str, *, fingerprint: bool = False
) -> dict[str, Any]:
    """Build fresh state and run a fixed virtual-time schedule."""
    source = SyntheticSource(
        width=config.width,
        height=config.height,
        fps=config.fps,
        pattern=config.pattern,
        seed=config.seed,
    )
    sink = IntegritySink()
    state = RateState()
    sender = Pipeline(
        [RateController(state), CodecEncodeStage(config.make_codec(), state=state)]
    )
    receiver = Pipeline([CodecDecodeStage(config.make_codec()), sink])
    if workload == "pipeline":
        built_sender = build_sender(config)
        built_receiver = build_receiver(config)
        source, sender = built_sender.source, built_sender.pipeline
        receiver, sink = built_receiver.pipeline, built_receiver.sink
    digest = hashlib.sha256()
    counts = {"units_emitted": 0, "events": 0, "feedback": 0}
    burst: list[MediaUnit] = []
    jitter = JitterBuffer(playout_delay=0.02)
    if workload == "jitter":
        built_sender = build_sender(config)
        source, sender = built_sender.source, built_sender.pipeline

    def observe(out: StageOutcome | PipelineOutput) -> None:
        units = out.emit if isinstance(out, StageOutcome) else out.emitted
        counts["units_emitted"] += len(units)
        counts["events"] += len(out.events)
        counts["feedback"] += len(out.feedback)
        if fingerprint:
            digest.update(repr(out).encode("utf-8"))

    def receive(out: PipelineOutput, now: float) -> None:
        observe(out)
        for report in out.feedback:
            observe(sender.feed_back(report, now))

    for index in range(frames):
        now = index / config.fps
        generated = source.frame(now)
        observe(generated)
        if workload == "source":
            continue
        sent = sender.push(generated.emit[0], now)
        observe(sent)
        if workload == "jitter":
            burst.extend(sent.emitted)
            continue
        for unit in sent.emitted:
            receive(receiver.push(unit, now + 0.0005), now + 0.0005)
        receive(receiver.tick(now + 0.003), now + 0.003)
    end_now = frames / config.fps
    if workload != "source":
        tail = sender.finish(end_now)
        observe(tail)
        if workload == "jitter":
            burst.extend(tail.emitted)
        else:
            for unit in tail.emitted:
                receive(receiver.push(unit, end_now + 0.0005), end_now + 0.0005)
    if workload == "jitter":
        for unit in reversed(burst):
            observe(jitter.process(unit, 0.0))
        observe(jitter.tick(1.0))
        observe(jitter.flush(1.0))
    elif workload != "source":
        receive(receiver.tick(end_now + 1.0), end_now + 1.0)
        receive(receiver.finish(end_now + 1.0), end_now + 1.0)
    return {
        **counts,
        "sink": sink.metrics(),
        "fingerprint": digest.hexdigest() if fingerprint else None,
    }


def measure(
    run: Callable[[], dict[str, Any]], repeats: int
) -> tuple[list[dict[str, float]], dict[str, Any]]:
    run()  # warm up imports and allocator paths; use fresh state each run
    samples = []
    result: dict[str, Any] = {}
    for _ in range(repeats):
        wall = time.perf_counter()
        cpu = time.process_time()
        result = run()
        samples.append(
            {
                "wall_seconds": time.perf_counter() - wall,
                "cpu_seconds": time.process_time() - cpu,
            }
        )
    return samples, result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=int, default=5000)
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument(
        "--workload",
        choices=("source", "stages", "pipeline", "jitter"),
        default="stages",
    )
    parser.add_argument("--encoding", choices=("raw", "jpegxs"), default="raw")
    parser.add_argument(
        "--pattern", choices=("counter", "bars", "noise"), default="counter"
    )
    parser.add_argument(
        "--memory",
        action="store_true",
        help="sample tracemalloc peak in a separate run",
    )
    args = parser.parse_args(argv)
    if args.frames < 1 or args.repeat < 1:
        parser.error("--frames and --repeat must be positive")
    config = MediaConfig(
        encoding=args.encoding, pattern=args.pattern, playout_delay=0.002
    )
    samples, result = measure(
        lambda: run_workload(args.frames, config, args.workload), args.repeat
    )
    verified = run_workload(args.frames, config, args.workload, fingerprint=True)
    peak_bytes = None
    if args.memory:
        tracemalloc.start()
        run_workload(args.frames, config, args.workload)
        _, peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    wall = statistics.median(s["wall_seconds"] for s in samples)
    print(
        json.dumps(
            {
                "schema_version": 1,
                "scope": "Python reference model; no real codec or hardware timing",
                "environment": {
                    "python": sys.version,
                    "implementation": platform.python_implementation(),
                    "platform": platform.platform(),
                    "machine": platform.machine(),
                    "gc_enabled": gc.isenabled(),
                },
                "workload": {
                    "name": args.workload,
                    "frames": args.frames,
                    "repeat": args.repeat,
                    "encoding": args.encoding,
                    "pattern": args.pattern,
                    "seed": config.seed,
                    "width": config.width,
                    "height": config.height,
                    "fps": config.fps,
                    "slices_per_frame": config.n_slices,
                    "source_payload_bytes": FRAME_HEADER_LEN + MODEL_BODY_BYTES,
                    "modeled_raster_bytes": config.width * config.height * 3,
                    "fec": config.fec,
                    "mtu": config.mtu,
                    "playout_delay_seconds": config.playout_delay,
                },
                "samples": samples,
                "median_wall_seconds": wall,
                "median_cpu_seconds": statistics.median(
                    s["cpu_seconds"] for s in samples
                ),
                "frames_per_second": args.frames / wall,
                "tracemalloc_peak_bytes": peak_bytes,
                "result": {**result, "fingerprint": verified["fingerprint"]},
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
