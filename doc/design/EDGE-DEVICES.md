# Edge devices: the full HDMI path and the full USB path over switched UniFi

Status: designed 2026-08-09 from a nine-lane live-web hardware search,
then hardened by a three-lens adversarial review (§Sanity check; all
confirmed findings are folded in below). Prices verified 2026-08-08
unless flagged. This supersedes the transport decision in
`BUILD-SPEC-CLOSET-KVM.md`: **transport = the existing switched UniFi
network; point-to-point fiber is ruled out (user decision).** It also
resolves the new requirement: the USB mesh must carry **WinWing
flight-sim gear — including their USB3 video displays — over the
switched network.**

## Requirements (restated, current)

1. Full HDMI path: closet PC (RTX 5090, HDMI 2.1, 4K120 floor) → desk
   monitor, transparent, over the switched UniFi fabric.
2. Full USB path: desk/cockpit USB peripherals — including WinWing USB3
   video displays — presented to the closet PC over the same fabric.
   Mesh-shaped: re-pairable via yoke (v1 delivers the seam, see below).
3. **Lowest latency — competitive gaming: single-digit ms added,
   ideally none** (inherited from `BUILD-SPEC-CLOSET-KVM.md`; every
   tier below is annotated against it).
4. Specific, buyable hardware to start bench testing now.
5. The network infrastructure already exists; only the edge devices are
   designed and built here.
6. Terminate + regenerate; own EDID/HPD and USB session state at the
   edges — **at both ends of each path, including the PC side** (the
   two diagnosed commercial-extender failures must not recur).
7. HDCP out of scope (PC output is unprotected).

## Finding zero: what the WinWing "USB3 video devices" actually are

Verified against winctrl.com (WinWing is mid-rebrand to WINCTRL) store
API, manuals, and community protocol work:

| Class | Devices | USB | Traffic |
|---|---|---|---|
| **DisplayLink USB monitor** | **"USB3.0 Instrument Display1" (D1)** ($123.50) and every bundle containing it (MFD1+D1 $180.05; Top Gun MIP 2 = 3× D1, $799.96) | USB 3.0 SuperSpeed, hardware USB2 fallback (status LED: purple=USB3, yellow=USB2) | Host-compressed proprietary **bulk** video from the PC's DisplayLink driver; 1024×768@60; est. **50–350 Mb/s per display** (unmeasured — bench task) |
| **Self-rendering HID panels** | MCDU, PFP 3N/4/7, ICP (DED), UFC 1/2, FCU/EFIS/RMP/TCAS/PAP3/PDC… | **USB 2.0** vendor HID, VID 0x4098 | 64-byte HID reports, character-cell display protocol (publicly reverse-engineered); KB/s |

⚠ Caveat (review finding): the **MCDU-32**'s screen transport is not
confirmed from a primary source — possibly DisplayLink-class. Day-one
bench rule: `lsusb -t` every owned WinWing device; reclassify anything
that enumerates SuperSpeed.

Consequences that shape the USB design:

- **Only the D1 displays need USB3-class throughput.** Everything else
  is trivially tunnelable USB2 HID (years of field evidence of sim HID
  gear over extenders/USB-over-IP).
- **D1 pixels originate on the PC** (DisplayLink driver renders and
  compresses on the host). They cannot be "terminated and regenerated"
  at the desk — the bulk stream itself must cross the network intact.
  Terminate/regenerate applies to the *USB sessions*, not the D1 pixels.
- The traffic is bulk + interrupt only — **no isochronous** in the
  WinWing set. Bulk has no protocol deadline; switched-LAN RTT
  (~0.2–0.5 ms) is orders of magnitude inside enumeration timeouts.
- Worst-case aggregate (3× D1 ≈ 1.05 Gb/s) **fits 2.5GbE with 2.4×
  headroom** — the desk box needs 2.5GbE, not 10G, for the USB lane.
- All WinWing gear is **power-fragile** (documented D1 "failed to be
  identified" without aux 5V; MCDU disconnect loops on weak hubs). The
  desk box must supply real 900 mA+/port, wire the D1 aux-5V feeds, and
  disable autosuspend — all controllable on a Linux host we own.
- **SimAppPro** (Windows-only, on the PC) needs raw HID access to
  VID 0x4098 and is the firmware updater. **Firmware flashing across
  the network path is forbidden by policy** (maintenance mode: direct
  USB, or accept the documented brick risk).

### DisplayLink operational risks (review finding — independent of transport)

- **Windows 11 24H2 broke WinWing MIP DisplayLink drivers** (field
  evidence). The DisplayLink driver is a Microsoft-update-exposed
  single point of failure for all D1 displays. Policy: pin/hold the
  DisplayLink driver and defer Windows feature updates on the sim PC.
- **DisplayLink drivers conflict with NVIDIA G-Sync** (main-monitor
  stutter / G-Sync disablement) — a direct collision between the two
  halves of this design on the same 5090. **"G-Sync + D1 coexistence"
  is an M2 go/no-go bench measurement**, before any transport work.
- The fallback (render viewports at the desk instead) breaks SimAppPro
  integration — it is not a silent escape hatch.

## The full USB path

```
CLOSET                                        DESK
PC (Windows: sim, SimAppPro,                  WinWing D1 displays (DisplayLink)
   DisplayLink driver,                        WinWing HID panels, HOTAS, kb/m
   yoke PC-side watchdog agent)                ▲ powered hubs + D1 aux 5V
 │                                             │
 ▼ path A: VirtualHere client (software)      real local USB3 host (desk box)
 ▼ path B: Icron Raven LEX (contingency)       · local enumeration, local power
 ▼ path C: SuperSpeed gadget (future)          · autosuspend off, per-port power
 │                                             │
 ▼ 2.5G into UniFi           ═══ fabric ═══    ▲ 2.5G into UniFi
yoke agent ◄──────────── control plane ───────► yoke agent + USB watchdog
```

Three termination options at the PC, in deployment order:

- **A — VirtualHere (bench now; likely production).** Server on the
  Linux desk box, client service on the PC. USB3 bulk streaming is
  implemented exactly for this topology (Linux server → Windows
  client), developer-measured at **480–800 Mb/s** on wired LAN;
  built-in auto-reconnect; DisplayLink-over-VirtualHere is
  field-demonstrated (MIMO display from a Linux server). $49/server
  after a free 1-device trial — the load-bearing question (does
  SimAppPro + DisplayLink tolerate the forwarded topology?) costs
  **$0 to answer**. The 480–800 Mb/s envelope is **latency/window
  bound, not NIC-bound** — if 3× D1 exceeds it, the scaling lever is
  **multiple server instances (one per D1) or path B**, not a faster
  NIC. Measure at M2.
- **B — Icron Raven 3204C Pro (contingency, with an explicit waiver).**
  $1,349.95 in stock (Markertek). Hardware USB 3.1 over a switched
  LAN — all four transfer types, no PC software. **Hard constraints
  (manual, verified): LEX and REX on the same subnet, minimum
  10GBASE-T, exactly ONE switch between them** — a hard precondition
  on the topology answer, not a footnote. **Waiver (review finding):
  path B forfeits requirements 2 and 6** — session state is sealed in
  Icron firmware, pairing is a physical button, telemetry is LEDs. If
  chosen, add the minimum ownable recovery layer: smart-PDU/PoE-switched
  outlets on LEX and REX driven by the yoke watchdog, plus a PC-side
  USB-tree health probe as the wedge detector.
- **C — yoke-native gadget (the transparent end-state, staged).**
  *USB2/HID tier works today*: raw-gadget + usb-proxy on RK3588 clones
  the HID panels byte-accurately (raw-gadget supports dwc3, multiple
  UDC instances). *SuperSpeed is blocked in mainline* (raw-gadget
  issue #61, open since 2023); the SS route is a bespoke FunctionFS
  composite gadget — scheduled only if A/B prove insufficient. One UDC
  = one VID/PID (Linux gadget cannot emulate a hub); Rock 5B+ exposes
  potentially 2 SuperSpeed UDCs (bench-verify); Rock 5C boards
  (**$69+ real in-stock floor**, review-corrected) add one identity
  each.

**The watchdog — both sides (review finding).** Desk box: yoke-supervised
state machine — keepalives, stall detection, automatic re-enumeration,
per-port power cycle (Sabrent switched ports) as the big hammer,
PID-change tolerance (WinWing role switching changes PIDs by design).
**PC side: a yoke watchdog agent on Windows driving VirtualHere's
documented named-pipe client API** — health-check the service, force
detach/reattach on stall, restart the service, alert. Field evidence
("SURPRISE UNBOUND" ~34 min in; clients hung in kernel_recvmsg) shows
the PC-side session is exactly as wedge-prone as the desk side. M2
acceptance: defined reattach time budget, characterized mid-flight
behavior (device-vanish vs frozen image), and a days-long soak with
forced link drops on **both** sides.

**The mesh seam (review finding — design it, don't assert it).** The
`vhusb` driver already listed as a v2 candidate in
`doc/research/HARDWARE.md` is the mechanism: a yoke driver that maps
pairing intents to VirtualHere client attach/release calls (named
pipe/CLI), exactly the `usbip`-driver pattern. **M3 deliverable.** Path
B is excluded from mesh service by its waiver. Until M3, the honest
scope is a single fixed PC↔desk link.

### USB hardware (buy now)

| Item | Price | Source | Role |
|---|---|---|---|
| FriendlyElec NanoPC-T6 (8GB) | ~$120–193 (verify; store 403'd) | FriendlyElec / Amazon youyeetoo | Desk host box: dual 2.5GbE, metal case |
| Radxa ROCK 5B+ 8GB | $175 (in stock, Arace, ships CN/HK) | arace.tech | Closet gadget box (path C): 2 SS-capable OTG ports, separate power USB-C |
| Sabrent HB-BUP7 7-port 36 W hub (per-port switches) | ~$36–55 | AVADirect/Newegg/Amazon | Desk: remote power-cycle lever; split load across 2 hubs |
| Honeycomb Flight Sim USB Hub 6-port 30 W | ~$50–70 | flyhoneycomb.com | Desk: the hub the WinWing cockpit community uses |
| VirtualHere server license | $49 (free 1-device trial first) | virtualhere.com | Path A transport |
| Icron Raven 3204C Pro | $1,349.95 (in stock) | Markertek | Path B — only after trial results + topology check + waiver accepted |
| Radxa ROCK 5C 2GB | $69 (in stock, Arace) | arace.tech | Path C: one extra gadget identity each |

Bench order (M2): NanoPC-T6 + hubs + VirtualHere trial with one D1 +
one MCDU → `lsusb -t` classification of every owned device → `usbmon`
measurement of real D1 bandwidth under DCS → **G-Sync coexistence
go/no-go** → SimAppPro detection/firmware-policy verdict → scale toward
3× D1 (multi-instance test) → decide A vs B vs fund C.

## The full HDMI path

```
CLOSET producer box                          DESK consumer box
PC HDMI 2.1 (FRL, 4K120)                     monitor HDMI 2.1 (or DP) in
 │ 30 cm certified 48G cable                   ▲ 30 cm certified cable
 ▼                                             │
Lontium FRL RX (GX-class)                    FRL/DP TX regen (tiers T1/T2/T3)
 · terminates FRL training locally            · trains monitor link against a
 · DSC decode iff we advertise it               LOCAL idle pattern, stream or no
 · serves SYNTHESIZED EDID                    · reads monitor EDID → control plane
 · HPD state machine w/ hysteresis             ▲ pixels
 ▼ MIPI D-PHY (lane count = NDA gate)        FPGA/x86: 25G → jitter buf → pixels
FPGA: pixels → RTP (+FEC) → 25G                ▲ SFP28
 ▼ SFP28
UniFi 25G port ══════ ≥25G end-to-end ══════ UniFi 25G port
yoke agent ◄─────────── control plane ────────► yoke agent
```

### Session-state ownership (the M1 brain — corrected by review)

- **EDID is synthesized, never passed through.** The closet box serves
  the **intersection of (monitor caps ∩ RX-chip caps ∩ transport-tier
  caps)**: Max_FRL_Rate rewritten to what the RX truly trains (FRL4 for
  GX-class), DSC capability fields set only if the pipeline decodes
  DSC, video modes pruned to what the transport carries, checksums
  recomputed. Serving the monitor's EDID verbatim would invite the GPU
  to train FRL5/6 against an FRL4 chip — recreating the exact
  training-roulette failure this project exists to kill. Served from
  local storage at all times, including network-down.
- **DSC is an EDID knob, not a law of nature (review finding).** The
  GPU sends DSC iff the advertised caps make the mode not fit. For the
  v1 4:2:2-10b tier, *not* advertising DSC forces uncompressed modes
  that fit FRL4 — DSC decode is **not required for v1**. It becomes
  required only when we advertise 4:4:4-10b HDR through an FRL4 part.
  This weakens "confirmed DSC decode" as the sole chip-selection
  criterion (though it keeps GXC preferred for the 4:4:4 future).
- **HPD with hysteresis and a local-training rule.** Assert when
  (RX ready ∧ pairing active ∧ **desk sink ready**), where desk-sink-
  ready = monitor attached ∧ EDID captured ∧ **desk TX trained against
  a locally generated idle pattern** — the TX must not need the
  network stream to train, or the bootstrap deadlocks (bridge TX parts
  typically require input video; this is an explicit LT9712 NDA
  question and a T1 design rule). **Transient transport loss does NOT
  deassert HPD**: the closet box holds HPD, freezes/blanks locally for
  a bounded window, and only deliberate unpair or monitor detach
  deasserts — otherwise every network blip is a GPU-visible hot-unplug
  and desktop reshuffle.
- **Link training happens twice, both across 30 cm certified cables.**
  No marginal 48G analog signal crosses the building.

### Format tiers — with the transparency compromise stated honestly

4K120 active rates: 4:2:2-10b ≈ 19.9 G; 4:4:4-10b ≈ 30 G (CTA timing
~35.6 G). Usable 25G payload ≈ 24.7 G (64/66b + jumbo framing) — the
25G margin for 4:2:2-10b is real.

**v1 target: 4K120 4:2:2 10-bit, uncompressed on 25G, license-free.
⚠ USER SIGN-OFF REQUIRED (review finding): 4:2:2 is NOT transparent
for desktop/RGB/fine-text content** — the project's own research says
PC desktop wants 4:4:4. v1 is transparent for game/video content,
visibly compromised for text rendering. The 4:4:4 closure paths: DSC
passthrough engineering, licensed DSC IP, or 100G. Also unresolved:
**who makes the 4:2:2** — EDID-steer the GPU into a 4:2:2 output mode
(unverified NVIDIA behavior for desktop) or subsample 4:4:4→4:2:2 in
the FPGA (unbudgeted stage). Design item, decided at M4 bring-up.

**The D-PHY arithmetic gate (review finding — top of the NDA question
list).** The lane research says LT6911GXC exposes **8 CSI data lanes ≈
20 Gb/s aggregate** (the 40G figure is confirmed only for LT6911GX,
whose DSC support is unconfirmed). With D-PHY protocol overhead
(~3–5%), 4:2:2-10b needs ~20.6–20.9 Gb/s — **it does not fit a 20G
D-PHY interface**; "zero margin" claims are actually negative. The
same math caps an LT9712-fed TX (~20G D-PHY in) below 4:2:2-10b —
T3 as specced carries 4:2:2-8b or 4:2:0 only. **Resolve GXC/GX real
usable lane count, mode, and per-lane rate with L2Tek before any PCB
or board-level commitment.**

### The Lontium bottleneck (plan around it, not through it)

Chips are gettable (~$6–25 est., JLCPCB/brokers; MOQ traps) but
**datasheets, init sequences, firmware, and eval boards are NDA-gated**;
no public Linux driver exists for any FRL-class part. Engage the
official distributor **L2Tek (UK)** now — LT6911GX/GXC + LT9712
samples, demo boards, firmware support, and the question list above;
expect weeks-to-months (the longest lead item in the plan). One
login-walled LT7911UXE demo board exists on AliExpress (item
1005007573182914) — worth a human-account price check. Fallback
ingest: Bitec FMC HDMI 2.1 card ($595, in stock) + quote-only HDMI 2.1
IP (Bitec or AMD PG350) — the licensed-IP path.

### Transport compute and the FEC gate

- **⚠ #1 transport risk: 25G FEC.** verilog-ethernet has NO RS-FEC
  (cl108) and no BASE-R FEC (cl74); community reports show UniFi
  Pro-Aggregation linking 25G DACs *with* cl74 and failing some SR
  optics, with no persistent FEC controls exposed. **De-risk for ~$70
  (review-corrected): probe with a used ConnectX-4 Lx (mlx5 supports
  ethtool show-fec/set-fec) against the actual closet switch** —
  enumerate which FEC modes link, DAC and SR both. The E810 is only
  needed later for the MTL bench tier / PTP.
- **FPGA platform: $320 KU5P QSFP28 board** (Thanksbuyer; secondhand
  silicon, no schematics — bring-up risk) — **buy AFTER the FEC probe
  passes on the real switch (review finding)**, not before; if the
  switch demands FEC, no open core exists and the boards strand.
  Fallback: ALINX AXKU5 (~$900–1,050 + $150 FMC SFP). AMD KCU116 is
  now $6,495 — do not buy (repo's ~$2.7k figure is stale). KU5P
  capacity is comfortable for RX + 25G MAC + RTP framer codec-free;
  a hand-rolled 2110-20-style RTP payload over verilog-ethernet is
  days of HDL; no open FPGA codec exists (re-confirmed).
- **x86 + MTL tier: a 4K60 bench-validation tier only** (honest
  verdict). No capture device delivers raw 4K120 into a host except
  the **Yuan SC750N1-L ($1,399)** — which is therefore the
  golden-reference instrument for validating the closet box, and the
  plan-B producer front-end.

### TX regen tiers (desk) — corrected by review

| Tier | What it is | 4K120 status | Added latency | Caveats |
|---|---|---|---|---|
| **T1** x86 + Radeon GPU | RX 6600-class HDMI out | **TX proven on Windows only.** Linux amdgpu FRL merged in kernel 7.2 (not yet stable), off by default (`dc_feature_mask=0x400`), no VRR. **RDNA2 = FRL5 40G, not 48G** — 4K120 4:4:4-10b fits *exactly*, nothing above. RX 7600 (~$250–270, RDNA3) for FRL6 headroom. **The 20 Gb/s receive pipeline (RTP → GPU → 120 fps scanout) is proven by nothing** — scoped as real work (DPDK-class RX, zero-copy GPU upload, direct scanout) | ~1 frame (8.3 ms) unless direct-scanout engineered — **fails req. 3 until then** | OS in the path; watchdog owns it |
| **T2** DP1.4 direct / PCON | FPGA + AMD DP1.4 TX IP (GTH-class) → DP monitor, or CAC-1085 PCON ($30–49) to HDMI | 4K120 8-bit RGB fits HBR3 **only with a CVT-RB2 modeline** (0.4% margin — pinning RB2 via the synthesized EDID is a T2 requirement, not an accident). 10-bit HDR needs DSC | sub-ms feasible (FPGA path) | license-light; PCON partially violates the principle |
| **T3** Lontium LT9712 | MIPI → HDMI 2.1 FRL6 | Product path, NDA; **D-PHY input caps below 4:2:2-10b** (see arithmetic gate) — needs lane-count resolution or DSC-in | sub-ms (clock-slaved) | symmetric with RX; longest lead |

**Latency budget vs requirement 3 (restored per review):** the
cable-feel target is met only by the clock-slaved FPGA path
(~50–300 µs one-way: ~8-line packetizer + wire + elastic RX buffer),
which requires **slaving the regenerated pixel clock to the ingest
rate** — an explicit M4 deliverable, not free. Frame-resync bring-up
mode costs ~8.5–17 ms (bring-up only). T1 costs ~1 frame until
direct-scanout is engineered. **The photodiode glass-to-glass rig is
the M4 acceptance instrument.**

**Still needed from the user: the monitor's exact model** (connector +
max mode) → picks T1/T2/T3 and the 4:2:2 sign-off context.

### HDMI hardware (buy now / buy gated)

| Item | Price | Source | Role |
|---|---|---|---|
| HDFury Dr HDMI 8K | $199 (verified shipping) | hdfury.com | **M1 bench anchor**: FRL6 EDID manager, copy-from-sink, scripted HPD via web server |
| Elgato 4K X | $230–250 | Best Buy/elgato.com | Cheapest FRL 4K120/144 sink instrument (compressed capture — instrument, not reference) |
| Mellanox ConnectX-4 Lx (used) ×2 | $62–76 | eBay | **Buy first**: the ~$70 UniFi FEC probe + bench 25G NICs |
| SFP28 DACs (UACC-DAC-SFP28 $23; FS from $14) + FS 25G-SR optics $49 | — | UI store / fs.com | FEC test matrix needs both DAC and SR |
| Yuan SC750N1-L | $1,399 | B&H | Only true 4K120 raw capture: golden-reference RX + plan-B producer |
| Radeon RX 6600 (~$239, review-corrected) or RX 7600 (~$250–270, FRL6) | — | Newegg/Amazon | T1 TX engine — Windows-proven; Linux experimental |
| Intel E810-XXVDA2 | ~$240–300 | Newegg/CDW | MTL bench tier + PTP — after the ConnectX FEC probe |
| KU5P QSFP28 dev board ×2 | $320 ea | Thanksbuyer | **Gated: buy after FEC probe passes** |
| Bitec FMC HDMI 2.1 | $595 | bitec-dsp.com | Only if the licensed-IP ingest path is chosen |
| Lindy 32119 8K EDID emulator | ~$85 | Lindy/Amazon | Cheap second FRL-era EDID dongle for A/B tests |

(The cheap 4K60-tier bench kit — Orange Pi 5 Plus, TC358743/Pi 5,
MS2130 probes, HDMI breakouts + logic analyzer, photodiode rig —
totals ~$700–900 and remains the right week-one purchase; itemized in
the start-testing-now lane report and `BUILD-SPEC-CLOSET-KVM.md`.)

## The network (what "infrastructure already present" must provide)

Verified Aug 2026:

- **25G SFP28 in UniFi = aggregation/campus switches only.**
  Pro-Aggregation ($970, 4× SFP28, 4 MB buffer — **in runout per B&H;
  verify at purchase**); Pro-XG-Aggregation ($2,696, 32× SFP28, in
  stock — **the planning number for the paid branch**); ECS-Aggregation
  ($4,314). **The EAV ProAV line has NO 25G access ports** (10G access
  + 100G uplink). We need speed, not fabric PTP — edges own timing.
- A 20 Gb/s flow is fine on-ASIC but **buffers are shallow** (4–8 MB ≈
  1.6–3 ms at 20 G): producer must pace; path must be 25G end-to-end
  (single flows can't LACP-split; any 25G→10G downshift is sustained
  overflow); ideally producer + consumer on one switch.
- **Unicast for v1.** A mis-snooped 20 Gb/s multicast flood is a
  LAN-killing event 23× the classic 1G AVoIP horror story. Multicast
  returns with the many-endpoint mesh, after the checklist passes.
- The ten-step verification checklist (inventory → querier pin → jumbo
  proof → flood-containment drill → 20 Gb/s 1-hour soak → mid-stream
  switch reboot) is in the unifi-fabric lane report; it must pass
  **before** the video stream rides the fabric. UniFi Gen 2 IGMP
  defects remain unconfirmed-fixed; Netgear M4250 stays the AV-VLAN
  escape hatch.

**Still needed from the user: the actual switch inventory** — models/
firmware between closet and desk, whether 25G SFP28 ports exist today,
whether both rooms can land on one switch (also gates Icron path B).
Determines whether $0, ~$970, or $2,696 of switching joins the BOM.

## Revised milestones

- **M1 — HDMI handshake ownership:** EDID-*synthesis* + HPD-hysteresis
  state machine modeled sans-I/O (`yoke.media` style) → 4K60 bench
  (Dr HDMI 8K + OPi5+/TC358743) → FRL bench (Dr HDMI 8K + Elgato 4K X
  as sink). In parallel: **start the L2Tek/Lontium engagement** (longest
  lead) with the D-PHY lane-count question first.
- **M2 — WinWing over the network (cheapest, run first in parallel):**
  desk host box + hubs + VirtualHere trial → device classification
  (`lsusb -t`) → D1 bandwidth under DCS → **G-Sync coexistence
  go/no-go** → SimAppPro verdict → both-side watchdog soak with forced
  drops → 3× D1 multi-instance scaling → decide A vs B (waiver) vs
  fund C. ~$250 answers the newest requirement's riskiest questions.
- **M3 — integrate behind yoke:** pairing + watchdogs both sides; the
  **`vhusb` driver** (VirtualHere client API ↔ pairing intents) makes
  the USB mesh real.
- **M4 — 4K120 video product path:** ConnectX FEC probe → KU5P
  bring-up → 4:2:2-10 uncompressed end-to-end (with the 4:2:2-source
  mechanism decided) → clock-slaved TX for the latency requirement →
  photodiode acceptance → 4:4:4 closure decision (DSC vs 100G).

## Sanity check (three-lens adversarial review, 2026-08-09)

Verdicts, all folded into the text above:

- **Arithmetic/protocol lens:** core rate/margin/buffer math confirmed
  sound (25G payload margin real; buffer and multicast math correct).
  Confirmed findings fixed: EDID must be synthesized (caps
  intersection), not passed through; HPD needed hysteresis + the
  local-idle-pattern training rule (bootstrap deadlock); GXC D-PHY is
  20G-class, so 4:2:2-10b **does not fit** without lane-count
  resolution (top NDA question); DSC-on-wire is caused by our
  advertised caps, not the GPU; 3× D1 fits 2.5GbE (10G NIC dropped);
  T2's DP fit requires CVT-RB2 explicitly.
- **Buyability lens:** sourcing largely survived live re-checks
  (Dr HDMI 8K, Icron price+constraints, VirtualHere claims, KU5P
  board, D1 price/identity, Rock 5B+, UniFi prices, EAV no-25G,
  SC750N1-L uniqueness all confirmed). Fixed: T1 "proven today" was
  Windows-only (Linux amdgpu FRL is 7.2-experimental); RDNA2 is FRL5
  40G not 48G; RX 6600 ~$239; Rock 5C floor $69; Pro-Aggregation in
  runout; FEC probe re-costed to ~$70 via ConnectX-4 Lx.
- **Requirements lens:** architecture shape endorsed (terminate+
  regenerate + real local host); no ADR 0002/0004 violations. Fixed:
  the latency requirement was silently dropped — restored with
  per-tier budgets; 4:2:2 non-transparency for desktop needs explicit
  user sign-off; PC-side watchdog added (path A was one-sided); path B
  demoted to contingency with a stated waiver; the mesh seam is now a
  named M3 deliverable (`vhusb`); DisplayLink Win11-24H2/G-Sync risks
  surfaced; T1's unproven receive pipeline labeled as scoped work;
  KU5P purchase gated behind the FEC probe; MCDU-32 classification
  caveat added.

## Open questions (user-owned)

1. **Monitor model** (connector + max mode) → picks TX tier T1/T2/T3.
2. **Switch inventory** closet↔desk → picks the 25G plan; gates Icron
   path B; sets the switching budget ($0 / ~$970 / $2,696).
3. **4:2:2 v1 sign-off** — accept visible desktop-text compromise in
   v1 (transparent for game/video), with 4:4:4 as the tracked closure?
