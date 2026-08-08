# 0007 — Sequence-number resumption uses Unix time as the floor

Date: 2026-08-08. Status: accepted.

Replay defense needs per-origin strictly increasing `seq`. Persistence
can be lost (reflash, corrupt card). A node resuming with `seq = 1`
would be ignored by every peer until their tracked value ages out.

Accepted: sequence numbers are **Unix nanoseconds**. On startup,
`seq = max(persisted + 1, unix_nanoseconds)`. Agents compute the floor
as `int(now_seconds) * 1_000_000_000` with integer math (exact — a
float `now * 1e9` would lose precision past 2⁵³); the CLI uses
`time.time_ns()` directly. Nanosecond resolution means even a burst of
frames from one origin never collides, and the floor always clears any
previously used value once the clock is sane.

Cost: requires roughly sane wall clocks (NTP is already a deployment
prerequisite for PoE appliances); a node whose clock is far behind AND
whose state was lost stays muted until its clock catches up — detectable
via the snooper (its frames are counted as `stale` by peers, surfaced in
`rx-drop` events from every other node). A companion bound (ADR 0010)
rejects frames whose seq runs more than an hour ahead of the receiver's
clock, so a spoofed nanosecond high-water cannot mute a real origin
permanently.
