# Build spec: transparent full-bandwidth PC-gaming KVM (closet → desk)

Status: active build target, 2026-08-09. This is the **first concrete
build** the user committed to, after ruling out commercial gear. Read
`HANDOFF.md` first for the project overview, then this. A cold agent
should be able to resume from here.

## The job

A gaming PC lives in an A/V closet. A monitor + USB peripherals (keyboard,
mouse, headset, maybe more) live at a desk some distance away. Deliver:

- **Full bandwidth** — target 4K120 (confirm the monitor's exact max mode
  and connector; see Open Decisions).
- **Lowest latency** — competitive gaming; must feel like sitting at the
  PC (single-digit ms added, ideally none).
- **Transparent** — the PC and the monitor/peripherals behave as if
  directly connected.
- **Reliable** — stays live for days without intervention.
- **Unprotected content** — a PC's game/desktop output asserts no HDCP, so
  **HDCP is out of scope for this build** (it only returns if DRM playback
  is added later; see `4K120-FLOOR.md`).

This is the first link. The broader yoke vision (many endpoints, dynamic
any-to-any routing over the switched network) comes later; do not let it
complicate milestone 1.

## Why commercial gear failed (do NOT rebuild these failures)

The user has been down the buy road; fiber HDMI/USB extenders and KVMs
failed with two specific, diagnosable symptoms:

1. **USB: constant disconnect/reconnect, wedges over days, needs a
   physical replug.** Diagnosis: the extender **relays USB timing** across
   the link with **no supervisor**. USB is a tightly-timed, host-polled
   bus; when the link drifts or a keepalive is missed the enumeration
   state wedges and never auto-recovers. Selective-suspend makes it worse.
2. **Video: flicker, black, "takes several minutes to stabilize," if at
   all.** Diagnosis: an **EDID/HPD handshake + FRL link-training**
   problem, NOT bandwidth. The extender doesn't hold a stable EDID to the
   GPU or manage the Hot-Plug-Detect line, so the GPU re-probes and
   re-trains the link repeatedly; passing a marginal 48 Gb/s analog FRL
   signal end-to-end through a cheap AOC fails training intermittently.

**Common root cause:** cheap extenders **passively relay marginal
high-speed signals and do not own the session state** (EDID/HPD for video,
enumeration for USB). That is exactly what a built endpoint fixes and a
sealed box cannot.

## The build principle (fixes both failures)

1. **Terminate + regenerate, never relay.**
   - HDMI: a **Lontium FRL bridge (~$15–25)** receives the PC's 4K120,
     equalizes, and outputs *clean pixels*; regenerate a *clean* FRL
     signal at the monitor (Lontium TX bridge or FPGA HDMI 2.1 TX). No
     marginal 48 G analog crosses the run → no link-training roulette.
   - USB: a **real USB host at the desk** (locally powered, locally
     enumerated) and a **real USB gadget at the closet** presenting the
     devices to the PC. USB timing never crosses the link → it cannot
     drift out of sync.
2. **Own the session state.**
   - Cache the monitor's real EDID; present a **stable EDID to the PC at
     all times**; drive **HPD deterministically** (assert only when the
     link is ready, never bounce it). The GPU locks one mode instantly and
     never re-probes → no flicker/black/minutes.
   - USB: an **auto-reconnect watchdog** that re-enumerates the instant a
     session hiccups → stays live over days, no replug.
3. **yoke is the supervisor.** The already-built control plane (persistent
   pairing, keepalives, auto-reconvergence, never-wedge) is the brain the
   dead-overnight extenders lack. Each endpoint runs a yoke agent.

## Architecture

```
CLOSET endpoint                         DESK endpoint
  PC HDMI 2.1 out                         monitor HDMI 2.1 in
    -> Lontium FRL RX (EDID/HPD mgr)        <- Lontium/FPGA FRL TX (clean regen)
    -> pixel handling (FPGA/SBC)            <- pixel handling
    -> TRANSPORT  ============================>  TRANSPORT
  PC USB host                             desk peripherals
    <- USB gadget (closet presents devices) <- USB host (real enumeration)
  yoke agent  <----- control channel ----->  yoke agent
```

Transport = **DECIDED 2026-08-09: the switched UniFi network. The user
ruled out point-to-point fiber.** The full path designs, part selections,
and network verification checklist live in `EDGE-DEVICES.md` (which
supersedes this section's transport language). A new requirement arrived
with the decision: the USB mesh must carry **WinWing flight-sim gear,
including their USB3 DisplayLink instrument displays** — see
`EDGE-DEVICES.md` §"The full USB path".

## Milestones (strict order — HDMI handshake first, it's highest leverage)

- **M1 — HDMI stable handshake.** Lontium FRL RX + EDID cache + HPD state
  machine + clean regen. **Success: instant lock, zero flicker/black,
  stable over days at 4K60; then push to 4K120 FRL.** Lock the handshake
  before chasing top bandwidth.
- **M2 — USB robust.** Desk-side host + closet-side gadget (dwc3) + the
  auto-reconnect watchdog. **Success: keyboard+mouse survive forced link
  drops without a replug; live over days.**
- **M3 — Integrate** both behind a yoke pairing (the agents already exist).
- **M4 — (later)** move from dedicated fiber to the UniFi network transport
  if desired; then scale toward the multi-endpoint mesh.

## Parts for milestone 1 (estimates — RE-VERIFY, web budget was exhausted)

- **Lontium LT6911GX/GXD** FRL RX bridge (~$15–25 chip; eval board
  ~$150–400) — the piece that lets us own the signal path.
- **HDMI 2.1 TX** for regen: a Lontium TX bridge, or an FPGA HDMI 2.1 TX.
- **Compute for pixels + EDID/HPD + transport:** an FPGA with GTY (25G) if
  going uncompressed 4K120 over network (`FPGA-COST.md`: KU5P-class, but
  for a dedicated-fiber point-to-point the requirement is lighter), or an
  SBC for lower tiers. Confirm against the transport decision.
- **USB:** an RK3588/Pi-class board for host+gadget (dwc3 ConfigFS), or
  fold into the FPGA/SoC.
- **Transport media:** dedicated fiber (SFP+/SFP28 + DAC/optics) or the
  UniFi fabric.

Cross-references: `doc/research/prototype/4K120-FLOOR.md` (ingest = Lontium;
4:2:2-10 fits 25G uncompressed license-free; 4:4:4 needs 100G or DSC),
`FPGA-COST.md` (cheapest GTY silicon; surplus cards are a trap),
`BUILD-PLAN.md` (the phased picture), `HARDWARE-PLATFORM.md`.

## What is already built (the foundation to build on)

- **yoke control plane** (`src/yoke/core`, `net`, `drivers`, `cli.py`,
  `snoop.py`): controllerless pairing, passive snooper, ~600 tests,
  `make verify` green. **This is the supervisor for M3.**
- **Software media pipeline** (`src/yoke/media`): sans-I/O stages, snooper,
  metrics, asyncio runner, `yoke media` CLI, network simulator. The
  reference/test model and offline oracle for the transport; and the
  place to model the EDID/HPD and USB-watchdog state machines sans-I/O
  before touching hardware.
- **`stream` driver** (`src/yoke/drivers/stream.py`): the control-plane →
  media seam.

## Open decisions (user owns; confirm before spending)

1. **Transport: RESOLVED 2026-08-09 — switched UniFi network** (user
   decision; no point-to-point fiber). See `EDGE-DEVICES.md`.
2. **"Full bandwidth" exact target + monitor:** still open — get the
   monitor's exact model (max mode + connector, HDMI 2.1 vs DP). It picks
   the TX-regen tier in `EDGE-DEVICES.md` and the 4:2:2-vs-4:4:4 format
   tier.
3. **HDCP:** out of scope for this build (PC output unprotected). Only
   returns if DRM playback is later required.
4. **Switch inventory (new):** which UniFi models/firmware sit between
   closet and desk, and whether both rooms can land on one 25G-capable
   switch — see `EDGE-DEVICES.md` §network.

## Immediate next actions for a cold agent

1. Get the two open answers from the user (transport; monitor spec).
2. Model the **EDID-management + HPD state machine** sans-I/O (mirror the
   yoke.core style; testable offline) — this is the M1 brain and the
   single highest-leverage fix.
3. Model the **USB host/gadget + auto-reconnect watchdog** the same way.
4. Source M1 parts (Lontium eval board + compute); re-verify all prices.
5. Bench M1: rock-stable 4K60 handshake → 4K120 FRL. Only then M2.
