# 0016 — Explicit provisioning and validated control inputs

Date: 2026-11-02. Status: accepted.

Missing durable state cannot identify a new endpoint versus a commissioned
endpoint whose state was lost. Ordinary agent startup therefore requires an
existing valid state file. A one-time foreground `yoke agent --commission`
invocation permits creation when absent, then follows the same durable
startup barrier as every restart. It never replaces corrupt or invalid
state. The service omits this option. Recovering lost state requires the
operator to stop the endpoint, investigate peer sequence/version history,
and explicitly choose recovery; automatic reset is forbidden.

The site PSK remains the accepted trusted-site model in ADR 0005, including
its key-holder forgery limitation. Empty PSK is unauthenticated laboratory
mode and is visibly reported at startup. Config and CLI use one key-file
reader: a private regular file, no final symbolic link, at most 4096 bytes,
with exactly one final newline removed. Explicit empty keys are errors.
Open mode must never be described as authenticated.

Validate IPv4 multicast group, unicast interface (or kernel-selected
`0.0.0.0`), port, and fixed control TTL before creating a socket. Socket
setup failure closes the descriptor. On Linux, disable IP_MULTICAST_ALL so
an unrelated group sharing the UDP port cannot enter this subscription.
The CLI exposes the same interface selection as an agent. Collection
windows must be finite, positive, and below the platform timeout limit;
node IDs use full syntax matching;
self-pairs, CLI/endpoint identity collisions, and exhausted versions fail
before an intent is sent. Use monotonic clocks for collection/retry timers
and a separate wall clock for frame timestamps.

An applied acknowledgment means the consumer durably committed the desired
pairing. CLI success says `committed`; activation is confirmed separately by
a matching announce. Status JSON is stable, sorted, and includes the
authentication mode; status and snoop remain receive-only.

Each new CLI operation receives a fresh unpredictable version author,
separate from its reusable envelope identity; retries reuse that complete
version. A delayed authentic acknowledgment for an earlier operation can
therefore never match a new operation merely because discovery observed
an older announce (the YWP/1 clarification in ADR 0012).

The final snoop snapshot receives the shell's current injected monotonic
time, even when shutdown follows complete network silence. Liveness and
cache expiry therefore advance without requiring another datagram. This
snapshot cannot send traffic or change an endpoint's pairing.

The service uses a dedicated unprivileged account and local state directory.
It does not wait on network-online or a management process: multicast can
converge after startup when the interface becomes usable. Driver-specific
privileges must be provisioned narrowly for the chosen endpoint; the sample
unit does not grant root to every driver.
