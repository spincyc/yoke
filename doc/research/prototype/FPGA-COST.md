# Cheapest FPGA endpoint for uncompressed 1080p60 HDMI-over-Ethernet

Status: deep cost search, 2026-08-09. Supersedes the rough "$300–780"
figure in HARDWARE-PLATFORM.md. Two research lanes (Xilinx/used-surplus,
and sub-$150/Gowin) reached **different floors** because the vendor
choice, not the silicon capability, sets the price.

Search caveat: the session's web-search budget was exhausted, so several
prices are from listing pages actually read, but a few (cheap FMC-HDMI
cards, some Gowin street prices) are estimates flagged below and worth a
fresh check before ordering.

## The finding: the floor is ~$130–195/endpoint, not $500

The two routes to the *same* capability (capture HDMI, send uncompressed
1080p60 over a ≥10G-class serial link, regenerate HDMI):

| Route | Cheapest endpoint | Why | Total for a 2-box link |
|---|---|---|---|
| **Gowin GW5A (Sipeed Tang Mega 138K)** | **$130 Dock** (SERDES→DIY SFP) or **$175–195 Pro** (native 2× SFP+) | native DVI-Rx HDMI capture **and** 12.5 Gbps SERDES on one cheap board | **~$260** (Dock+DIY) / **~$350–390** (Pro) |
| **Xilinx Kintex-7** | ~$377 board + ~$100–150 FMC-HDMI + ~$20 SFP+ DAC ≈ **~$500** | cheapest K7 board with SFP+ has **no HDMI** — you bolt on an FMC HDMI card | **~$1,000** |

So the Xilinx route really does sit near $500/endpoint (that lane insisted
"$500 is physics" — true *for Xilinx*). The Gowin route undercuts it 2.5–4×
because Gowin bundles native HDMI-in and fast SERDES on a hobbyist board.
**The honest floor for uncompressed 1080p60 is ~$130–195 per endpoint**
(Gowin), or ~$500 if you want the mature Xilinx ecosystem.

## What both lanes agree on (the hard constraints)

- **HDMI-*in* is the gatekeeper, not Ethernet.** It eliminates the whole
  ultra-cheap tier: Colorlight/ECP5 boards (~$30) have *no HDMI connector*;
  most Gowin boards are HDMI-*out* only. Filtering on native TMDS capture
  collapses the sub-$150 field to essentially the **Tang Mega 138K**.
- **No free FPGA compression core exists.** GitHub-scale search confirmed:
  VC-2/Dirac is software-only (BBC C++), DSC is C reference models only,
  JPEG-XS FPGA cores are commercial (intoPIX/Fraunhofer). So a cheap board
  cannot lean on a codec to fit 1080p60 into 1G — you subsample (4:2:0 /
  lower fps) or buy link speed. Roll-your-own line-DPCM gives only ~1.5–2:1,
  data-dependent, not enough.
- **Transceiver speed sets the tier:** Artix-7 GTP (~6.6G) and ECP5 (~5G,
  1G PHYs) **cannot** clock a 10.3125G SFP+ link → excluded for uncompressed
  1080p60. Kintex-7 GTX and Gowin GW5A SERDES (both to 12.5G) can.
- **Bandwidth vs 1G:** 1080p30 4:2:0 (0.75G) and 720p60 4:2:0/4:2:2 fit a
  1G link; **1080p60 in any form does not** (1.5–3G) → needs the SFP path.
  (Correction to an earlier note: 720p60 *RGB* is 1.33G and does **not** fit
  1G; only its 4:2:0 form does.)
- **HDMI IP + HDCP:** Xilinx HDMI RX/TX IP is paid-licensed; the free
  workaround is DVI-style TMDS decode via SelectIO/ISERDES (what
  LiteX/HDMI2USB do) — fine for **unprotected** capture, no license. HDCP
  remains the separate legal/silicon wall for protected sources.
- **Two endpoints.** A transparent link is a TX box + an RX box — double
  every number.

## Recommendation

For the cheapest pin-to-pin prototype: **2× Sipeed Tang Mega 138K Pro Dock
(~$175–195 each, ~$350–390 the pair)** — native DVI-Rx capture, native
2× SFP+ at 12.5G, carries uncompressed 1080p60 including full RGB, no
FMC/DIY. The $130 plain Dock saves ~$45/board but needs a DIY SERDES→SFP
breakout.

Weigh against the Xilinx route (~$1,000/pair) only if the tooling maturity
matters: Vivado + a large ecosystem + proven open TMDS-capture gateware,
vs. Gowin's less-mature IDE and a **non-hardened DVI-Rx whose 1080p TMDS
capture margin must be bench-piloted first** — the top risk to retire
before committing to the Gowin path.

## Open items to confirm before ordering

- Gowin Tang Mega 138K DVI-Rx capture margin at 1080p60 (pilot it).
- Live price of a cheap FMC-HDMI (ADV7611+ADV7511) card for the Xilinx
  route (~$100–150 estimated; the one soft number in the $500 total).
- Whether the plain Dock's SERDES→SFP breakout is DIY-feasible for you, or
  worth the +$45 for the Pro's native cages.
- Gowin toolchain maturity for TMDS ingest + a 10GBASE-R + RTP datapath vs.
  the LiteX/verilog-ethernet stack, which is Xilinx/ECP5-proven.
