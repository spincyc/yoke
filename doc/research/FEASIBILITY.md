# Feasibility: can yoke's goal be built?

Status: cold review, 2026-09-24, from four live-web research lanes plus
local benchmarks. This is the canonical verdict; `HARDWARE.md` holds the
market detail, `COMPRESSION.md` the codec trade-offs, and
`../design/EDGE-DEVICES.md` the endpoint design built on this.

Flags: **V** verified against a primary source or measured; **I**
inferred or computed; **⚠** bench-unverified (see `AGENTS.md`).

## The goal

A fully decentralized, point-to-point, reconfigurable mesh of HDMI and
USB bridge devices over a switched IP network. The network baseline is
**switched 10GbE** (user, 2026-09-24). Endpoint hardware should be as
small as possible. The first concrete link is a closet gaming PC
(RTX 5090, 4K120) to a desk monitor plus USB peripherals, at competitive-
gaming latency.

## Verdict: possible. Two specific combinations are not.

| Part of the goal | Verdict | Basis |
|---|---|---|
| Decentralized, reconfigurable mesh | **Built** | The control plane (`src/yoke/core`, `net`, `drivers`) pairs endpoints with no controller; proven offline by `make verify`. |
| HDMI bridges over switched 10G | **Possible; sold today at 4K60** | SDVoE endpoints carry 4K60 4:4:4 at ~1.3:1 with <100 µs latency (V). On 10G they need a site API server (ZeeVee ZMP), so they meet the no-controller rule only with a waiver; per-device commercial control is 1G (Just Add Power) (`HARDWARE.md` §1). |
| 4K120 over switched 10G | **Possible with ~2.7–3.3:1 visually lossless compression** | Arithmetic below. Buildable with licensed IP or NDA-gated Lontium silicon; no open parts suffice. |
| USB bridges over switched IP | **Possible** | USB2 is routine; USB 3.2 over a switched LAN ships (Icron Raven 3204C Pro, V); software re-pairable USB3 bulk exists (VirtualHere, V). |
| HDCP-protected sources | **Possible as encrypted pass-through, point-to-point** | A bridge can forward ciphertext and tunnel the handshake without ever decrypting, as HDMI extenders do. It cannot compress, so 10G carries protected content up to ~4K60 4:2:0 or 4K24 (§HDCP). Permitted by ADR 0011. Decrypting, compressing, or one-to-many needs licensed repeater silicon. |

**Impossible:** bit-exact 4K 4:4:4 at 60 Hz or above over 10G
(physics), and decrypting or compressing HDCP content without a license
(law). **Not yet sold:** a 4K120 endpoint over 10G that is
near-transparent. Everything else is engineering and money.

### Settled concern (2026-09-24)

An earlier session concluded the goal was not possible. That conclusion
conflated "no one sells it" with "cannot be built", assumed 25G
uncompressed as the only 4K120 route, and missed GPU-side DSC (below).
Rejected on the evidence in this document; re-raise only with new
evidence.

## The arithmetic

10GbE carries 10.0 Gb/s at the MAC. After Ethernet, IP, UDP, and RTP
overhead the usable payload is **≈9.4 Gb/s at 1500 MTU, ≈9.9 Gb/s at
9000 MTU** (I). Plan on ≤ ~9 Gb/s sustained for headroom.

Active-pixel rates (3840×2160 = 8.29 Mpx):

| Format | 4K60 | 4K120 | Fits 10G? |
|---|---|---|---|
| 4:2:0 8-bit (12 bpp) | 5.97 Gb/s | 11.9 Gb/s | 4K60 only |
| 4:2:2 10-bit (20 bpp) | 9.95 Gb/s | 19.9 Gb/s | no |
| 4:4:4 8-bit (24 bpp) | 11.9 Gb/s | 23.9 Gb/s | no |
| 4:4:4 10-bit (30 bpp) | 14.9 Gb/s | 29.9 Gb/s | no |
| **DSC 8 bpp** | 3.98 Gb/s | **7.96 Gb/s** | **yes, ~15% headroom** |
| DSC 12 bpp | 5.97 Gb/s | 11.9 Gb/s | 4K60 only |

So on 10G, **4K 4:4:4 at 60 Hz or above needs compression** (4K30 4:4:4
8-bit, 5.97 Gb/s, fits uncompressed).
4K120 4:4:4 needs 2.7:1 (8-bit) to 3.3:1 (10-bit). DSC at 8 bpp is 3:1
on 8-bit RGB, which is VESA's visually-lossless design point, and 3.75:1
on 10-bit, which is beyond it (I).

For comparison, uncompressed 1080p120 4:4:4 8-bit (5.97 Gb/s) and
1440p120 4:2:2 8-bit (7.08 Gb/s) fit 10G bit-exact.

## The 4K120 routes, ranked

| Route | Added latency | License | Status |
|---|---|---|---|
| **A. GPU's own DSC carried compressed over DisplayPort** | sub-ms transport (I) | none for the link; NDA silicon | novel; gated on Lontium NDA |
| B. Lontium re-encodes uncompressed input to DSC 8–9 bpp | sub-ms (I) | NDA silicon | gated on Lontium NDA |
| C. FPGA with licensed codec IP (JPEG XS / DSC) | sub-ms | quote-only IP; JPEG XS pool ~$5 per 4K instance (V) | buildable |
| D. Pro capture card + x86 + software JPEG XS | **~15–32 ms** (I, from measured stages) | pool fee | buildable now from retail parts |
| E. Hardware H.26x / NVENC | 5–7 frames at 4K (V, arXiv 2511.18688) | — | fails the latency goal |

### Route A: DSC passthrough (the key finding)

The GPU already compresses with VESA DSC when the sink advertises DSC
and the mode doesn't fit the link. If an endpoint can pick that bpp and
carry the compressed stream without decoding it, no codec is needed.
NVIDIA's open kernel modules (the Blackwell display classes inherit the
same code) show:

- **Uncompressed is always tried first**; DSC engages only when the
  advertised link can't carry the mode (V,
  [nvhdmipkt_C671.c](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/main/src/common/modeset/hdmipacket/nvhdmipkt_C671.c)).
- **DisplayPort:** bpp = min(link bandwidth ÷ pixel clock, sink maximum
  bpp from **DPCD 0x067–0x068**) (V,
  [nvt_dsc_pps.c](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/main/src/common/modeset/timing/nvt_dsc_pps.c),
  [dp_deviceimpl.cpp](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/main/src/common/displayport/src/dp_deviceimpl.cpp)).
  A synthesized sink advertising max 8 bpp plus a link too slow for
  uncompressed 4K120 forces **8 bpp ≈ 7.96 Gb/s** (I). NVIDIA's DP MST
  path already falls back to 8 bpp (V).
- **HDMI:** the GPU takes the lowest FRL rate where the minimum bpp fits,
  then the *highest* bpp that fits there. 4K120 at 8 bpp doesn't fit
  3 lanes × 3G, so it steps to 3 × 6G and lands on **12 bpp ≈ 11.9 Gb/s,
  over 10G** (code V, arithmetic I). Only native 4:2:0 DSC fits. **So
  route A is DisplayPort-only for 4:4:4.**
- The Linux bpp override needs the sink's `DSC_All_bpp` and does not help
  a Windows gaming PC (V).

Receivers that hand over the compressed stream instead of decoding it:

| Part | What it does | Access |
|---|---|---|
| **Lontium LT6911GXD** | HDMI 2.1 (FRL5) + DP 1.4a in; "DSC pass-through" on its MIPI DSI output; also its own DSC 1.2a encoder (route B). QFN88, status "Preview" (V, [brief](https://www.zhuoxunweihong.com/wp-content/uploads/2024/04/LT6911GXD_U2_Brief_Draft3-1.pdf)) | NDA via L2Tek; ~$10–25 chip |
| AMD HDMI 2.1 RX subsystem (PG351) | "DSC Pass Through" to AXI4-Stream, new since 2022 (V, [PG351](https://docs.amd.com/r/en-US/pg351-v-hdmi-rxss1/DSC-Pass-Through)); TX side PG350 accepts it | licensed IP, price by quote |
| AMD DP 1.4 RX/TX (PG300/PG299) | hooks for external DSC and FEC cores (Rambus) (V) | licensed IP |

**Blockers, in order:** (1) Lontium NDA firmware: whether DPCD max-bpp
and the DSC path are configurable; (2) a desk-side transmitter that
re-emits a DSC stream to a DSC-capable monitor (or decodes it); (3) the
pixel clock must be locked across the link; (4) the Windows driver's DSC
policy is inferred from shared open code, not observed ⚠. **No one has
been found carrying DSC over Ethernet** (V, negative search), so this is
novel work.

Removing codec IP also removes the D-PHY bandwidth problem the previous
25G design had: ~8 Gb/s fits a 4-lane 2.5G D-PHY (I). A 3-lane 2.5G
connector (7.5 Gb/s) does not, so a board with 3-lane connectors needs
two of them (dual-port DSI) ⚠.

### Route D: the commodity path, measured

SVT-JPEG-XS on a Core Ultra 9 285K (lane benchmark, V): 4K 4:2:2 10-bit
at 4 bpp encodes at 167 fps on 8 P-cores and decodes at 246 fps;
measured per-frame latency when paced at 120 fps is 5.8 ms encode and
4.3 ms decode. Four P-cores fail 120 fps. The recommended small box
(Minisforum MS-A2, 16 Zen5 cores) was not benchmarked (I). Consumer HDMI 2.1
capture cards deliver **MJPEG** at 4K120; raw 4K120 needs a pro card
(Yuan SC750N1-L or DeckLink 8K Pro G2). Stage budget: capture ≥1 frame
(8.3 ms) + repack 1–2 + encode 3–6 + serialize (overlappable) + decode
2.4–4.3 + present 0–8.3 ms ⇒ **~15–32 ms added** (~15 only with slice
streaming and a VRR display; ~20–32 typical). Workable for desktop use;
fails "feels like a cable" for competitive gaming.

## USB

USB 3 over a switched network exists, but no product is at once
driverless, controllerless, API-re-pairable, and multi-switch:

| Option | What it gives up |
|---|---|
| Icron Raven 3204C Pro (hardware, all transfer types) | button pairing, 1:1, exactly one switch, 10GBASE-T |
| Aten KX9980 (USB 3.2 in KVM-over-IP) | needs the CCKM central manager |
| VirtualHere, usbip, Digi, SEH, Silex | a driver on the host PC |

The mesh-able path is software: a real USB host at the device end plus
VirtualHere (Linux server → Windows client; documented attach/detach
API). USB3 **DisplayLink** over any USB-over-IP is **unproven** (only a
USB2 DisplayLink display has been shown working, V). Details in
`HARDWARE.md` §USB.

## HDCP: pass-through, not a repeater

HDCP encrypts the pixel data on the link; the source and the sink
authenticate over HDMI's DDC channel and share the session key. A bridge
that **forwards the ciphertext unchanged and tunnels the DDC handshake
verbatim** is invisible to both: the source authenticates the real
display, and the bridge never decrypts. This is how HDMI extenders,
fiber HDMI cables, and HDBaseT carry protected content. It is permitted
for yoke-designed endpoints by ADR 0011.

**What it can't do:** ciphertext is effectively random, so it cannot be
compressed, and every encoding step (DSC, JPEG XS, chroma subsampling)
needs decrypted pixels. The bridge carries every encrypted bit at full
rate. With blanking regenerated identically at the far end, the payload
is roughly the active-pixel rate:

| Protected signal | Ciphertext rate | 10G | 25G |
|---|---|---|---|
| 1080p60 / 1080p120 RGB 8-bit | 2.99 / 5.97 Gb/s | yes | yes |
| 4K24 RGB 8-bit (films at native rate, incl. Dolby Vision) | 4.78 Gb/s | yes | yes |
| 4K60 4:2:0 10-bit (HDR streaming) | 7.46 Gb/s | yes | yes |
| 4K60 4:2:2 12-bit or RGB 8-bit (typical HDR menu mode) | 11.9 Gb/s | no | yes |
| 4K120 | 23.9–29.9 Gb/s | no | 8-bit 4:4:4 only; 40G+ otherwise |

The bridge owns the EDID it presents, so it can steer a source into a
format that fits (e.g., advertise 4K60 only at 4:2:0); EDID carries no
HDCP information, so this is not a protection downgrade.

**Engineering requirements:**

- **Raw link capture.** Merchant HDMI receiver chips (including Lontium)
  are HDCP endpoints that decrypt with licensed keys or output black, so
  they cannot supply ciphertext. Capture TMDS in FPGA transceivers
  (feasible for HDMI 2.0-class links); HDMI 2.1 FRL would also need its
  link layer implemented.
- **Identical timing, clock-locked.** The HDCP keystream is indexed by
  frame and pixel position, so the far end must reproduce the source's
  exact timing. HDCP 2.x uses AES counter mode: a lost packet corrupts
  only its own pixels, but losing frame sync blanks the screen until the
  source re-authenticates.
- **Real-time handshake tunnel.** HDCP 2.x over HDMI is message-based
  (write/read message buffers plus a status register at DDC address
  0x74), so a local DDC proxy that forwards writes and mirrors the far
  sink's replies fits its timeouts. The **locality check allows 20 ms
  round trip** on HDMI; a switched LAN is ~0.3 ms. HDCP 1.4's register-
  mirrored link check (Ri' every 128 frames) is more timing-sensitive ⚠.
- **Strictly one sink per session.** The source authenticates one
  display; a second display on the same ciphertext cannot decrypt it.
  One-to-many protected distribution needs a licensed repeater.

**Legal reading, not a legal opinion:** a relay that never decrypts is
not circumvention (that is what HDCP strippers do), and the HDCP license
binds licensees implementing HDCP functions, which a pass-through does
not. Extenders in this class are sold widely. A commercial product needs
this reviewed, plus HDMI Adopter status.

**Licensed repeaters** (terminate as a certified sink, protect the IP
hop with HDCP 2.x IIA, re-originate downstream) are only needed to
decrypt, compress, or fan out. HDCP has no per-unit royalty, only an
annual adopter fee, but hand-rolled repeater HDCP must pass robustness
and compliance testing (Aug 2026 research). yoke drives purchased
repeaters and never participates (ADR 0004).

## What earlier docs got wrong (corrected here)

| Earlier claim | Correction |
|---|---|
| "USB 3.x over switched Ethernet does not exist" | False: Icron Raven 3204C Pro (V) |
| "Nobody sells 4K120 over IP" | BZBGEAR XLink-ONE XTREME announced (June 2026) 4K120 over 1GbE at ≥26:1, shipping Q4 2026, unavailable at retail (V). Nothing near-transparent. |
| Consumer capture cards "output encoded HEVC" | They output MJPEG at 4K120; raw stops at 4K60 (V) |
| "DSC is the wrong layer" | DSC passthrough is the leading 4K120-over-10G route |
| 4K120 plan = 25G uncompressed 4:2:2 | The baseline network is 10G; 25G is not assumed |
| "SDVoE lacks PoE"; "JAP MaxColor 10G SFP option" | Both refuted (V) |
| "SDVoE/IPMX run ~2–4:1 on purpose" | SDVoE runs 4K60 4:4:4 at ~1.3:1; 4K30 4:4:4 and 4K60 4:2:0 uncompressed (V) |
| DisplayLink over VirtualHere "field-demonstrated" | Only for a USB2 DisplayLink display |
| Protected content needs a licensed repeater; the locality check makes a relay impossible | A non-decrypting pass-through works point-to-point; the check allows 20 ms round trip on HDMI vs ~0.3 ms on a LAN. Bandwidth is the real limit (§HDCP). |

Earlier research documents (Aug 2026 lanes, build plan, FPGA cost,
4K120 floor, platform pick) were removed from the tree on 2026-09-24;
retrieve them with `git show 4e28967:<path>`.
