# The 4K120 hardware floor

Status: four-lane deep search, 2026-08-09. **The user set 4K120 as the
absolute floor.** This establishes what that costs. Prices are flagged
where the session's web-search budget was exhausted and live pricing
couldn't be re-verified — treat them as order-of-magnitude, confirm before
buying.

## The one-paragraph truth

**Transparent 4K120 over switched IP is bleeding-edge — nobody sells it.**
Every shipping AV-over-IP platform (SDVoE/BlueRiver, Crestron DM-NVX,
IPMX/Matrox, AVPro MXNet, Just Add Power, ZeeVee, WyreStorm) caps at
**4K60 / HDMI 2.0 / 10G**; HDBaseT 3.0 is 16 Gbps and not even switched IP.
There is **no 4K120 AVoIP chipset or module to OEM.** So the "buy" column
is empty: a 4K120 endpoint is a from-scratch build, and the whole problem
reduces to one hard part — **HDMI 2.1 FRL ingest** — which is the dominant
cost and risk, bigger than the 25G transport.

## The pivot: ingest is the whole problem, and it's Lontium-or-Bitec

To get 4K120 *pixels* out of an HDMI 2.1 source (an RTX 5090 sends 4K120 as
FRL, up to 48 Gbps), you need an HDMI 2.1 FRL receiver. The market:

- **Merchant FRL-to-pixel silicon = Lontium, essentially alone.**
  LT6911GX/GXC (FRL4 32G, 4K120), LT6911GXD / LT7911UXE (FRL5 40G,
  4K120/4K144) genuinely do it, ~**$10–25/chip**. Everyone else (ITE, ADI,
  MegaChips, Socionext, Parade, Realtek, Synaptics) is HDMI-2.0-max on the
  merchant market or OEM/ASSP-only. Concentration risk (one Chinese vendor,
  NDA docs), but it's the cheap path.
- **Turnkey FPGA front-end:** Bitec FMC HDMI 2.1 card (**€595**, verified)
  + an HDMI 2.1 FRL IP core (AMD's paid LogiCORE on a GTY device, or
  Bitec's own IP on Lattice/Microchip/Intel).
- **Cheapest "pixels to a PC" (not transparent):** a consumer HDMI 2.1
  capture card — AVerMedia GC575 (~$200, PCIe) or Elgato 4K X ($250, USB-C)
  — but they output **encoded HEVC**, not raw pixels, and refuse HDCP. Fine
  for a 5090 desktop/game; wrong for a transparent endpoint.

**The cost-saving insight:** let the **~$15 Lontium bridge do the FRL (and
DSC) decode** and output raw pixels on MIPI/LVDS; the FPGA then only does
pixels → RTP → 25G. That **avoids the paid AMD HDMI 2.1 IP, the DSC IP,
and (for unprotected sources) HDCP entirely** — the license-free DIY path.
Cost: a custom PCB + MIPI/LVDS RX in fabric + Lontium's NDA init sequences.

## The DSC sleeper gotcha

A 5090 at **4K120 10-bit 4:4:4 HDR almost certainly sends VESA-DSC-
compressed FRL** on the wire (it's also how anything reaches "8K30"). Your
RX must **decode DSC** to recover raw pixels. Lontium's 8K-class parts
include a DSC decoder; AMD's is a separately-enabled option; capture cards
handle it internally. Build an RX without DSC and it ingests 4K120
4:2:0/4:2:2 fine but chokes on the GPU's default 4:4:4 HDR mode.

## The format fork sets the tier (from the bandwidth lane)

4K120 active-pixel rates (verified): 4:2:0 8b ≈ 11.9 G; **4:2:2 10b ≈ 20 G**;
**4:4:4 10b ≈ 30 G**; 4:4:4 12b ≈ 36 G. Usable 25G ≈ 23.5 G.

- **4:2:2 10-bit (~20 G) fits a single 25G link UNCOMPRESSED, license-free.**
  For **cinematic HDR — film, TV, streaming, UHD Blu-ray — this is a strict
  superset of the source** (all mastered 4:2:0 10-bit), i.e. beyond
  transparent, zero compromise. Ingest via an FRL4 (8 G/lane) Lontium part.
  **This is the cheap tier.**
- **4:4:4 10/12-bit (30–36 G) does NOT fit 25G** — needed for PC desktop /
  RGB gaming / fine colored text. Requires either **100G QSFP28
  uncompressed** (license-free, bigger FPGA + optics) or **light
  compression** (self-written DSC = royalty-free but engineering; licensed
  DSC core = NRE, no per-unit royalty; JPEG-XS = NRE **plus** a patent-pool
  per-unit royalty — avoid). Ingest via an FRL5/6 (10–12 G/lane) Lontium
  part or AMD's 48G IP. **This is the expensive tier.** Both codecs are
  sub-frame (JPEG-XS ~10–30 µs, DSC sub-line) so transparency holds. No
  free/open FPGA codec exists.

## The 25G silicon (the cheap half — from the FPGA lane)

25G needs **GTY** transceivers (GTH tops at 16 G). Cheapest GTY silicon =
**Kintex UltraScale+ (KU5P)**. The used-datacenter "goldmine" (SQRL
BCU1525, Alveo U50/U200, ~$350–800) is a **trap for an endpoint** — no
video-ingest path, PCIe form factor needs a host PC, 200 W+; useful only to
prototype the transport/compression logic. The cheapest **standalone board
with 25G + an FMC to attach HDMI** is ~**$1,500 (ALINX AXKU5)** or ~$2,700
(AMD KCU116). Watch logic capacity: KU5P is small — RX + codec + 25G MAC
may force a bigger (pricier) part.

## The floors, by tier

| Tier | What it does | Ingest | Transport | Realistic $/endpoint | License |
|---|---|---|---|---|---|
| **Prototype, cinematic** | transparent 4K120 4:2:2-10 HDR, unprotected | Lontium FRL4 bridge (~$150–400 eval, or ~$15 chip on custom PCB) | 25G uncompressed | **~$1,650–2,000** (bridge + KU5P FMC board + SFP28) | **none** |
| **"Pixels across the room" (not transparent)** | encoded 4K120 desktop/game | capture card ~$200 | x86 + GStreamer/HEVC | **~$200 + a PC** | none |
| **Any-source, unprotected** | bit-exact incl. PC RGB/4:4:4/12b | Lontium FRL5/6 bridge or Bitec FMC (€595) + IP | 100G uncompressed, or 25G + self-DSC | **~$2,500–4,500** | none (self-DSC) |
| **The product** | all sources incl. **HDCP-protected**, A/V-snob | licensed HDMI 2.1 + HDCP 2.3 IP (+DSC) | 25G/100G | **$2,500–4,500 BOM + $50–200k IP + $0.5–2M eng** | HDCP+HDMI+Forum ~$30–45k/yr + royalty |

## Recommendation

**Clarify what "4K120 floor" means — it's a 10× cost lever:**

- If it means **cinematic HDR passthrough** (movies/streaming, 4:2:2 fine):
  the floor is **~$1,650–2,000/endpoint**, license-free, buildable — a
  Lontium FRL4 bridge → GTY FPGA (KU5P + FMC) → 25G uncompressed, open
  gateware, no IP licenses. This is the one to prototype.
- If it means **bit-exact to any HDMI 2.1 source** (PC RGB/4:4:4/12-bit
  gaming): **~$2,500–4,500/endpoint** via 100G-uncompressed or self-DSC,
  still license-free but bigger silicon and real HDL.
- If it means **the protected-content product**: it's the **$0.5–2M,
  12–24-month licensed-FPGA effort** gated on HDMI 2.1 FRL + HDCP 2.3 IP —
  a thing the entire commercial industry does not yet ship.

## Open items to confirm before spending (web budget was exhausted)

- Live Lontium quotes + stock (LT6911GX/GXD, LT7911UXE) and their NDA docs.
- A matching **4K120 HDMI 2.1 TX** bridge for the regenerate side (Lontium
  or an FPGA HDMI 2.1 TX IP).
- AMD HDMI 2.1 Subsystem exact FRL/GTY/device list + license cost (AMD page
  timed out).
- KU5P logic-capacity check for RX + codec + 25G MAC (may force KU11P/15P).
- MIPI/LVDS D-PHY RX rate into the FPGA for the 4K120 pixel stream.
- Whether the 5090's default 4K120 mode is DSC (it likely is) → DSC decode
  needed even on the "uncompressed" path.
