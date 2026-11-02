"""Measure pure fleet projection/encoding separately from cached reads."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import statistics
import time

from yoke.core import frames
from yoke.core.types import MAX_TRACKED, FrameType
from yoke.telemetry.collector import TelemetryCollector
from yoke.telemetry.http import SnapshotCache


def workload(nodes: int, repeat: int) -> dict[str, object]:
    if not 1 <= nodes <= MAX_TRACKED or not 1 <= repeat <= 1000:
        raise ValueError("nodes must be 1-4096 and repeat 1-1000")
    collector = TelemetryCollector(b"", now=0.0, wall_time=0.0)
    start = time.perf_counter()
    for index in range(nodes):
        collector.observe(
            frames.encode(
                frame_type=FrameType.ANN,
                origin=f"bench-{index}",
                seq=1,
                ts=0,
                body={
                    "role": "producer",
                    "kind": "usb",
                    "name": f"USB {index}",
                    "state": "active",
                    "err": None,
                    "media": None,
                    "pairing": None,
                },
                psk=b"",
            ),
            0.0,
        )
    admission_seconds = time.perf_counter() - start
    sample = collector.snapshot(1.0)
    cache = SnapshotCache(sample)
    projection = []
    cached = []
    for _ in range(repeat):
        start = time.perf_counter()
        cache.update(collector.snapshot(1.0))
        projection.append(time.perf_counter() - start)
        start = time.perf_counter()
        for _ in range(1000):
            cache.representations()
        cached.append((time.perf_counter() - start) / 1000)
    result = cache.representations()
    return {
        "nodes": nodes,
        "repeat": repeat,
        "admission_seconds": admission_seconds,
        "sample_encode_median_seconds": statistics.median(projection),
        "cache_read_median_seconds": statistics.median(cached),
        "json_bytes": len(result.json),
        "prometheus_bytes": len(result.metrics),
        "snapshot_sha256": hashlib.sha256(result.json).hexdigest(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nodes", type=int, default=32)
    parser.add_argument("--repeat", type=int, default=20)
    args = parser.parse_args()
    try:
        result = workload(args.nodes, args.repeat)
    except ValueError as error:
        parser.error(str(error))
    print(
        json.dumps(
            {
                "schema_version": 1,
                "environment": {
                    "python": platform.python_version(),
                    "platform": platform.platform(),
                },
                "result": result,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
