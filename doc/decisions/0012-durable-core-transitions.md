# 0012 — Durable transition feedback and reserved outbound sequences

Date: 2026-11-02. Status: accepted for the software implementation pass.

Keep the YWP/1 multicast envelope, site-PSK authentication, Lamport intent
ordering and independent consumer pairing. Correct implementation defects
without silently introducing the proposed YWP/2 source-authority protocol.
No per-device authorization, generic physical USB proxy or hardware timing
claim follows from these software changes.

The prior runner swallowed save errors while the core had already installed
the candidate pairing, allowing a successful ACK and later duplicate ACKs
for a change absent from disk. A `Persist` effect now carries a request id.
The core stages the candidate; only matching successful persistence feedback
installs it and releases driver/ACK effects. Startup is also gated. The shell
serializes core inputs across this barrier; failed saves retain committed
configuration. A temporary storage failure must not manufacture success.

Previously only the pre-send sequence was saved, and the Unix-second floor
allowed the same startup frames to be reissued after a same-second crash.
Persist `seq` as a reserved upper bound, allocate 4096 values before using
them, and restart above the saved bound. This uses the existing nanosecond
wire sequences and state shape. Persisting a block avoids a write for every
heartbeat. A reservation may be skipped, never reused; exhausting it requires
another durable barrier before more frames are released.

Timers use monotonic injected time and a separate injected startup wall time
provides the sequence floor/future bound. Wall-clock-dependent admission,
volatile/pruned receiver replay state, explicit state-loss recovery and
site-key-holder forgery remain accepted YWP/1 limitations. These changes do
not establish distributed USB ownership or rollback-resistant identity.

State parsing must reject invalid types and ids rather than coerce them.
Atomic-save durability means complete writes and file fsync, replace, then
directory fsync; unique exclusive private temporary files prevent writer
collision. Refuse symlinks/nonregular files and never reset corruption.
Hold a POSIX nonblocking exclusive advisory lock on a stable private
`<state>.lock` for the runner lifetime; never unlink the lock on release.
This prevents concurrent reservations against one state file, not duplicate
identities deliberately configured against different paths. It is unrelated
to pairing validity and does not add expiry or central authority.

Keep the core sans-I/O and runtime imports stdlib-only. New failure behavior
is pinned with deterministic core, codec and simulated-network regressions;
driver/shell tests independently exercise actual effect sequencing.

Round-one review found that failed unpair teardown was suppressed as a stale
unpaired status, and repeated error polls could continually postpone retry.
Track requested teardown until idle, including after restart of a consumer
with a nonzero version, and preserve deadlines across repeated unhealthy polls.
An unavailable candidate still requires old-route cleanup. Pending backend
drift also reconciles with bounded backoff. ACK body types are validated
before membership checks so authenticated malformed JSON cannot crash the
state machine. Error announcements budget human detail around intact opaque
media parameters and reject descriptions whose mandatory fields cannot fit.

Final cold review found that unknown tolerated intent extensions could make
a relay's outbound frame unencodable, and changed producer descriptions
could retain old active evidence. Cache/reissue only defined semantic intent
fields, and invalidate readiness before descriptor-driven attachment.
Client operation versions use fresh unpredictable author ids so stale
announcements cannot cause new operations to accept old version-only ACKs.
Observation remains conservative when legacy clients reuse conflicting
versions; escaping terminal controls and applying node-equivalent sequence
bounds makes multicast diagnostics safe to inspect without changing payload
or pairing authority.
