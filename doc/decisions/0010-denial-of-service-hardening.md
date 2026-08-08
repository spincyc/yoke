# 0010 — Denial-of-service hardening of the frame and node layers

Date: 2026-08-08. Status: accepted. Arose from an adversarial review of
the v1 implementation.

Three unauthenticated- or single-frame denial-of-service vectors were
found and closed. All are "works in the wild" concerns, not lab
theatre.

1. **Non-ASCII signature crash (critical).** `decode()` validated only
   that `sig` was a string, then called `hmac.compare_digest(sig,
   expected)`, which raises `TypeError` on a non-ASCII str operand.
   Every caller catches only `FrameDrop`, so one crafted datagram — in
   open OR keyed mode, since the crash precedes verification — took down
   the receive loop of every agent and the snooper. Fix: `sig` must
   match `^([0-9a-f]{64})?$` (SHA-256 hex, or empty in open mode) before
   the compare; anything else drops as `auth`. A non-str `sig` remains
   `malformed`.

2. **Spoofed sequence high-water (major).** A frame carrying a victim's
   origin id and an absurd seq poisoned every receiver's per-origin
   replay high-water, stale-dropping the real node's later frames until
   each peer rebooted. Fix: sequence numbers are Unix nanoseconds
   (ADR 0007), and a frame whose seq runs more than `SEQ_FUTURE_MARGIN_S`
   (1 h) ahead of the receiver's clock is rejected without advancing the
   mark. This also forced the seq unit to be consistent between agents
   (`int(now) * 1e9`, exact integer math) and the CLI (`time.time_ns()`).

3. **Unbounded per-origin maps (major).** `_replay`, `_peers`,
   `_rejected`, and `_intent_cache` are keyed by attacker-choosable node
   ids and were never pruned; a device cycling ids exhausted memory. Fix
   (`_prune`, run each poll): forget peers lost/departed longer than
   `PEER_FORGET` (5 min) and FIFO-cap every map at `MAX_TRACKED` (4096),
   never evicting the currently paired producer.

Residual, accepted: a PSK-holding device is already trusted to forge
intents (ADR 0005), so these bounds harden against unauthenticated and
buggy senders and cap the blast radius of a compromised one; they do not
make a key-holder harmless. Per-device keys remain the v2 answer to that.
The seq bound assumes NTP-sane clocks, already a deployment prerequisite.
