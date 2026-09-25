# Yoke media pipeline (software, sans-I/O)

Status: v1 design, 2026-08-09. Normative for the software AV-over-IP
pipeline that runs the compression sweet spot (COMPRESSION.md) and plugs
into yoke's control plane as the `stream` media driver.

## Principle: the media plane is the control plane's twin

The control plane (`yoke.core`) is a sans-I/O state machine with a
passive multicast snooper. The media plane copies the pattern exactly:

- **Sans-I/O stages.** Each pipeline stage is a pure transform: `process(
  unit, now) -> StageOutcome` (emit / events / feedback). No sockets, no
  clock reads. The shell (`yoke.media.runner`) and tests supply I/O and
  time. Every stage and every failure is therefore testable offline.
- **Observable by design.** Every stage emits `StageEvent` taps. The
  pipeline's forward output is *independent of whether anyone reads the
  taps* — so a snooper verifies the stream without perturbing it. This is
  a testable invariant (`push` with vs without an observer emits the
  identical unit sequence), and it is the media analogue of `yoke snoop`.
- **Two observation modes.** White-box: in-process taps (per-stage
  events). Black-box: a passive UDP subscriber that reconstructs health
  from the packets on the wire alone — the true "snoop without
  interfering," and the conformance oracle for the wire format.
- **Deterministic simulation.** A seeded media network sim (drop / dup /
  reorder / delay / corrupt / congest) drives the receiver pipeline, so
  FEC recovery, jitter buffering, and rate adaptation are reproduced
  exactly with no hardware.

## The pipeline

Sender: `source → codec → packetize → fec → [wire]`.
Receiver: `[wire] → fec-decode → jitter → depacketize → codec-decode →
sink`. Feedback: `sink/jitter` measure loss/jitter/congestion →
`NetworkReport` → carried back → sender's rate controller slides the
codec target within the visually-lossless band.

| Stage | Transform | Fan-out |
|---|---|---|
| `source` | synthetic frames with an embedded frame-id + digest (glass-to-glass verifiable) | 1 → 1 frame |
| `codec` (encode) | model compression at a target ratio; `passthrough` is bit-exact; line-based slices | 1 frame → N slices |
| `ratecontrol` | consume feedback, set codec target within [RATIO_MIN, RATIO_MAX] | signal only |
| `packetize` | RTP header + payload, split ≤ MTU | 1 slice → M packets |
| `fec` (encode) | ST 2022-7 duplicate and/or XOR parity | N → N (+ parity) |
| `jitter` | reorder, hold for playout delay, drop late | buffered, tick-driven |
| `sink` | reassemble frame, verify digest/id, record metrics, emit feedback | M packets → 1 frame |

## Data model (types.py — normative)

- `MediaUnit(kind, seq, pts, payload, source_digest, meta)` — one item.
  `seq` monotonic per source; `pts` the frame's 90 kHz timestamp shared
  by its slices/packets; `source_digest` rides from the origin for
  end-to-end integrity. `with_payload()` derives a wrapped unit keeping
  identity.
- `StageEvent(stage, kind, seq, pts, now, detail)` — a non-interfering
  tap.
- `NetworkReport(now, loss, jitter, congestion, frames_ok, frames_bad)` —
  receiver→sender feedback driving `ratecontrol`.
- `StageOutcome(emit, events, feedback)` — a stage's return.
- `PipelineOutput(emitted, events, feedback)` — a whole-pipeline push.

## Verification / debugging tools (first-class)

- `yoke-media probe` — push a known pattern through a pipeline (or over
  the wire), report end-to-end integrity, effective ratio, loss, FEC
  recoveries, and simulated glass-to-glass latency.
- `yoke-media snoop` — passive verifier: join the media group (or read a
  tap stream), render per-stage health and topology, transmit nothing.
- `yoke-media trace` — dump a single unit's journey through every stage
  (JSON), for debugging.
- `yoke-media simulate` — run the pipeline over the seeded sim with a
  fault script; report survival and where units died.

Metrics are computed only from `StageEvent`s (white-box) or wire packets
(black-box), never by instrumenting the transform — keeping observation
non-interfering.

## Integration with the control plane

The `stream` driver (`yoke.drivers`) advertises the media parameters in
the pairing intent's `media` dict (`{"transport": "rtp", "group": ...,
"port": ..., "pt": ..., "encoding": "raw|jpegxs|vc2", "fec": ...}`) and,
on `attach`, starts a receiver pipeline (and on the producer, a sender);
`detach` stops it. Real codecs (SVT-JPEG-XS, FFmpeg VC-2, GStreamer) live
behind the `Codec` protocol and are invoked out-of-process — the
stdlib-only sans-I/O pipeline models the stage so the whole thing is
testable with zero third-party deps, and the real codec is a drop-in
behind the same seam (the `jap`/`igmp` driver pattern).
