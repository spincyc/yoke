# Hardware platform recommendation

Status: 2026-08-09. The concrete answer to "what do I run this on," staged
from pure-software (today) to the real fabric to the product. Rationale
lives in BUILD-PLAN.md and COMPRESSION.md; this is the decision.

## Now (pure software, zero purchase): any two Linux hosts

The `yoke.media` pipeline is sans-I/O and stdlib-only, so **you develop
and test the entire system — every stage, the snooper, FEC recovery,
jitter, rate control, glass-to-glass integrity — on your workstation or
two VMs/containers, with no hardware and no network.** `make verify` is
the whole proving ground. This is the point of the sans-I/O design: the
hardware never gates the software.

The first "real" run over loopback multicast needs nothing but the box
you already have.

## Step 1 — the software-transparent tier on your fabric: 2× mini-PC + Intel NIC

When you want it running over the actual UniFi network at the compression
sweet spot, the pragmatic platform is **x86, because the only free
line-based visually-lossless codec that hits the sweet spot
(SVT-JPEG-XS) is x86/AVX-512 only.**

| Item | Pick | Why | ~Cost each |
|---|---|---|---|
| Compute | Intel N-series or Core mini-PC (AVX2/AVX-512), e.g. an NUC-class or a used SFF (i5-12500T+) | runs SVT-JPEG-XS + Intel MTL (ST 2110); AVX-512 for JPEG-XS throughput | $200–450 |
| NIC | **Intel E810** (25G SFP28) or **E810-XXV / X710** (10G) | MTL's best-supported NIC for DPDK pacing + HW timestamping; SFP28 negotiates to 10G | $250–500 |
| Cabling | SFP28 DAC (negotiates 10G/25G) | one cable type covers both | $30 |
| Capture (unprotected sources) | USB: MacroSilicon MS2130 ($20) to start; later a Magewell M.2 4K ($300) | brings a real HDMI source in for end-to-end | $20–300 |

Two of these on a dedicated AV VLAN give you: real ST 2110 uncompressed
(1080p) or SVT-JPEG-XS at 2.5–4:1 (4K), ST 2022-7 dual-path FEC, the
adaptive rate controller driven off the E810's congestion signals, and
the `yoke media snoop` verifier on a third cheap host. **This is the
software SDVoE-lite, and it's where "transparent on my fabric" gets
proven on real content.** Budget ~$1–1.6k for the pair + a snoop box.

Why not the RK3588 SBC here: it has no line-based visually-lossless codec
(only frame-based H.265), so it can't reach the sweet-spot latency band
(it lives at ~35–55 ms). It's excellent as a cheap **consumer/decoder**
or for the KVM-grade tier, not for the transparent tier.

## Step 2 — the deterministic / cable-feel tier: FPGA

For sub-16 ms, no-OS-jitter, line-based-in-hardware transport (and as the
base for the licensed product), the cheapest viable endpoint is the
**Sipeed Tang Mega 138K Pro (~$175–195)**: native DVI-Rx HDMI capture +
2× SFP+ at 12.5 Gbps, carrying uncompressed 1080p60. **See FPGA-COST.md
for the full head-to-head** — the Gowin route is 2.5–4× cheaper than the
Xilinx route (~$500/endpoint: a ~$377 Kintex-7 board + an FMC HDMI card),
at the cost of less-mature tooling and a non-hardened TMDS-capture margin
to bench-pilot first. Open gateware: litevideo + verilog-ethernet + a
hand-written RTP framer. No free compression core exists, so uncompressed
1080p60 needs the ≥10G SFP link (Kintex-7 GTX or Gowin GW5A SERDES;
Artix/ECP5 can't clock it). Multi-month HDL effort — do it only after
Step 1 proves the design. 4K60 uncompressed needs a 25G GTY board.

## Step 3 — the product (protected content): licensed foundation

Per the HDCP fork (BUILD-PLAN.md): SDVoE/BlueRiver for a fast 4K60/HDCP
2.2 product, **licensed FPGA HDMI 2.1 + HDCP 2.3 IP** for the 4K120
flagship that reuses yoke's control plane, or IPMX as the open hedge —
plus the DCP + HDMI adopter agreements. A business step the
protected-content requirement forces; the prototype tiers prove
everything else first.

## The one-line answer

**Start on the machine you're reading this on (pure software). First
purchase: two x86 mini-PCs + Intel E810 NICs (~$1–1.5k the pair) to run
SVT-JPEG-XS/MTL at the compression sweet spot on your 10/25G UniFi. FPGA
and licensed silicon come only after the software proves the design.**
