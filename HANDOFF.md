# Yoke — handoff for the next agent

Date: 2026-08-09. Read this first, then `README.md`, `doc/design/`, and
`doc/research/prototype/`. Everything below is committed on `main`;
`make verify` is green (ruff + mypy --strict + pytest, ~600 tests).

## ►► ACTIVE BUILD (start here): `doc/design/BUILD-SPEC-CLOSET-KVM.md`

The user committed to a **first concrete build**: a transparent,
full-bandwidth (target 4K120), low-latency **PC-gaming KVM from an A/V
closet to a remote monitor + USB peripherals**. Commercial extenders
failed them with two diagnosed symptoms — **USB wedges over days / needs
replug** (relayed USB timing, no supervisor) and **video flicker/black/
minutes-to-stabilize** (EDID/HPD + FRL link-training, not bandwidth). The
build fixes both by **terminating + regenerating** the signals (Lontium
FRL bridge; real USB host+gadget) and **owning the session state** (stable
cached EDID + deterministic HPD; USB auto-reconnect), with **yoke as the
supervisor** that keeps it live over days. Build order: **HDMI handshake
first (M1)** → USB (M2) → integrate behind yoke (M3). Content is
unprotected (PC output) so **HDCP is out of scope here.** Two open
decisions block sourcing: transport (dedicated fiber, recommended, vs
UniFi network) and the monitor's exact max mode + connector. Full detail,
parts, and milestones are in the build spec. **This supersedes the earlier
"which resolution/protected-scope" open items for the immediate work.**

## What yoke is

Two planes:

1. **Control plane** (`src/yoke/core`, `net`, `drivers`, `cli.py`,
   `snoop.py`) — a controllerless, sans-I/O state machine that pairs
   producer↔consumer endpoints over signed UDP multicast, with a passive
   snooper. Proven, thoroughly tested. This is the finished foundation
   and should not need rework. Normative: `doc/design/PROTOCOL.md`,
   `doc/design/ARCHITECTURE.md`, ADRs `doc/decisions/0001–0010`.

2. **Media plane** (`src/yoke/media`) — a software AV-over-IP pipeline:
   sans-I/O stages (source → codec → ratecontrol → packetize → fec →
   jitter → sink), a wire codec, an asyncio UDP runner, a passive
   white/black-box snooper + metrics, a seeded network simulator, and the
   `yoke media` debug CLI. It runs end-to-end over real loopback sockets
   (bit-exact, ~8 ms — but see the LATENCY caveat below). Normative:
   `doc/design/MEDIA.md`, `doc/design/MEDIA-TESTING.md`.

The media plane plugs into the control plane as the `stream` driver
(`src/yoke/drivers/stream.py`), same seam as `igmp`/`jap`/`usbip`.

## The requirements, as they evolved (READ — they changed a lot)

The user pushed the target progressively harder. Current understanding:

- Transparent HDMI (and USB) over their **switched UniFi 2.5/10/25G**
  network — endpoints behave "as if right next to each other."
- **Pin-to-pin**: the product is a transparent line-duplicator —
  TMDS pins → gateware → RTP over the wire → gateware → TMDS pins. NOT a
  software/GStreamer datapath (that's prototype scaffolding only).
- Works with **ALL sources incl. HDCP-protected** (Apple TV, Shield,
  Sony TV, RTX 5090), pro-sumer / A/V-snob quality.
- **4K120 is the absolute floor** (stated late; see the ⚠ below — it
  dominates the hardware answer and was under-addressed before this
  handoff).
- Cheap to prototype, no licenses to start; but must *ultimately* reach
  the above. Everything offline-testable; snoop-verifiable without
  interfering; robust first-class debug tools. High code quality.

## ⚠ The 4K120 requirement changes the hardware conclusion — resolve this first

Earlier hardware research targeted 1080p60 and found a cheap FPGA floor
(~$130–195/endpoint, Gowin Tang Mega — `doc/research/prototype/FPGA-COST.md`).
**That does NOT reach 4K120.** The honest numbers:

| 4K120 format | Uncompressed bitrate | Fits |
|---|---|---|
| 4:2:0 8-bit | ~11.9 Gb/s | 25G |
| 4:2:2 10-bit | ~19.9 Gb/s | 25G (thin) |
| 4:4:4 10-bit | ~29.9 Gb/s | needs 40G+ **or** ~1.5:1 light compression |
| 4:4:4 12-bit | ~35.8 Gb/s | needs 40G+ or compression |

Implications, all pointing away from the cheap tier:
1. **Ingest:** a 5090 outputs 4K120 as **HDMI 2.1 FRL (up to 48 Gb/s)**.
   The cheap boards' DVI/HDMI-1.4-class receivers cannot ingest FRL. You
   need an **HDMI 2.1 FRL receiver** — licensed Xilinx HDMI 2.1 IP (paid)
   or a dedicated FRL RX chip.
2. **Transport:** 4K120 needs **25G+ (GTY-class) transceivers**, and for
   4:4:4 either 40G+ or a **light visually-lossless codec** (JPEG-XS/DSC
   — commercial IP; there is NO free FPGA compression core, confirmed).
3. **Silicon:** this is **Virtex UltraScale+/Versal ($k-class) FPGA + IP
   licensing**, or the **SDVoE / IPMX / licensed-HDMI-2.1 product path**,
   not a $175 Gowin board.
4. **HDCP 2.3** on top if protected content is in scope (Apple TV/Sony).

**So prototype and product are now cleanly separated by format:**
- **Prototype (cheap, now):** 1080p60 on a Gowin Tang Mega (~$175/endpoint)
  or Kintex-7 (~$500) to prove the *architecture* — pin-to-pin TMDS
  gateware + RTP transport + the yoke control plane — on unprotected
  sources. This validates everything except the 4K120 format and HDCP.
- **Product (4K120):** HDMI 2.1 FRL RX + 25G+ GTY FPGA + JPEG-XS/DSC light
  compression + HDCP 2.3 — the licensed, $k-class tier. **Next research
  task:** **DONE 2026-08-09 → `doc/research/prototype/4K120-FLOOR.md`.**
  Transparent 4K120-over-IP is bleeding-edge (nothing commercial ships it;
  all AVoIP caps at 4K60/HDMI2.0/10G). The problem reduces to **HDMI 2.1
  FRL ingest = Lontium** (LT6911GX/GXD ~$15-25/chip, only merchant 4K120
  FRL RX) or Bitec FMC (€595)+paid IP; letting Lontium do FRL+DSC decode
  avoids the paid AMD/HDCP IP. Floors/endpoint: **cinematic 4:2:2-10
  ~$1,650-2,000 license-free** (fits 25G uncompressed), any-source 4:4:4
  ~$2,500-4,500 (100G or self-DSC), protected product $0.5-2M+licensing,
  12-24 mo. The live open decision is now **which "4K120 floor" the user
  means** — cinematic HDR vs bit-exact-any-source vs protected-product — a
  10x cost lever.

## Latency: how to measure it honestly

The Python/loopback ~8 ms number is **Python + asyncio overhead, not a
system latency prediction** — do not quote it as such. The Python pipeline
is a **correctness oracle** (protocol, FEC, jitter, integrity, snoop), not
a latency oracle. To approximate real latency: benchmark the actual
(already-native) tools — GStreamer/MTL/SVT-JPEG-XS on x86 — or, for the
product, characterize the FPGA gateware in HDL simulation + on the bench.
Don't rewrite the pipeline in C for latency; measure the real components.

## Key design conclusions (with pointers)

- **Transport:** on 10/25G, uncompressed RTP (RFC 4175 / ST 2110) is
  bit-transparent, zero-license, sub-line latency; for what doesn't fit,
  the **compression sweet spot** is line-based, visually-lossless, ~2.5–4:1,
  sub-1 ms, with FEC funded by the saved headroom + an adaptive rate
  controller. `doc/research/prototype/COMPRESSION.md`.
- **HDCP:** "both ends unaware" of protected content = a **licensed
  repeater** (terminate + re-originate HDCP); there is no legal unlicensed
  transparent path, and the 7 ms locality check + the bandwidth/clock wall
  make a dumb relay impossible. HDCP has **no per-unit royalty** but a real
  compliance/robustness gate. ADR 0004; `BUILD-PLAN.md` §HDCP fork.
- **USB:** HID/mass-storage tunnel (usbip/VirtualHere); isochronous
  (webcams/audio) can't survive TCP → carry as media, not passthrough USB.
- **Prior-art shortcut** (prototype only): Sunshine + `moonlight-embedded
  -platform rk` is a finished network→HDMI consumer on RK3588.

## What is done vs. open

Done: control plane; software media pipeline + all stages + snooper +
metrics + runner + CLI + `stream` driver; offline sim + real-socket
loopback tests; two-host `make media-send/recv` targets; the compression,
HDCP, hardware, and FPGA-cost research docs.

Open decisions (the user owns these — none resolved yet):
1. **Prototype resolution** — 1080p60 (validate architecture cheaply) as
   the stepping stone to the 4K120 product? (Recommended: yes.)
2. **Protected content (HDCP) in v1?** — gates all licensing.
3. **FPGA as product, or x86+MTL just to get real latency numbers?**
4. **Keep the GStreamer/SBC de-risking step, or go straight to FPGA?**
5. **The 4K120 hardware combo** — needs the focused research above.

Immediate next actions for the next agent:
- Run the **4K120 hardware search** (HDMI 2.1 FRL ingest + 25G FPGA/chipset
  + JPEG-XS/DSC; SDVoE-2000 vs IPMX vs from-scratch FPGA cost).
- If the user greenlights a 1080p60 prototype: spec the exact Gowin Tang
  Mega 138K Pro BOM and the gateware plan (litevideo/verilog-ethernet +
  RTP framer), and bench-pilot the Gowin DVI-Rx capture margin first.
- Optionally: implement the real `stream`-driver codec behind the sans-I/O
  seam (SVT-JPEG-XS via MTL on x86) and a glass-to-glass latency rig.

## Working notes

- Runtime is stdlib-only (ADR 0008); dev tools via the `dev` extra.
  `make verify` is the gate. `make media-recv` / `make media-send PEER=`
  are the two-host commands; `yoke-media` is the debug CLI.
- Git identity for this repo: "Yoke Project". Commit messages end with the
  Co-Authored-By line. Push is not authorized without the user asking.
- The session repeatedly hit `ENOSPC` on the tmpfs holding
  `/tmp/claude-1000/.../tasks/*.output`; clearing those consumed agent
  transcripts frees it.
