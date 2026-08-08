# 0005 — Control-frame authentication: HMAC-SHA256 pre-shared key

Date: 2026-08-08. Status: accepted.

Anyone who can send a datagram to the control group could otherwise
re-route streams. Options considered: mTLS/unicast mesh (rejected —
kills passive snooping and multicast semantics), asymmetric signatures
per device (deferred — provisioning burden without a controller to
enroll against), plaintext (rejected — fails "in the wild").

Accepted: one site-wide pre-shared key; every frame carries
HMAC-SHA256 over its canonical bytes. Replay is bounded by per-origin
strictly increasing sequence numbers persisted across restarts (ADR
0007). Control metadata stays cleartext by design so the snooper works
without touching endpoints; a snooper holding the PSK additionally
verifies signatures.

Threat accepted: a compromised endpoint holding the PSK can forge
intents. Rotation is manual (update key file, restart agents). Per-device
keys with an enrollment story are a v2 candidate and get an ADR of their
own if pursued.
