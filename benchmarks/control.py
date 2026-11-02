"""Reproducible control-plane workload with informational wall/CPU timings.

Run ``python benchmarks/control.py --nodes 12 --intents 1000 --repeat 5``
from an environment containing the installed yoke package. No sockets,
sleeping, external devices or timing thresholds are involved.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import time

from yoke.core.types import Body, Pairing, Role
from yoke.simulation import Simulation


def workload(*, nodes: int, intents: int, seed: int, observers: int = 0) -> Body:
    if nodes < 3 or intents < 1 or observers < 0:
        raise ValueError("nodes >= 3, intents >= 1 and observers >= 0 are required")
    net = Simulation(seed=seed, record=False)
    for index in range(observers):
        net.add_observer(f"snooper-{index}")
    producers = ("prod-a", "prod-b")
    for producer in producers:
        net.add_node(producer, Role.PRODUCER)
    consumers = [f"con-{index}" for index in range(nodes - len(producers))]
    for consumer in consumers:
        net.add_node(consumer, Role.CONSUMER)
    net.run(3)
    expected: dict[str, Pairing] = {}
    for index in range(intents):
        consumer = consumers[index % len(consumers)]
        version = (index // len(consumers) + 1, "bench-cli")
        producer = producers[(index // len(consumers)) % len(producers)]
        net.intent(consumer, producer, version=version, origin="bench-cli")
        expected[consumer] = Pairing(producer, version)
        net.run(0.002)
    net.run(5)
    for consumer, pairing in expected.items():
        net.assert_agreement(consumer, pairing)
    return net.snapshot()


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def _nonnegative(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nodes", type=_positive, default=12)
    parser.add_argument("--intents", type=_positive, default=1000)
    parser.add_argument("--repeat", type=_positive, default=5)
    parser.add_argument("--observers", type=_nonnegative, default=0)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)
    if args.nodes < 3:
        parser.error("--nodes must be at least 3 (two producers and one consumer)")
    wall: list[float] = []
    cpu: list[float] = []
    reference: Body | None = None
    for _ in range(args.repeat):
        wall_start, cpu_start = time.perf_counter(), time.process_time()
        snapshot = workload(
            nodes=args.nodes,
            intents=args.intents,
            seed=args.seed,
            observers=args.observers,
        )
        cpu.append(time.process_time() - cpu_start)
        wall.append(time.perf_counter() - wall_start)
        if reference is not None and snapshot != reference:
            raise AssertionError(
                "identical benchmark workloads produced different state"
            )
        reference = snapshot
    assert reference is not None
    print(
        json.dumps(
            {
                "schema_version": 1,
                "benchmark": "control",
                "environment": {
                    "python": platform.python_version(),
                    "implementation": platform.python_implementation(),
                    "platform": platform.platform(),
                    "machine": platform.machine(),
                    "cpu_count": os.cpu_count(),
                },
                "workload": {
                    "nodes": args.nodes,
                    "intents": args.intents,
                    "observers": args.observers,
                    "seed": args.seed,
                    "repeat": args.repeat,
                },
                "result": reference,
                "timing": {
                    "wall_seconds": wall,
                    "cpu_seconds": cpu,
                    "wall_median_seconds": statistics.median(wall),
                    "cpu_median_seconds": statistics.median(cpu),
                    "intents_per_wall_second": args.intents / statistics.median(wall),
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
