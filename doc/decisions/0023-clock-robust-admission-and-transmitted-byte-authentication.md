# 0023 — Clock-robust admission, bounded sequence floor and transmitted-byte authentication

Date: 2026-11-04. Status: accepted. Arose from a cold review of the
control plane. The envelope, frame types and reference sender bytes stay
**YWP/1**; every change below is a receiver or local-policy clarification
that existing YWP/1 senders already satisfy.

## Context

The Radxa ZERO 3E boxes have no battery-backed clock. After a PoE outage
they boot with the last saved time, often hours behind, and NTP corrects
them later — or never, on an isolated network. Four defects followed:

1. The agent captured `wall - monotonic` once at startup and derived every
   later future-sequence admission bound from it (ADRs 0010, 0012). An
   agent started two hours behind dropped every correct-clock peer frame
   as `stale` for its whole uptime, even after NTP stepped the clock. The
   telemetry receiver, snooper and CLI shared the flaw.
2. The restart floor was `max(saved, unix_now)`. One boot with a far-future
   clock (a bad RTC, an operator typo, a spoofed unauthenticated NTP reply)
   persisted a future reservation; every peer then dropped the node as
   future until real time caught up, and no recovery existed short of
   editing `state.json`.
3. Signatures covered a *re-serialization* of the parsed JSON defined by a
   Python `json.dumps` call. A third-party sender that formats a float with
   `%.17g` or escapes `é` as `é` signed its own bytes and failed `auth`.
4. Omitting `psk_file` silently ran unauthenticated open mode with only a
   stderr warning; any key length was accepted; the control socket bound
   `INADDR_ANY:9753` and so also accepted unicast datagrams.

Smaller findings: a consumer ignored its producer's announced `state` and
waited out a backoff of up to 30 s after a device replug; there was no
local log handler and multicast event detail (128 bytes) was the only
record of a driver failure; the spec omitted `driver_status`, event detail
shapes and the USB `host` forms the code accepts.

## Decision

**Admission re-reads wall time.** Every frame input to a node, snooper or
telemetry collector carries the current wall clock read by the shell at
receipt. The future bound `seq <= (floor(wall) + 3600) * 1e9` uses that
reading; without one the last supplied (or startup) estimate is used.
Timers remain purely monotonic, so a clock step never expires peers or
reschedules retries. A step forward recovers admission on the next frame
without a restart. Authenticated frames rejected only by this bound are
logged locally (rate limited) with their lead over the local clock, which
is the operator's evidence that this box's clock is behind.

The service orders itself `After=time-sync.target` without `Wants=` or
`Requires=`: when a site enables a time-wait unit the first announce
waits for synchronization, otherwise startup proceeds. Nothing in the
agent waits for, or fails on, an unsynchronized clock. While a box's clock
stays more than an hour behind its peers it still cannot admit their
frames — the accepted ADR 0007/0010 prerequisite — but its own frames
remain admissible because its sequence floor comes from durable state.

**The restart floor cannot be dragged far by the wall clock.** With a
nonzero durable bound `S`, the floor is `max(S, min(unix_now_ns, S +
SEQ_FLOOR_JUMP))`, `SEQ_FLOOR_JUMP` = 60 s. Sequence numbers advance one
nanosecond per frame, so 60 s of jump clears more than 10¹⁰ frames
emitted after `S` was saved; the durable bound alone already prevents
reuse. A single far-future boot therefore advances the floor by at most a
minute and stays admissible to every peer whose clock is sane. Repeated
restarts under a bad clock ratchet by at most a minute each; it takes 60
consecutive bad boots to reach the one-hour admission margin. A fresh
commission (`S = 0`) still uses the wall clock: commission only with a
synchronized clock. The capped amount is logged at startup.

The agent warns when the floor is inadmissible by its own clock: at
startup when `S` exceeds `(unix_now + 3600) * 1e9`, and at runtime
(rate limited) when its next sequence exceeds that bound for the current
reading. Either this clock is behind (peers with sane clocks still admit
the node, and the warning stops once time syncs) or an earlier boot or
release persisted a future reservation. The defined recovery for the
second case is `yoke agent --config FILE --rebase-sequence`, run once in
the foreground with the service stopped and the clock confirmed correct.
It takes the state lease, refuses unless the saved bound is beyond the
admission margin of the current clock, and atomically saves the bound
`(floor(unix_now) + 3600) * 1e9` with the pairing unchanged. No peer whose
clock agreed with this one can have admitted any value above that bound,
so the subsequent sequence numbers are fresh for them. No manual state
edit is involved, and nothing rebases automatically: a box cannot tell a
behind clock from a future reservation, and an automatic rebase would let
a spoofed-past NTP reply cause sequence reuse.

**Signatures bind the transmitted bytes.** A receiver verifies the HMAC
over the received datagram with its top-level `sig` member excised (the
exact rule and published vectors live in PROTOCOL.md and
`tests/vectors/ywp1-auth.json`). For compatibility it also accepts a
signature over the legacy canonical re-serialization. Reference senders
are unchanged: they emit canonical JSON with `sig` in sorted position, for
which both messages are identical. Third parties may format numbers and
escape strings as they like, provided they sign what they send. The
format check of `sig` still precedes any comparison (ADR 0010).

**Lab mode is explicit.** An agent configuration without `psk_file` must
set `open_mode = true`; setting both is an error. Keys shorter than 32
bytes are refused by the agent and the CLI (the recommended provisioning
is 64 hexadecimal characters). `yoke pair` and `yoke unpair` refuse to
send unsigned intents unless `--open` is given; passive `status` and
`snoop` still observe an open lab, with the existing warning. Control
sockets bind the multicast group address, so the kernel delivers only
datagrams addressed to the group; unicast to the host's port is never
read.

**Producer readiness edges trigger consumer retries.** When a paired
producer's announce moves from a non-`active` state to `active` while the
consumer is in `error` or `pending` (and no teardown is outstanding), the
consumer resets its backoff and reattaches immediately instead of
waiting for the scheduled retry. Producer state remains advisory: it never
causes a detach.

**Local logs carry full detail; multicast stays bounded.** The agent
configures one stderr handler (journald priority prefixes when
`JOURNAL_STREAM` is set). The core emits leveled local log effects for
driver state or detail changes, committed pairings, peer loss and return,
inadmissible-sequence conditions and drop counter changes, without the
128-byte truncation applied to `evt` detail and `driver_status`. Driver
error text puts the failure before the command line, so truncated wire
detail keeps the cause.

**Spec drift is resolved in PROTOCOL.md:** the optional announce
`driver_status` object, every normative event's detail shape, and the
USB/IP `host` forms (IPv4/IPv6 literal or DNS name, bounded by the
connection record) are now specified.

## Consequences

Agents, observers and the CLI require no coordinated upgrade: reference
senders already produce bytes valid under both verification messages, and
all clock changes are local. Configurations that relied on implicit open
mode, or on a key shorter than 32 bytes, fail fast with an actionable error
and must be updated. A sequence floor may now lag real time by more than a
minute after downtime; that is harmless because admission bounds only how
far a sequence may run *ahead*. Restoring an older state backup remains an
explicit operator recovery (ADRs 0012 and 0016); the capped floor does not
promise to clear values emitted by restarts after that backup.
Deterministic regressions pin each change: wall-clock steps and pre-NTP
boots in `tests/simnet.py`, the floor cap and rebase command, published
authentication vectors, open-mode refusal, destination filtering, the
producer-recovery retry and log emission.
