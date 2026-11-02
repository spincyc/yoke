# Yoke media pipeline (software, sans-I/O)

Status: reference design, revised 2026-11-02 (ADR 0015). Normative for the software AV-over-IP
pipeline that runs the compression sweet spot (COMPRESSION.md) and plugs
into yoke's control plane as the `stream` media driver.

This is a **synthetic software reference**, not a JPEG XS/VC-2 codec or a
hardware throughput result. `raw` verifies model bytes exactly; `jpegxs`
and `vc2` exercise a ratio model. Real codecs and capture/display remain
external integrations. Multicast below carries unprotected reference
content only. ADR 0011's HDCP ciphertext relay remains point-to-point;
this transport does not authorize protected-content multicast.

## Reference transport and passive observation (normative)

Media UDP is separate from YWP/1 control traffic (`PROTOCOL.md`). The
default IPv4 media group is `239.255.42.42`, port `5004`. Sender, receiver,
and independent `snoop --live` select the same group, port, and local IPv4
interface. TTL (default 1) and multicast loopback (default on) are explicit
socket settings. Interface `0.0.0.0` delegates route selection to the OS;
use the local interface address on multihomed hosts, and `127.0.0.1` for
the single-host gate.

In multicast mode a sender always sends to `--group`; `--peer` is only a
unicast destination with `--unicast`. Senders bind an ephemeral source
port, and each receiver/observer binds the shared destination port and
joins the group independently. Joining or leaving an observer does not
change pairing, receiver membership, sender output, or rate control. The
passive observer has no send operation, emits no feedback, uses actual
monotonic arrival times, and validates reference datagrams before deriving
wire health. Its membership uses normal OS IGMP behavior; "passive" means
no application datagrams or protocol participation.
On Linux, listeners disable `IP_MULTICAST_ALL` so a group joined by another
local process on the same port cannot leak into this listener's stream.

Feedback is **disabled by default**, including multicast. A designated
active receiver may explicitly send `YKFB` feedback to one configured
unicast IPv4 address and port. Feedback must never be sent to a multicast
address, inferred from observed traffic, or emitted by snoopers. Select
at most one designated reporter for a reference multicast stream: this
reference protocol does not implement multi-receiver report aggregation
or election. Feedback JSON must be an object, loss/congestion finite in
`[0,1]`, jitter finite and nonnegative, and frame counters unsigned 64-bit
integers; malformed reports are dropped without changing rate control.

Reference media datagrams have a fixed 12-byte RTP v2 header (P/X/CC zero)
and the yoke metadata trailer, followed by fragment bytes. Unsupported
header variants, zero/out-of-range slice or fragment counts/indices,
unknown path values, and inconsistent RTP/trailer timestamps are rejected.
Frame metadata (source, digest, counts, payload type, SSRC) is immutable
while assembling; conflicting duplicate fragments cannot overwrite bytes.
Reference defaults bound one frame to 64 MiB, active assembly to 16 frames
and 256 MiB total, counts to 65535 per wire field, and actual fragments to
65536 per frame. Jitter/FEC caches and completed-frame history are bounded;
budget exhaustion drops data and emits diagnostic events instead of
growing without limit. The wire layout is unchanged by these validation
rules; this is not an interoperable JPEG XS/RFC 4175 payload format.
Live UDP supports `none` and `dup` FEC only. `xor` is an offline simulation
model without a defined wire representation; live commands reject it before
opening sockets. `dup` sends two reference copies on the selected socket,
not independently routed redundant paths or a claim of ST 2022-7 compliance.

Default jitter storage is at most 32767 packets (the RTP serial-number
half-space) and 256 MiB. XOR repair admits at most 1024 members per group,
256 pending groups, 256 MiB buffered payload, and 1024 recent completed
groups. Duplicate merging remembers at most 32768 packet identities and
256 MiB payload; eviction reports single-path recovery. Constructors may
select smaller budgets for deterministic stress tests.
After a long outage, a validated reference packet from the same SSRC with
a strictly newer 64-bit frame identity may establish a new jitter-buffer
epoch when its wrapped RTP sequence would otherwise be late or ambiguous.
The buffer discards held old-epoch data and emits an explicit discontinuity
event with an unknown gap count. Bare RTP cannot establish this epoch;
SSRC changes or source-counter restarts require full receiver/pipeline
reconstruction for the new session: resetting only jitter leaves other
stages' FEC and completed-frame histories intact. The frame counter is not
authenticated and cannot distinguish rollback from replay.

Passive inference uses a bounded 4096-packet extended RTP sequence window,
so rollover is normal, unique markers count once, and late packets recover
only gaps still in the window. Cumulative loss retains older gaps. Stream,
stage, frame-path, and pending timing maps are bounded; latency aggregates
use constant storage. Invalid/nonfinite/negative metric values are ignored,
and unspecified integrity results never imply success. Topology exposes
these inference limits; a black-box observer cannot reconstruct unseen
stages or claim original source integrity from an RTP header alone.

Codec accounting preserves a valid modeled raster `frame_bytes` value
from the source (falling back to actual payload length for ordinary byte
inputs). Ratios derived from modeled byte counts are labeled as model
values; actual datagram byte counts remain wire measurements. Direct
in-process codec transforms preserve source capture time. Decoder slice
metadata must agree on frame identity/count/index and cannot overwrite
conflicting slices or leave partial/completed histories unbounded.
Incomplete or inconsistent reconstructed frames fail sink integrity.
The sink retains 4096 recent sequence identities. Ordering and late-gap
recovery are exact within this moving window; older arrivals are diagnosed
as outside history and do not rewrite cumulative loss/order counters.
Integrity is still checked on those arrivals. Capture-to-sink latency is
reported only when an explicit finite nonnegative source capture time is
available on an aligned clock; host-local receive times do not establish
cross-host or physical glass-to-glass latency.
Sink `last_latency` and `mean_latency` are `null` until the first valid
explicit capture-time sample. A measured `0.0` is valid and distinct from
unknown; later frames without capture time leave valid aggregates intact.

## Reference simulation measurements

The CLI simulator returns receiver reports on a reliable modeled reverse
link after a fixed 0.5 ms delay, then calls the sender's feedback path at
that delivery time. Forward faults do not also fault the reverse channel.
Rate adjustment events therefore follow receiver measurements causally and
affect subsequent synthetic frames; this demonstrates a controller model,
not real codec throughput or real-network congestion measurement.
Outgoing reports use the receiver jitter buffer's current inter-arrival
estimate independently of source capture time, in both the UDP shell and
simulator. Report loss remains the sink's observed residual frame-gap
estimate; it is distinct from the simulator's known injected packet loss.

Wire OUT/IN taps bracket the simulated network before receiver FEC and
jitter. They count parity and duplicate copies symmetrically. The simulator
also reports its known injected drops and added network duplicates, so
duplicate arrivals cannot conceal known wire loss. Post-FEC data delivery
and FEC recovery are separate counters. Known source frame identities are
compared with sink integrity events: never-arriving leading, trailing, or
entire streams count as lost, and simulate's verdict uses the same complete
integrity gate as probe (simulate remains a reporting command with exit 0).

`MediaConfig.mtu` and CLI `--mtu` are complete IPv4 path MTU (98..65535),
including IPv4/UDP headers and the reference trailer. Assembly reserves
28 bytes for IPv4/UDP and the trailer before passing an internal RTP budget
to `Packetizer`. Standalone `Packetizer(mtu)` retains its RTP+fragment
budget convention for direct offline stage tests.

Simulation supports virtual times in `[0, 3600]` seconds, fault/reverse-link
delays and jitter magnitudes in `[0, 60]` seconds, and tick intervals in
`[0.000001, 1]` seconds. Time must be finite and monotonic. A link has a
100000-step cumulative work budget (tests may choose a smaller budget).
Unsupported timing, precision that prevents clock progress, or exhausted
work fails with an actionable `ValueError`; it must never spin indefinitely.

Finite streams end with `Pipeline.finish(now)`, an idempotent terminal
operation that calls stage flushes in order and routes their output through
downstream stages before those stages finish. Sender finalization emits
any partial XOR parity group before network drain; receiver finalization
runs after the last scheduled arrival. Terminal jitter flush forces held
packets out in RTP order at the actual supplied time and reports the forced
drain, without inventing later arrivals or playout timestamps. Incomplete
remaining assembly is diagnosed and released. No new push is permitted
after finish; tick/feedback emit nothing and deadlines disappear. UDP
receivers ignore late arrivals after finalization while closing transport.

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
| `source` | synthetic frames with an embedded frame-id + digest (reference end-to-end verifiable) | 1 → 1 frame |
| `codec` (encode) | model compression at a target ratio; `passthrough` is bit-exact; line-based slices | 1 frame → N slices |
| `ratecontrol` | consume feedback, set codec target within [RATIO_MIN, RATIO_MAX] | signal only |
| `packetize` | RTP header + payload, split ≤ MTU | 1 slice → M packets |
| `fec` (encode) | duplicate-copy model and/or offline XOR parity | N → N (+ parity) |
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
  recoveries, and simulated source-to-sink latency.
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

Producer media parameters travel in the producer's `ann.media` object;
pairing intents name endpoints and do not carry media configuration.
`StreamDriver.attach` starts an external receiver process using its command
builder; the default builder validates standard-RTP media parameters.
`detach` stops that process. Process liveness is reported separately from
actual media flow. `StreamProducer` only builds and returns advertised
metadata; it owns no sender process or media-flow health check. Capture
and sender lifecycle must be managed externally.

The default external receiver template uses GStreamer and standard RTP,
whereas `yoke-media run` uses the synthetic reference trailer defined
above. These are distinct payload contracts and are not directly
interoperable. Producer announcements identify them explicitly with
`framing: "standard-rtp"` for the external stream driver and
`framing: "yoke-reference-v1"` for the synthetic sender. The external stream
consumer requires `standard-rtp`; matching encoding names alone do not
establish compatibility. The in-process `Codec` seam supplies
passthrough/ratio
models; no SVT-JPEG-XS, FFmpeg VC-2, or GStreamer codec implementation is
currently plugged into that seam. An external codec integration requires
explicit framing, process lifecycle, and compatibility work and its own
verification; the reference models are not real-codec drop-ins.
