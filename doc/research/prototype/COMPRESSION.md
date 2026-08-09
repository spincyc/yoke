# The compression sweet spot: what a little costs in time, buys in transit

Status: design analysis, 2026-08-09. The question this answers: on a fast
switched network, exactly how much compression should we apply — and the
answer is a real optimization, not "none" or "a lot."

## The dial has five axes, and one of them is not linear

Turning the compression knob moves five things at once:

1. **Latency (time)** — what you pay.
2. **Bandwidth (transit)** — what you save.
3. **Quality** — bit-exact → visually lossless → lossy.
4. **Loss-resilience** — how a dropped packet hurts.
5. **Compute/power** — encoder cost.

The one that changes the whole analysis: **latency vs compression is not
a smooth curve — it's flat, then a cliff.** There are two regimes, and
the sweet spot lives entirely in the first one.

### Regime 1 — line-based codecs (the flat part)
The codec works on a few lines at a time and emits as it goes; it never
buffers a whole frame. Latency is a few lines to a fraction of a frame —
**sub-millisecond**, essentially constant across the whole regime.

| Codec | Latency | Ratio range | Quality | License |
|---|---|---|---|---|
| JPEG-XS | <1 ms (≈8–32 lines) | ~1.5:1 to ~10:1 | visually lossless to ~6:1 | patent-pooled (free code exists) |
| VC-2 LD (Dirac) | few lines, sub-ms | ~2:1 to ~4:1 | visually lossless | **royalty-free** |
| VESA DSC | <1 line | ~3:1 (to 3.75:1) | visually lossless (VESA-tested) | open spec, RAND IP |

Within Regime 1 the latency cost of turning the dial from 1:1 to ~6:1 is
**flat and negligible**. The transit benefit is linear (2:1 = half the
bytes). So the rational move is to ride the dial *up* to the
visually-lossless limit and stop — you're buying transit and resilience
at a latency cost you cannot perceive.

### Regime 2 — frame-based codecs (the cliff)
The codec needs the whole frame (intra) or several frames (inter GOP).
Latency jumps to **1 frame (16.7 ms @60) for intra-only, 33–80 ms for a
full GOP.** You get huge ratios (20:1 to 100:1+) but you've left
"transparent" behind — 16–80 ms is detectable, and DCT coding smears
text/UI. This is the wall you do **not** want to cross for a transparent
link. It's only for bandwidth-starved paths (WAN, 1G shared).

**The whole game: stay in Regime 1, ride the dial to the
visually-lossless limit, never touch the cliff.**

## The numbers (anchor: 4K60 4:2:2 10-bit ≈ 10 Gbps uncompressed)

| Operating point | Ratio | Bitrate | Added latency | Quality | On 10G | Headroom | Loss resilience |
|---|---|---|---|---|---|---|---|
| Uncompressed | 1:1 | ~10 Gb/s | ~0 (packetize) | bit-exact | at line rate | **none** | **none — every drop glitches** |
| Near-lossless | 1.5:1 | ~6.7 Gb/s | <0.5 ms | ~mathematically lossless | tight | ~33% | poor |
| **Sweet spot** | **2.5:1** | **~4 Gb/s** | **<1 ms** | **imperceptible loss** | **easy** | **~60%** | **good — FEC affordable** |
| Sweet spot, aggressive | 4:1 | ~2.5 Gb/s | <1 ms | imperceptible (most content) | easy | ~75% | very good |
| Light lossy | 6:1 | ~1.7 Gb/s | <1.5 ms | slight risk on fine detail/noise | easy | ~83% | excellent |
| — the cliff — | | | | | | | |
| Intra-only H.26x | ~20:1 | ~500 Mb/s | ~17 ms (1 frame) | DCT-lossy, text smear | trivial | huge | 1-frame corruption |
| Full GOP H.265 | ~100:1 | ~100 Mb/s | 33–80 ms | lossy | trivial | huge | until next I-frame |

For 1080p60 (anchor ~2.5–3 Gbps), the same 2.5:1 sweet spot lands at
~1–1.2 Gbps — which is how you fit a transparent 1080p stream onto a
**2.5G** link with headroom, or many of them onto 10G.

## Why the headroom matters more than the fit (compression × QoS × FEC)

Uncompressed's real problem isn't fit — it's that it has **zero error
resilience**. One dropped packet is one visible glitch, so it demands a
*perfect* network: dedicated VLAN, strict-priority queuing, no
oversubscription — and it's *still* brittle to microbursts on a shared
switched fabric. That is the opposite of "works anywhere on my network."

A little compression fixes this in three compounding ways — this is the
part your instinct was reaching for:

1. **Fit + headroom → QoS becomes trivial.** At 2.5:1 you use ~40% of a
   10G link. The stream never contends because there's always room; DSCP
   priority is a safety net, not a lifeline.
2. **Saved bandwidth funds FEC → the stream survives loss.** Spend the
   ~6 Gb/s you saved on forward error correction: **SMPTE ST 2022-7**
   (send two copies down diverse switch paths/queues — seamless
   protection switching) or RTP parity FEC (~10–25% overhead, recovers
   isolated drops). Now a microburst doesn't glitch; the FEC reconstructs
   it. You literally convert transit savings into reliability.
3. **Smaller/fewer packets → lower NIC-CPU load and smaller microbursts.**
   Halving the bitrate halves the packet rate that was the real
   bottleneck for uncompressed on a software endpoint.

So compression, QoS, and FEC are **one integrated answer**: a little
compression buys the headroom that makes QoS effortless and funds the FEC
that makes the stream robust on a real, shared, switched network. This is
not a compromise away from transparent — it's what makes transparent
*reliable* off the bench. It is exactly what SDVoE and IPMX ship: they run
~2–4:1 light compression on 10G on purpose, not because they must.

## The sliding scale, made dynamic (the genuinely clever version)

The dial doesn't have to be static — make it a **closed-loop rate
controller** that slides along the compression scale in real time, which
is literally the "sliding scale + QoS" you asked for:

- **Input:** live network state — ECN congestion marks, one-way-delay
  variation, loss, and the QoS-guaranteed headroom on the AV VLAN.
- **Law:** when the fabric is quiet, run near the top of the
  visually-lossless band (~2:1, closest to transparent). When contention
  appears, slide down toward 4:1–6:1 to shrink the stream and ride out
  the congestion — still line-based, still sub-ms, still visually
  lossless. **Never cross into the frame-based cliff.**
- **Result:** the experience stays transparent-feeling and never glitches,
  because the controller trades imperceptible quality for congestion
  survival exactly when needed. JPEG-XS and VC-2 both expose per-slice
  target-bitrate control, so this is a real knob you drive from a network
  monitor — not a fantasy.

### Free notches below the codec (spend these first)
- **4:4:4 → 4:2:2 chroma subsampling:** ~1.5:1 for free, imperceptible on
  video and most content — the first notch, no codec engaged.
- **Desktop/KVM: build-to-lossless** (Teradici's trick): send a
  light-compressed frame the instant pixels move, then progressively
  refine static regions to bit-exact. Text feels instant *and* ends up
  pixel-perfect — a temporal sweet spot orthogonal to the spatial one.

## The sweet spot, stated plainly

**Line-based, visually-lossless, ~2.5:1 to 4:1, sub-1 ms, with saved
headroom spent on ST 2022-7 / FEC and a closed-loop controller that
slides within the visually-lossless band under QoS pressure.**

- **Time cost:** <1 ms added, against an ~8 ms display-scanout floor —
  unmeasurable in use. (The cliff, by contrast, costs 16–80 ms — which is
  why the dial must never go there for a transparent link.)
- **Transit benefit:** 2.5–4× — turns "doesn't fit / no headroom" into
  "uses a third of the link with room for redundancy."
- **Quality:** visually lossless — passes A/V-snob scrutiny (the failure
  is chroma/bit-depth truncation and DCT smear, neither of which a
  line-based visually-lossless codec at 2–4:1 introduces).
- **Robustness:** with the FEC the headroom funds, it survives a shared,
  contended switched fabric instead of demanding a perfect one.

## How it maps to the build tiers

- **x86 + MTL (Phase 2a):** SVT-JPEG-XS (x86/AVX-512) at 2–4:1 over ST
  2110-22, with ST 2022-7 dual-path FEC. This is the software SDVoE-lite
  running *at the sweet spot*, today, on your 10/25G fabric.
- **FPGA (Phase 2b / product):** a DSC- or VC-2- or JPEG-XS-class
  line-based core in fabric — sub-line latency, the sweet spot in
  hardware, deterministic, no OS jitter. The rate-control input comes from
  the NIC's congestion signals.
- **SBC (Phase 1):** RK3588 has no line-based visually-lossless codec —
  only frame-based H.265. So the SBC path *cannot* reach this sweet spot;
  it lives one regime up (the ~35–55 ms tier). That's the concrete reason
  the transparent tier is x86-MTL or FPGA, not the SBC.

## The unifying point

This sweet-spot transport is the **design center for the whole product**,
both forks: it delivers a transparent-feeling, QoS-robust, any-medium
point-to-point experience on your switched network for unprotected content
with **no license**, and it is the *identical* transport a licensed
repeater uses for protected content. Same dial, same sweet spot. The
compression knob — set to line-based, visually-lossless, ~2–4:1, adaptive,
FEC-protected — is the answer that delivers what you're asking for.
