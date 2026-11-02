# 0015 — Reference media multicast, passive debugging, and input bounds

Date: 2026-11-02. Status: accepted for the software reference.

## Context

The media CLI labeled a sender multicast while sending to `--peer`, and
the passive live listener used a zero timestamp and did not validate the
reference trailer. This did not demonstrate the intended independent
same-group debugging path. Malformed feedback and unbounded assembly
caches also made field failures hard to reproduce safely.

## Decision

`MEDIA.md` defines the reference transport before implementation: IPv4
multicast uses a shared group/port with explicit local interface, TTL,
and loopback options. The sender's source port is ephemeral. Receivers
and observers join independently; unicast remains available explicitly.
Linux listeners disable the kernel's all-local-multicast-groups option to
isolate the selected group when multiple groups share a port on one host.
An observer processes arrival datagrams and monotonic times only, has no
application send path, and never changes pairing or rate control.

Feedback is opt-in from a designated active receiver to an explicit
unicast endpoint only. It is never multicast or inferred from sender
addresses, preventing automatic feedback fanout as observers join.
Feedback scalar types, finite numeric ranges, and counters are checked
before rate control. This is a lab reference, not a multi-receiver
feedback aggregation protocol or an authenticated media protocol.

Keep the existing fixed RTP plus metadata wire layout. Reject unsupported
RTP header layouts and inconsistent or out-of-range fragment metadata.
Keep XOR FEC offline-only: no parity wire layout is defined, so live CLI
commands reject XOR before opening sockets. Live duplicate FEC remains
two copies on the selected socket, without a redundant-path compliance claim.
Treat frame metadata as immutable and reject conflicting duplicates.
Bound assembly, jitter, FEC, and completed history by explicit resource
budgets, including a 64 MiB frame ceiling and 16-frame/256 MiB assembly
ceiling. Report rejected data through existing diagnostic events.
Permit explicit jitter discontinuity recovery after a long outage using a
validated strictly newer full-width reference frame identity on the same
SSRC; discard the held prior epoch and report an unknown gap. Bare RTP
sequence numbers or source/SSRC restarts cannot establish such an epoch;
rebuild the whole receiver for a new session, since resetting jitter alone
does not clear other stages' histories.

Observer histories use bounded extended RTP sequence tracking across wrap;
count markers once, permit late recovery inside the retained window, and
keep older loss cumulative. Bound stream/stage/path/timing maps and use
constant-storage latency aggregates. Reject invalid metric scalars and
never count an unknown integrity result as success. Expose the inference
limits in topology instead of implying reconstruction of unseen stages.

Preserve modeled raster sizes in codec ratio accounting and keep them
distinct from bytes actually carried on the wire. Bound decoder slice
assembly and validate slice identity/count/index before reconstruction;
incomplete reconstructed frames must fail integrity.
The sink retains 4096 sequence identities, reports older arrivals outside
history without rewriting cumulative loss/order counters, and still checks
their integrity. Latency requires explicit finite aligned-clock source
capture time; a UDP receive timestamp alone is insufficient.
Represent unknown sink latency as null until the first valid explicit
sample, preserve measured zero, and retain valid aggregates when subsequent
frames omit capture time.

The CLI simulator delivers receiver reports to the sender after a
deterministic reliable reverse-link delay, with causal adjustment taps.
Both live and simulated report adapters use receiver inter-arrival jitter,
without requiring a shared source clock or inventing capture timestamps.
Measure both wire boundaries before receiver FEC/jitter, report known
injected drops independently of network duplication, and compare all known
source identities with sink integrity events to account for edge/all loss.
Simulate's displayed verdict uses probe's complete integrity gate.

Interpret MediaConfig/CLI MTU as the complete IPv4 path MTU: reserve
IPv4/UDP headers and reference trailer before internal packetization.
Standalone Packetizer's argument remains its RTP+fragment byte budget.

Bound simulator time, delay, tick interval, and cumulative work explicitly;
reject unsupported or nonprogressing virtual clocks instead of hanging.
Finite streams use idempotent ordered pipeline finalization, including
partial sender XOR parity, actual-time forced jitter drain, and incomplete
assembly diagnostics. Finished pipelines reject new pushes, have no
deadlines, and produce nothing from further ticks or feedback.

Prove delivery and independent observer fanout with real IPv4 multicast
on loopback. Skip only demonstrated unsupported/forbidden socket errors;
timeouts, missing packets, and generic I/O failures remain failures.
Exercise malformed input and teardown with deterministic tests.

Advertise the reference sender's payload as `framing: "yoke-reference-v1"`.
The external stream driver advertises/requires `framing: "standard-rtp"`;
it must reject the custom reference trailer rather than launching an
incompatible standard-RTP receiver based only on an encoding name.

## Scope and consequences

The pipeline remains a synthetic/model software reference. Passing the
tests establishes neither a real JPEG XS codec nor hardware throughput,
real-fabric performance, or physical glass-to-glass latency. YWP/1 and
control-plane pairing semantics are unchanged. The agent still never
touches media; ADR 0011's protected ciphertext relay remains point-to-point
and protected-content multicast is outside this reference.
