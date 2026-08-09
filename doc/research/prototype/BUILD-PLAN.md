# Yoke prototype hardware: how to verify the design cheaply, and how it grows into a pro-sumer product

Status: research synthesis, 2026-08-08. Sources and per-lane detail in
`lanes/`. This document answers: *how do we build endpoints we can test
locally, with no licenses, that ultimately become a product working with
every HDMI source and display at A/V-enthusiast quality?*

## The two forcing facts

Everything below follows from two numbers and one law.

1. **Bandwidth.** Uncompressed video is `W×H×fps×bpp`. 1080p60 RGB ≈
   **3 Gbps**; 4K60 4:2:2-10bit ≈ **10–12 Gbps**; 4K60 RGB ≈ **15–18
   Gbps**. (Active-pixel rates; ST 2110 sends active pixels only.)
2. **Your fabric.** You have 2.5/10/25G UniFi. So: **1080p60 fits an
   uncompressed 10G link with 3× headroom, and 4K60 fits an uncompressed
   25G link.** No codec, no license, bit-exact — this is literally what
   broadcast ST 2110 does.
3. **The law: HDCP.** Apple TV, an Nvidia Shield, a Sony TV playing
   Netflix, 4K Blu-ray — all assert HDCP 2.2/2.3 on protected content.
   There is **no legal, unlicensed way** to carry protected content
   transparently. Every cheap capture front-end (USB dongle, TC358743,
   LT6911, even the RK3588's HDMI-in) outputs **black** on protected
   content by design. Carrying it requires being a **licensed HDCP
   repeater** (DCP LLC adopter agreement + certified silicon/IP), exactly
   like SDVoE/Just Add Power/HDBaseT. Stripping HDCP is illegal
   (DMCA §1201). See §"The HDCP fork".

The consequence that shapes the whole plan: **the prototype and the
product differ in exactly one block — the media front-end.** Unprotected
sources (a PC, a 5090 gaming, consoles in game mode, cameras) do **not**
assert HDCP, so a DIY-capture endpoint proves the entire system today.
The product swaps DIY-capture for licensed-repeater silicon and keeps the
network, transport, sync, and yoke control plane bit-identical. Architect
so nothing else is throwaway.

## The transport insight (this is the "outside the box" answer)

The instinct to "interpret the HDMI signal, send it, reproduce it" is
right — and on a fast fabric the cleanest form is **don't compress at
all**. Send the raw pixels as RTP (RFC 4175 / SMPTE ST 2110-20). It is:

- **bit-transparent** (no codec artifacts — A/V-snob-proof),
- **zero-license** (no patents),
- **sub-line latency** (no frame buffering in the codec),
- and it **fits your network** at the tiers above.

The one cell where uncompressed doesn't fit (4K60 high-color-depth on a
10G link) has a **royalty-free** answer: **VC-2 / Dirac Pro** (BBC-origin,
SMPTE VC-2, the LD profile is explicitly patent-free), ~4:1 visually
lossless at a few lines of latency, via FFmpeg or an FPGA core. JPEG-XS is
technically superb and even has free code (Intel SVT-JPEG-XS + GStreamer
elements) but is **patent-pool-encumbered** (intoPIX/Fraunhofer/Vectis) —
fine to experiment with, not "license-free." DSC is the wrong layer (a
display-link codec with no IP packetization). H.265 is lossy *and* the
most royalty-heavy — reserve it for bandwidth-starved links only.

| Content × link | 2.5G | 10G | 25G |
|---|---|---|---|
| 1080p60 | uncompressed 4:2:2 (tight); VC-2 for RGB/10-bit | **uncompressed RGB — the sweet spot** | uncompressed, trivial |
| 1440p60 | VC-2 ~4:1 | uncompressed | uncompressed |
| 4K60 | VC-2 / H.265 | uncompressed 4:2:0; **VC-2 for 10-bit/RGB** | **uncompressed RGB — clean transparent 4K** |

Practical GStreamer note: uncompressed RTP breaks on **packets/sec**
before bandwidth — enable **jumbo frames (MTU 9000)**, use GStreamer
≥1.18 buffer-lists, and for 4K expect to want a hardware/offload NIC
(DPDK/ST 2110-class) rather than pure software packetization.

## The latency truth (what "right next to each other" costs)

A locally-attached monitor is **not** zero latency — it's already ~1
frame of scanout + input polling. "Feels local" means *added* latency
below the task's just-noticeable difference: **<~20 ms added for
desktop/KVM, <~30 ms for gaming; for pure video, latency is irrelevant
and only A/V lip-sync matters** (keep within +45/−125 ms).

| Path | Realistic glass-to-glass @1080p60 | Feel |
|---|---|---|
| **USB-UVC dongle + SBC** | 50–130 ms | usable, not transparent |
| **SBC, native HDMI-in + HW H.265 low-latency** (RK3588) | ~35–55 ms | very good KVM/AV; a careful user detects it |
| **FPGA, line-based, uncompressed or VC-2/JPEG-XS** | **~5–16 ms** | genuinely cable-like |

The SBC floor is dominated by ~16.7 ms of unavoidable full-frame capture
buffering plus encode+decode+jitter. To reach the gaming/"cable" bar you
need **line-based capture + FPGA transport**. This is the core reason the
product tier is an FPGA (or licensed-repeater-ASIC) design, not an SBC.

## Seamlessness is more than pixels

To behave "as if right next to each other" the endpoint must also:

- **Proxy EDID/HPD.** The consumer reads the real display's EDID and
  ships it upstream; the producer presents it to the source and asserts
  HPD only after it's loaded — so the source targets the display's native
  mode. Getting this wrong = blank screen or HDCP re-auth flicker. On
  Linux this is I²C/DDC EEPROM plumbing (TC358743 exposes it;
  `edid-decode` to author/validate). On FPGA it's a DDC I²C slave backed
  by block-RAM EDID.
- **Carry audio in sync.** De-embed HDMI audio (I²S→ALSA on bridges, UAC
  on dongles), transport as RTP (muxed with shared timestamps, or AES67
  L16/L24 for the pro build), and **adaptively resample at the sink** —
  the audio clock and pixel clock are independent and will drift.
- **Present tear-free.** Producer and consumer clocks free-run and drift.
  The pragmatic homelab genlock is **VRR/FreeSync on the consumer
  display** (present each frame as it arrives; the panel stretches its
  refresh) plus a small adaptive jitter buffer with frame-repeat
  fallback. True genlock = PTP/ST 2059, which ordinary UniFi can't clock
  (see network §).

## USB: carry the payload, not the bus

Transparent arbitrary-USB-over-IP is a genuine hard limit, not a tooling
gap. HID (keyboard/mouse) and mass-storage tunnel cleanly over **usbip**
(free) or **VirtualHere** ($49/producer). **Isochronous** transfers
(webcams, USB audio) structurally **cannot** survive a TCP hop —
microframe timing is lost; you get "works for a minute then artifacts."
The fix that matches this project: **don't pass the webcam as USB — carry
it as a media stream** (capture UVC locally, send as video; on the far
side present a UVC *gadget* fed by the decoded stream). Use usbip/
VirtualHere only for HID + mass-storage; RK3588/Pi do USB **gadget mode**
(dwc3 ConfigFS) to present HID/UVC/mass-storage downstream.

## The network (your UniFi fabric)

Your fabric carries this; the risk is containment, not capacity.

- **Small mesh → prefer unicast.** For a handful of endpoints, unicast
  sidesteps the entire IGMP-snooping/querier flood-risk surface and is
  simpler and more robust. Use multicast only for true one-to-many
  (a source to many displays); then pin exactly one IGMP querier, enable
  fast-leave, and test containment before trusting a 12 Gbps flow.
- **Dedicated AV VLAN**, **jumbo frames (MTU 9000; UniFi caps 9216)** on
  every NIC in the path, DSCP EF/CS4 marking, per-port multicast
  rate-limit as a blast-radius cap.
- **Endpoint link speed sets the ceiling:** 1080p uncompressed → 10G is
  huge headroom; 4K uncompressed → **25G SFP28 at the endpoint** (your
  fabric has it) or light compression to stay on 10G.
- **PTP:** ordinary UniFi (Pro/Enterprise/Aggregation) are **not** PTP
  clocks — they forward but jitter the timestamps. Ubiquiti's new
  **Enterprise-AV (EAV)** line (announced Apr 2026) adds PTP
  grandmaster/boundary + native ST 2110/Dante/SDVoE — verify it's
  shipping before designing around it. Homelab workaround: software
  `ptp4l` on HW-timestamping NICs, or VRR-on-the-display and skip genlock.
- **Cabling:** SFP28 DACs negotiate down to 10G; one DAC type covers both.
  MSA-compliant third-party (FS/10Gtek) DACs work on UniFi.

## Compute for the DIY tiers

**RK3588 is the only viable SBC** (Orange Pi 5 Plus or Radxa Rock 5B):
it's the only one with a **native 4K60 HDMI receiver**, a **hardware
H.265 low-latency encoder** (via Rockchip MPP), and a **PCIe 3.0 x4** slot
for a 10G NIC. Raspberry Pi 5 is disqualified as a *producer* (Broadcom
removed the HW encoder; HDMI-in only via a laggy bridge) but is a fine
*consumer* (HW decode + HDMI out). Caveats that shape the build:

- The low-latency HDMI-RX + MPP-encode stack is in the **vendor BSP
  kernel / out-of-tree patches**, not mainline — you pin a maintained
  kernel (Armbian / Joshua Riek Ubuntu). Build pipelines in **GStreamer
  or ffmpeg-rockchip**, not OBS (which chokes on the multiplanar format).
- Encode codec = **H.265/H.264 low-latency, never AV1** (RK3588 AV1 is
  decode-only).
- **10G is overkill for a compressed stream** (1080p60 H.265 is tens of
  Mbps); the fast NIC is about fitting your fabric and, for the
  *uncompressed* SBC experiment, carrying ~3 Gbps. Skip 25G on the SBC —
  RK3588 can't push much past ~10 Gbps of real traffic regardless of NIC.
- **Power/thermal:** PoE + 10G NIC + encoder together push a Rock 5B+
  past 802.3at; spec **802.3bt** PoE and active cooling or the encoder
  throttles.

## Capture front-ends (DIY tier)

| Front-end | Use | Res / latency | Notes |
|---|---|---|---|
| **MacroSilicon MS2130** USB3 dongle (~$15–20) | phase-0 prototyping | 1080p60, ~50–130 ms | zero-driver UVC; latency crutch, not the endgame |
| **TC358743** HDMI→CSI on CM4 (~$25–35) | mainline 1080p60 unit | 1080p60, ~1 frame | **mainline v4l2, full EDID control**, I²S audio — the reference DIY producer |
| **Lontium LT6911UXE** HDMI2.0→CSI (~$50) | 4K60 unit | 4K60, sub-frame | mainline landing 2025; prefer UXE over UXC; Jetson/Genio class |
| **Magewell Eco Capture 4K Plus M.2** (~$600) | fidelity reference | 4K60, low | rock-solid driver, EDID, audio — premium, not cheap-at-scale |

All black out on HDCP — correct for the DIY tier (unprotected sources).

## FPGA (the transparent tier)

For sub-16 ms cable-like transport, or as the base for the licensed
product, FPGA does line-based capture → RTP → 10/25G with no OS jitter.

- **Cheapest credible bench:** QMTech Kintex-7 XC7K325T dual-10G (~$300)
  or the turnkey **Alinx AV7K325** (~$780: 4× 10G SFP+, HDMI-in via
  ADV7619, HDMI-out via SiI9136, 4K30). Using **external HDMI bridge
  chips avoids the paid Xilinx HDMI IP license.**
- **Open gateware that closes the license-free path:** litevideo (HDMI
  capture/out, BSD) + LiteX (glue) + a hand-written RTP/UDP framer +
  LiteEth or **verilog-ethernet** (MIT; 10G, and 25G) NIC. No commercial
  IP required for 1080p uncompressed.
- **Reality check:** there is **no free low-latency FPGA compression
  core** (JPEG-XS/DSC commercial; VC-2 has no mature RTL) — so the FPGA
  path stays **uncompressed and sized to the link**. And it's a
  **multi-month HDL project** vs days-to-weeks for the SBC path; choose it
  only when sub-ms determinism is the goal.
- **4K60 uncompressed** needs 25G GTY-class boards ($k) or a licensed
  JPEG-XS core over 10G.

## The HDCP fork (the product gate)

To carry protected content (Apple TV, Shield, Sony TV apps, 4K Blu-ray)
you must be a **licensed HDCP repeater**: terminate HDCP as a certified
sink, re-protect the IP hop with HDCP 2.x **IIA (Interface Independent
Adaptation** — the spec mechanism for HDCP over a non-HDMI transport),
re-originate HDCP downstream, and honor repeater duties (KSV/topology
aggregation, ≤127 devices/depth 7, SRM revocation).

**The license is more approachable than the engineering.** Per DCP LLC
directly: HDCP has **no per-unit royalty** — an annual adopter fee only
(historically ~$15k/yr, current figure quoted on application; unverified),
plus device-key sets (30 days, or $5k express). A small vendor can become
an adopter. What's *not* small-company-friendly is implementing a
compliant HDCP 2.3 repeater with IIA from scratch and passing
**robustness** (hiding keys/cleartext from JTAG/debug/accessible memory —
the hardest FPGA task) and interop compliance. **So: become a DCP adopter,
but build on silicon/IP that already holds the HDCP component license and
a certified repeater — never hand-roll HDCP crypto.** Also budget the
**HDMI Adopter** license, which *does* carry a per-unit royalty (~$0.15/
unit, gated) — the one real unit tax, into COGS.

Three foundations for the protected tier:

| Foundation | Ceiling | HDCP | Effort / access | Verdict |
|---|---|---|---|---|
| **SDVoE / Semtech BlueRiver** | 4K60, HDMI 2.0b, 10G, uncompressed/zero-latency, multichannel audio | **2.2**, in hardware | OEM route: Alliance membership + NDA + MOQ + dev kit | Fastest legitimate protected 4K60 today; **can't do 4K120/HDMI 2.1** → won't satisfy an RTX-5090-at-4K120 buyer |
| **FPGA HDMI 2.1 + HDCP 2.3 IP** (AMD subsystems + Bitec/3rd-party FRL/HDCP cores) | **4K120 / 8K, HDMI 2.1 FRL 48G** | **2.3** | Highest: HDMI+DCP+AMD-HDCP NDAs, 5–6-figure IP NRE, you own robustness+compliance | The only route to the true enthusiast bar; reuses yoke's control plane |
| **IPMX** (open: ST 2110 + JPEG-XS + AES67 + NMOS) | 4K60 4:4:4 from 1G (JPEG-XS), uncompressed on 25G, scales to 4K120 | "interoperable HDCP" **claimed**, least field-proven for consumer sources | Standards-based, fits UniFi 10/25G, multi-vendor; you still adopt DCP + validate HDCP yourself | Best long-term/open hedge; highest HDCP-validation burden today |

**A/V-snob acceptance checklist** (dealbreakers if any fail): no chroma
subsampling below source (4:4:4 / 10–12-bit, no banding); **HDR10 /
HDR10+ / Dolby Vision** pass bit-exact — DV needs a **Dolby license +
mandatory testing** and a bit-exact metadata **tunnel** (any repacketizing
that corrupts it drops DV→HDR10, an instant dealbreaker); **VRR / ALLM /
CEC** passthrough (5090/console buyers); **lossless audio** bitstream
(Dolby TrueHD / DTS-HD MA / Atmos / DTS:X — HBR packets, carried untouched
by any bit-transparent transport). **eARC return** (TV apps' Atmos back to
an AVR over the same IP link) is a distinct, often-missing feature —
decide it in or out explicitly.

**Minimum legitimate stack for "protected 4K/HDR, Apple TV → Sony TV,
transparent":** DCP adopter (HDCP + 2.x addendum, keys) + HDMI adopter +
a foundation whose silicon already carries certified HDCP (SDVoE now, or
licensed FPGA HDMI 2.1 IP for the flagship) + pass robustness/repeater/DV
compliance. Engage DCP early — IIA-over-IP compliance is a live
conversation, not a checkbox.

## Fastest path to a working demo (build ON these, don't start from zero)

There are two clean license-free camps and nothing in between:
**H.264/HEVC over gigabit** (what LKV373A, Just Add Power, and PiKVM all
actually do — tens-of-ms to ~100 ms) and **uncompressed/JPEG-XS over
10G** (ST 2110 / SDVoE — sub-frame). The cheap commercial boxes are all
the former; the pro silicon (Semtech BlueRiver/SDVoE) is NDA-locked — but
**Intel's Media Transport Library + SVT-JPEG-XS is an open software
equivalent** of the latter on an x86 box with a 10G NIC.

**The shortcut that collapses ~80% of the consumer endpoint: Sunshine +
moonlight-embedded.**

- **Consumer** = an RK3588 SBC running `moonlight-embedded -platform rk`.
  Its `src/video/rk.c` already does Rockchip MPP hardware decode → zero-
  copy DMA-BUF → DRM/KMS atomic commit → **raw HDMI out, no desktop**.
  This is exactly the "network → real HDMI" box the DIY/KVM world normally
  lacks (PiKVM/JetKVM all dead-end in a browser). And it carries the input
  path back (keyboard/mouse → host via uinput). GPL-3.0, actively
  maintained.
- **Producer, case A (source is a PC/GPU you control — e.g. the 5090):**
  run **Sunshine** on it (hardware encode + input injection done for you).
  Pair once → ~25–45 ms photon-to-photon on wired LAN, this week, zero new
  code.
- **Producer, case B (arbitrary external HDMI — console, Apple TV in game
  mode, another PC you can't run software on):** Sunshine captures its own
  framebuffer, so it's out. Use **V4L2 capture + GStreamer**: on RK3588
  `mpph265enc`, or on x86 `vaapih265enc → rtph265pay → udpsink`; the
  consumer pipeline is `udpsrc → rtpjitterbuffer latency=10 → mppvideodec
  → kmssink`. PiKVM's **ustreamer** (TC358743 capture + HW H.264 + its
  `libs/drm` HDMI-output primitive) and `kvmd`'s USB-gadget code
  (`/dev/hidg*` HID, emulated mass-storage) are liftable for this side.

**For the uncompressed/transparent tier without FPGA:** an x86 mini-PC +
a ~$300–500 Intel E810 10G NIC running **MTL** gives you real ST 2110
(uncompressed 2110-20, or JPEG-XS 2110-22 to drop 1080p/4K onto a 2.5G/10G
link) — the open "SDVoE-lite," all COTS, no license. SVT-JPEG-XS is
x86-only (AVX-512), so this tier is x86, not SBC.

Traps confirmed: the open-FPGA video path is a trap — **litevideo/litehdmi
and HDMI2USB are deprecated/abandoned**; only the FPGA *output* and the
*Ethernet MAC* (verilog-ethernet/Corundum) are alive, so an FPGA build
means reviving capture gateware + writing the RTP framer yourself.
"Open NDI" does not exist (libndi is proprietary) — use ST 2110-22/JPEG-XS
instead. Sunshine/Moonlight/PiKVM are all GPL-3.0 (a distributed endpoint
firmware inherits GPLv3); MTL/SVT-JPEG-XS are BSD.

## Verification (this is testable, per yoke's principles)

Build a **glass-to-glass latency rig**: an LED (or screen region) driven
at the source, a **photodiode** on the consumer display, an MCU timing
the gap — repeatable, automatable, sub-ms. This is the headline number
the project cites; supplement with 240 fps slow-mo for quick checks and
per-stage instrumentation (ustreamer-dump, RTP capture timestamps).
Also measure A/V sync (flash+click), tearing (high-speed capture of a
motion pattern), and drift over a 30-min run.

## How this plugs into yoke

The yoke control plane is unchanged — this is the media plane it already
delegates to a driver (ADR 0002). Add a **`gst` driver** whose `attach`/
`detach` start/stop a GStreamer (or FPGA-control) pipeline for the
producer's media parameters, exactly as the `igmp`/`jap` drivers do. It
stays **offline-testable**: the driver takes an injected command runner,
and a loopback pipeline (`videotestsrc` → RTP → `udpsink`/`udpsrc` →
sink) exercises it with no hardware, matching the sans-I/O test strategy.
The pairing intent already carries a driver-defined `media` dict — for
this driver it carries the RTP group/port, resolution, and codec.

## Phased plan (ordered; each phase is a strict subset of the next)

The discipline: the network, transport, sync, and yoke control plane are
**identical** across every phase. Only the media front-end changes —
DIY-capture (unprotected) → licensed repeater (protected). Nothing is
throwaway.

### Phase 0 — Prove control plane + transport this week. ~$150, or free.
- **Producer:** Sunshine on the RTX-5090 PC (hardware encode + input
  injection, done for you).
- **Consumer:** one RK3588 board (Radxa Rock 5B / Orange Pi 5, ~$150)
  running `moonlight-embedded -platform rk` → MPP decode → DRM/KMS → real
  HDMI out, headless.
- **Result:** a transparent HDMI+input endpoint pair, ~25–45 ms on wired
  LAN, **zero new code**. Baselines latency and proves "network → real
  HDMI" end to end. Wire yoke's control plane in next by adding the
  `stream` driver (below) so pairing is controllerless.

### Phase 1 — Arbitrary-HDMI capture, unprotected, transparent. ~$450/pair.
- **Producer:** Orange Pi 5 Plus (RK3588, native 4K60 HDMI-in, ~$145) on a
  pinned Armbian/Riek BSP kernel; GStreamer `v4l2src → mpph265enc`
  low-latency `→ rtph265pay → udpsink`. (Or TC358743-on-CM4 for a fully
  mainline, EDID-controllable 1080p60 unit.)
- **Consumer:** Radxa Rock 5C (RK3588S2, decode-only, ~$45) `udpsrc →
  rtpjitterbuffer latency=10 → mppvideodec → kmssink`, VRR on the display.
- **Network:** native 2.5G, or add a $60 AQC113 M.2 10G NIC for the
  uncompressed experiment. Dedicated AV VLAN, jumbo frames, unicast.
- **Add:** EDID proxying (consumer→producer), HDMI audio with sink-side
  adaptive resampling, USB HID via gadget mode, and the **LED/photodiode
  glass-to-glass rig** (project verification infra).
- **Result:** the full DIY endpoint, transparent on every unprotected
  source (5090 gaming, PC desktop, consoles in game mode, cameras),
  ~35–55 ms. This is where the yoke `stream` driver gets exercised for
  real.

### Phase 2 — The transparent tier (sub-16 ms / uncompressed), still unprotected.
Two independent sub-paths; do 2a first (cheap, COTS), 2b only if you want
true sub-ms determinism or are heading for the FPGA product.
- **2a — x86 + software ST 2110.** Mini-PC + Intel **E810** 10G NIC
  (~$300–500) running **Intel Media Transport Library** — real
  uncompressed 2110-20 (1080p) or **JPEG-XS 2110-22** (4K on 10G). Bit-
  exact, all COTS, no license, on your 10/25G fabric. The open "SDVoE-
  lite."
- **2b — FPGA.** Alinx **AV7K325** (~$780; HDMI via ADV7619/SiI9136 bridge
  chips → no Xilinx HDMI-IP license) or QMTech K7 (~$300) + **litevideo +
  verilog-ethernet + a hand-written RTP framer**. 1080p60 uncompressed
  over 10G, ~5–16 ms, no OS jitter. A multi-month HDL project; the base
  for the licensed product. 4K60 uncompressed needs a 25G GTY board ($k).
- **Result:** proves the "as if right next to each other" bar and the
  uncompressed-over-your-fabric thesis end to end.

### Phase 3 — The product: protected content, all sources, A/V-snob grade.
The HDCP fork (see above). Sequence:
1. **Become a DCP HDCP adopter** (License + 2.x addendum, order keys) and
   an **HDMI adopter**. Engage DCP on **IIA-over-IP compliance** early.
2. **Pick a foundation:** SDVoE/BlueRiver for a fast, legitimate 4K60/
   HDCP-2.2 product now; **licensed FPGA HDMI 2.1 + HDCP 2.3 IP** for the
   4K120/8K flagship that reuses yoke's control plane; IPMX as the open
   hedge. (Ship SDVoE/IPMX now, keep FPGA-IP as the flagship roadmap.)
3. **Clear the enthusiast checklist:** 4:4:4/10–12-bit, HDR10/HDR10+/DV
   bit-exact tunnel (+Dolby license/testing), VRR/ALLM/CEC passthrough,
   lossless-audio bitstream, and an explicit **eARC-return** decision.
4. **Pass robustness + repeater + DV compliance.** This, not the license
   fee, is the real gate.

**Budget shape:** Phases 0–2a are hundreds of dollars and weeks of work on
COTS parts with no licenses — enough to fully validate the yoke design and
the transport thesis. Phase 3 is a business decision (annual license fees,
IP NRE, compliance) that only the protected-content requirement forces.

## How this plugs into yoke (and stays offline-testable)

Add one media-plane driver, `stream`, alongside `sim`/`igmp`/`usbip`/`jap`
(ADR 0002 — the control plane is unchanged). Its `attach(producer, media)`
starts the receive pipeline for the producer's advertised parameters;
`detach()` stops it; `describe()` (producer side) advertises them. The
`media` dict the pairing intent already carries becomes, e.g.,
`{"transport": "rtp", "group": "239.x", "port": N, "pt": 96,
"encoding": "raw|jpegxs|h265", "caps": "..."}`.

- **Prototype driver:** wraps GStreamer/`moonlight`/MTL via the existing
  injectable command-runner seam (like `usbip`/`jap`), so it is unit-
  tested against scripted transcripts with **no hardware**.
- **Loopback integration test:** `videotestsrc → rtpvrawpay → udpsink` on
  one port, `udpsrc → …` on another, entirely on `127.0.0.1` multicast —
  exercises attach/detach/failure paths offline, matching yoke's sans-I/O
  test discipline. Every error condition (pipeline won't start, producer
  gone, codec mismatch) is reproducible without a capture card.
- **FPGA/appliance driver:** same interface, driving the device's control
  API (register writes / vendor API) instead of a GStreamer process — a
  thin warden, exactly like the `jap` driver.

So the entire hardware program above lands as **one new driver** behind
yoke's existing, already-proven, offline-tested control plane.

## Open items to confirm before spending (from the research)

- Current DCP HDCP annual fee and HDMI per-unit royalty (both gated behind
  application/login — get real numbers before Phase 3 budgeting).
- RK3588 HDMI-RX + MPP-encode kernel choice pinned and reproducible
  (BSP/out-of-tree today; mainline landing in progress).
- SDVoE dev-kit price/MOQ and whether IPMX HDCP interop with real consumer
  sources (Apple TV → Sony) is field-proven yet.
- Bench-confirm the AQC113-on-RK3588 sustained throughput and the
  MS213x/Lontium firmware/EDID specifics before standardizing a unit.

