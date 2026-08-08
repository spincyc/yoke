# 0003 — Controllerless pairing: consumer-owned state, versioned intents

Date: 2026-08-08. Status: accepted.

No process may be required to run continuously for the mesh to work or
reconfigure. Options considered:

- **Central controller** (rejected: single point of failure, explicit
  requirement against it).
- **Distributed consensus (Raft etc.)** (rejected: needs quorum
  membership management; overkill — there is no shared datum, each
  consumer's pairing is independent).
- **Consumer-owned state + totally ordered intents** (accepted): each
  consumer is the sole authority for its own pairing. Intents are
  multicast, carry a Lamport-counter/origin-id version, and the highest
  version wins idempotently. Announcements replicate state for
  visibility; peers cache the newest intent per consumer and re-send it
  when they hear a consumer announcing older state (anti-entropy with
  suppression), so offline consumers converge on return.

Producers stay passive (HDMI encoders always transmit to their own fixed
multicast group; switching is decoder-side join/leave — the commercial
AVoIP pattern), which is what makes per-consumer independence sound:
pairing consumer C to producer P touches only C.
