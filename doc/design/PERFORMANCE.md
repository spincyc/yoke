# Software performance measurements

Measured 2026-11-02 with CPython 3.14.9 on Linux x86-64. These are costs of
the synthetic Python reference implementation, not codec throughput, USB
compatibility or capture-to-display latency. Timings are informational;
correctness and resource limits remain verification gates.

The comparison table records the optimization snapshot. Later review added
explicit stream finalization, jitter discontinuity diagnostics and nullable
unknown latency. Current outcome fingerprints include those additions and
therefore intentionally differ from historical fingerprints. Compare
performance variants of the same hardened revision when checking semantic
equivalence; a cross-release fingerprint difference alone is not a regression.

## Optimization results

| Workload | Comparison | Before | After | Change |
| --- | --- | --- | --- | --- |
| Counter pattern, 10,000 frames, median of 7 | Same hardened stages, with and without the performance changes | 407.29 ms | 394.76 ms | 3.1% less time |
| Color bars, 10,000 frames, median of 7 | Same hardened stages, with and without the performance changes | 356.67 ms | 326.72 ms | 8.4% less time |
| Reversed burst of 4,000 packets, median of 5 | Original versus heap-based jitter release | 365.19 ms | 36.49 ms | 90.0% less time |
| Fresh observer processing sequences 0 and 32,767, median of 5 × 200 iterations | Original versus bounded bit-window gap accounting | 1,297.2 µs | 5.4 µs | About 240 times faster for this workload |

The stage comparison retained all new validation and bounded assembly
logic. Only the performance changes were reversed for the comparison:
skipping empty pipeline frontiers, reusing a deterministically reseeded
random generator, caching the color-bar body/checksum, using a fixed frame
header structure and avoiding unused sink-report allocations. Complete
outcome fingerprints and counters matched in both stage workloads. Peak
tracked allocation for the counter workload remained approximately 1 MiB.

The jitter comparison replaced only the jitter implementation. Emitted
units, events and feedback matched. Releasing due packets from a heap
avoids repeatedly scanning the remaining buffer.

The observer measurement includes constructing a new observer, decoding
two synthetic sequence values and recording both arrivals. It does not
measure an isolated packet callback or network throughput. Verdicts and
wire counters matched for this workload and checked recovery, duplication,
reordering and rollover cases within retained history. An independent
repeat measured 1,275.8 µs versus 5.17 µs.

Observation history is bounded to 4,096 sequence positions. A packet older
than that window cannot repair an expired gap or prove duplication; those
limits are part of the reported semantics, not performance shortcuts that
silently preserve an unlimited-history claim.

## Reproduce current workloads

```sh
make benchmark-control
make benchmark-media BENCH_FRAMES=10000 REPEAT=7
```

For more focused media measurements:

```sh
.scratch/venv/bin/python benchmarks/media.py --frames 10000 --repeat 7 --memory
.scratch/venv/bin/python benchmarks/media.py --frames 10000 --repeat 7 --pattern bars
.scratch/venv/bin/python benchmarks/media.py --workload jitter --frames 1000 --repeat 5
```

The default four slices per frame make the last command a 4,000-packet
burst. Benchmark JSON includes the workload, interpreter, machine class,
wall/CPU samples, counters and output fingerprints. Memory sampling and
fingerprinting run separately from the timed samples.

The observer microbenchmark uses an injected two-byte sequence decoder so
it isolates gap accounting from RTP parsing:

```python
from statistics import median
from timeit import repeat
from yoke.media.snoop import MediaSnooper

def observe_gap():
    observer = MediaSnooper(lambda raw: {"seq": int.from_bytes(raw, "big")})
    observer.observe_packet(b"\x00\x00", 0.0)
    observer.observe_packet(b"\x7f\xff", 1.0)

samples = repeat(observe_gap, number=200, repeat=5)
print(median(samples) / 200 * 1_000_000)  # microseconds per complete workload
```

For historical comparisons, use baseline commit `10ab5d4` and the same
interpreter, workload and machine. Keep the measured path isolated from
unrelated work. A targeted improvement does not establish that the entire
release is faster: stronger validation, bounds and recovery add work.
An early whole-pipeline comparison was slower and overlapped other lanes,
so it does not support an attributable performance claim.

## Verification boundary

Use [the simulation campaign](SIMULATION.md) and `make verify` before
interpreting timings. Benchmark tests verify deterministic results and
successful integrity, without imposing a speed threshold on shared CI.
The independent code review and final cold review cover the optimized
paths alongside their correctness and memory bounds.
