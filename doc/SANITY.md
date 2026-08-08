# Yoke sanity check — what is done, what works, what remains

Date: 2026-08-08. This is an honest assessment for someone deciding
whether to trust yoke in the wild.

## What was asked, and what it became

The brief: extract a beginning USB/HDMI-over-network mesh from `telos`
into its own project, name it, and push the design to completion —
producer and consumer endpoints (HDMI/USB + Ethernet/PoE), any producer
re-pairable to any consumer, transparent transport (no HDCP in our
software), no always-on controller, a multicast debug snooper, hardware
research, production-grade code, everything offline-testable.

No such beginning existed in `telos` (searched every document, decision,
branch, and `telos-private`; recorded in ADR 0001). Yoke was built fresh
as a sibling repository.

## What is done

| Area | State |
|---|---|
| Naming | **yoke** — to pair two things; works as a CLI verb |
| Architecture | `doc/design/ARCHITECTURE.md` — control-plane/media-plane split |
| Wire protocol | `doc/design/PROTOCOL.md` — YWP/1, normative |
| Decisions | 10 ADRs including 2 from the review |
| Hardware research | `doc/research/` — 9-lane study + buy/avoid/impossible verdicts |
| Protocol core | `src/yoke/core/` — sans-I/O state machine + wire codec |
| Drivers | sim, igmp, usbip, jap (Just Add Power) + shellcmd seam |
| Shell | asyncio runner, multicast socket, CLI, passive snooper |
| Config/state | TOML config, atomic persisted state |
| Tests | 375 offline tests; simulated lossy network; loopback smoke |
| Verify gate | `make verify` = ruff + mypy --strict + pytest, all green |

~3,500 lines of runtime, ~5,100 of tests. Zero third-party runtime
dependencies (agents must run on small PoE boxes).

## What actually works (evidence)

- **The whole stack runs live.** A loopback end-to-end smoke started two
  real agents over real multicast, paired producer→consumer to ACTIVE,
  unpaired, and re-verified — with a passive `yoke snoop` capturing every
  frame including graceful `bye`s, transmitting nothing. Rejections
  (self-pair, role confusion) refuse in <1 s instead of burning the ack
  deadline.
- **Convergence is proved, not asserted by hand.** A deterministic
  simulation runs many real state machines over a virtual network that
  drops, duplicates, reorders, delays, and partitions from a seed. Every
  row of the failure model has a scenario; seeded property tests
  (seeds 0–99) confirm every consumer converges to the highest-version
  intent any node received, consumers stay independent, replay is
  idempotent, and the mesh goes quiescent.
- **The design self-corrected under adversity.** The simulation found a
  real livelock (verbatim anti-entropy re-sends stale-dropped once the
  author's sequence advanced); it was fixed at the protocol level
  (ADR 0009: re-issue under the relayer's own origin) with regressions at
  unit, mesh, and wire level.
- **An adversarial review hardened it.** A multi-agent review with
  independent verification surfaced 22 confirmed defects — including a
  critical one-packet unauthenticated DoS (a non-ASCII signature crashed
  every receiver). All were fixed with regressions (ADR 0010).

## Honest limitations — the gap between "green tests" and "in the wild"

**Hardware is unverified.** Not a single powered endpoint has been
touched. The `jap` driver encodes researched — not bench-confirmed —
assumptions about Just Add Power's command vocabulary and channel→group
mapping; they are isolated and executable so they are cheap to correct
after a bench session. The whole ⚠ queue in `doc/research/HARDWARE.md`
(UniFi IGMP snooping behavior per model/firmware, re-pair glitch
duration, HDMI/USB re-pair semantics, snooper line-rate capture) needs a
lab before any "works in the wild" claim about the *media plane* is
earned. What is proved is the **control plane**: discovery, pairing,
convergence, observability, and failure handling.

**Security model is pre-shared-key.** One site-wide key authenticates all
control frames (ADR 0005). A compromised endpoint holding the key can
forge intents; the DoS hardening (ADR 0010) caps the blast radius of
unauthenticated and buggy senders but does not make a key-holder
harmless. Per-device keys are the stated v2 answer.

**Two known-minor async races remain** (review findings, not yet fixed):
the driver-poll loop can publish a driver status captured between an
intent's application and its attach task running (a brief spurious
`media-down`/`media-up` blip that self-corrects on the next poll), and a
driver call's worker thread can outlive task cancellation. Both are
minor, self-correcting, and documented; the fix is a single-thread driver
executor with generation-tagged statuses.

**HDCP position is a well-supported reading, not a legal opinion**
(ADR 0004): control software that never touches keys or content needs no
HDCP license, matching universal industry practice — but a commercial
launch should get that reviewed.

## What I would do next, in order

1. Buy two Just Add Power endpoints and a UniFi PoE switch; run the
   `HARDWARE.md` ⚠ queue on the bench. This converts the media-plane
   assumptions from researched to proven.
2. Fix the two minor async races with a driver-operation FIFO.
3. Add an NMOS/IS-05 driver (`nmos`) to make the IPMX path real.
4. Decide per-device keys vs. accepting the PSK threat model for the
   deployment, and if the former, design the enrollment story (needs an
   ADR — it touches the no-controller principle).

## Bottom line

The control plane is production-shaped and thoroughly proved offline: it
converges under adversity with no central controller, it is observable by
a passive snooper, every error path has a deterministic test, and it
survived an adversarial review. It is **not yet proved against real
hardware** — that is the one gap between this and a system you can trust
in the wild, and it is gated on a bench, not on more code.
