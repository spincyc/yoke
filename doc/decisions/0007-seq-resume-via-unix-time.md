# 0007 — Sequence-number resumption uses Unix time as the floor

Date: 2026-08-08. Status: accepted.

Replay defense needs per-origin strictly increasing `seq`. Persistence
can be lost (reflash, corrupt card). A node resuming with `seq = 1`
would be ignored by every peer until their tracked value ages out.

Accepted: on startup, `seq = max(persisted + 1, int(unix_time))`.
Frames are emitted well below 1/second on average, so `int(unix_time)`
always clears any previously used value once the clock is sane. Cost:
requires roughly sane wall clocks (NTP is already a deployment
prerequisite for PoE appliances); a node whose clock is far behind AND
whose state was lost stays muted until its clock catches up — detectable
via the snooper (its frames are counted as `stale` by peers, surfaced in
`rx-drop` events from every other node).
