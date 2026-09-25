# Yoke — handoff for the next agent

Date: 2026-09-24. Everything below is committed on `main`; `make verify`
is green (ruff + mypy --strict + pytest, 581 tests). Push is not
authorized unless the user asks.

## The goal (user, 2026-09-24)

A **fully decentralized, point-to-point, reconfigurable mesh of HDMI and
USB bridge devices over a switched IP network**, with a **switched 10GbE**
baseline, and **endpoint hardware as small as possible**. Deliverables
the user asked for: the hardware to buy, and the code driven to
completion. The first concrete link is a closet RTX 5090 gaming PC (4K120
floor, competitive-gaming latency) to a desk monitor + USB peripherals
including WinWing flight-sim gear.

**Verdict: possible** (`doc/research/FEASIBILITY.md`). Impossible only:
bit-exact 4K 4:4:4 at 60 Hz or above over 10G (physics) and HDCP in a DIY endpoint (law).
The leading 4K120-over-10G route carries the **GPU's own DSC stream**
compressed over DisplayPort, ~8 Gb/s, sub-ms, gated on Lontium NDA
silicon. An earlier session's "not possible" conclusion is settled
there; don't re-raise it without new evidence.

## Read in this order

| Doc | What it is |
|---|---|
| `doc/research/FEASIBILITY.md` | The verdict, the arithmetic, the 4K120 routes, corrections to earlier claims |
| `doc/design/EDGE-DEVICES.md` | Endpoint design: DSC-link video path, USB path, session-state ownership, milestones |
| `doc/research/HARDWARE.md` | Market survey (Sept 2026), shortlists, network rules, driver roadmap; ⚠ = bench-unverified |
| `doc/research/COMPRESSION.md` | Codec regimes and how each 4K120 route uses the compression dial |
| `doc/design/ARCHITECTURE.md`, `PROTOCOL.md` (normative), `doc/decisions/` | The control plane |
| `doc/design/MEDIA.md`, `MEDIA-TESTING.md` | The software media pipeline (reference model and test oracle) |
| `doc/design/DEPLOYMENT.md` | Running agents on a site |

Documents removed on 2026-09-24 as superseded (Aug 2026 lanes, build
plan, FPGA cost, 4K120 floor, platform pick, closet-KVM build spec,
sanity report) are retrievable with `git show 4e28967:<path>`.

## What exists

1. **Control plane** (`src/yoke/core`, `net`, `drivers`, `cli.py`,
   `snoop.py`): controllerless, sans-I/O pairing over signed UDP
   multicast, with a passive snooper. Proven offline, including seeded
   convergence properties and an adversarial review. Drivers: `sim`,
   `igmp`, `usbip`, `jap`, `stream`.
2. **Software media pipeline** (`src/yoke/media`): sans-I/O stages,
   wire codec, asyncio UDP runner, passive snooper, network simulator,
   `yoke-media` CLI. A correctness oracle, not a latency oracle: its
   loopback timing is Python overhead, not a system prediction.

Nothing has touched real endpoint hardware yet.

## Open decisions (the user owns these)

1. **The v1 trade.** On 10G, something gives: (a) staged — buy 4K60
   HDCP-capable commercial endpoints for the mesh now (controller-free
   only at 1G, Just Add Power; 10G ZeeVee needs a site server) and build
   the 4K120 DSC link in parallel; (b) 4K120 build only, unprotected; (c) 4K60 buy
   only; or (d) add 25G ports after all. The user asked to clarify before
   answering; **ask what they want to clarify first.**
2. **Desk monitor model:** connector and max mode; for route A it must
   accept DSC over DisplayPort.
3. **DisplayPort from the 5090:** acceptable (route A), or must the PC
   side stay HDMI (route B, Lontium re-encode)?
4. **Switch inventory:** which 10G SFP+ ports serve closet and desk, and
   can both land on one switch?
5. **Scope of the media invariant (ADRs 0002/0004).** `AGENTS.md` and
   the ADRs say no yoke code path or component may receive, inspect, or
   relay media payload. The `stream` driver honors that by running
   pipelines as separate processes, but `yoke.media` (runner, snooper)
   receives and inspects payload, and the DSC-link endpoint's gateware
   (`EDGE-DEVICES.md`) would relay it. Either amend the invariants' scope
   by ADR (the agent and control plane never touch media; yoke-designed
   media code and gateware may, on unprotected content only; HDCP stays
   forbidden) or keep yoke strictly a control plane and treat endpoints
   as a separate project. Needs the user's call before M4.

## Next actions

Independent of the open decisions (except as noted):

- **M1 brain:** model EDID/DPCD synthesis and the HPD hysteresis state
  machine sans-I/O with offline tests (`EDGE-DEVICES.md` §session state).
  Moot if the user picks (c), 4K60 buy only.
- **`vhusb` driver:** VirtualHere named-pipe client API behind the
  existing driver seam, offline-tested against scripted transcripts;
  the USB mesh seam.
- **Known races** (`ARCHITECTURE.md` §known limitations): single-thread
  driver executor with generation-tagged statuses.

Needs the user (purchases or external contact):

- **M2 USB bench** (~$150): NanoPi R76S + powered hub + VirtualHere
  trial; first question is USB3 DisplayLink (D1) over VirtualHere.
- **L2Tek/Lontium engagement** for LT6911GXD samples, firmware, and the
  question list in `EDGE-DEVICES.md`: the longest lead item. Moot under
  (c).

## Working notes

- Runtime is stdlib-only (ADR 0008); dev tools via the `dev` extra.
- `make verify` is the commit gate; `make media-recv` / `make media-send
  PEER=` are the two-host media commands.
- Git identity for this repo: "Yoke Project". Commit messages end with
  the Co-Authored-By line.
