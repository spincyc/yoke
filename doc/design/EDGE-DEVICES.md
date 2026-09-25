# Edge devices: the HDMI and USB endpoints of the 10G mesh

Status: rewritten 2026-09-24 for the switched-10GbE baseline. It absorbs
the former closet-KVM build spec and keeps the Aug 2026 WinWing, USB,
and session-state findings, corrected against current research. The
verdict it builds on is `../research/FEASIBILITY.md`; parts and prices
are in `../research/HARDWARE.md`.

## Requirements

1. **Mesh:** any producer re-pairable to any consumer, no controller.
   The control plane already does this (`ARCHITECTURE.md`).
2. **Network:** the existing switched UniFi fabric at **10GbE** (user,
   2026-09-24). Only edge devices are designed here. Point-to-point fiber
   is ruled out (user, 2026-08-09).
3. **Size:** endpoint hardware as small as possible.
4. **First link:** closet gaming PC (RTX 5090, 4K120 floor) → desk
   monitor + USB peripherals, including WinWing flight-sim gear and its
   USB3 DisplayLink displays.
5. **Latency:** competitive gaming, single-digit ms added, ideally none.
6. **Terminate and regenerate; own session state** (EDID/HPD, USB
   enumeration) at **both** ends of each path.
7. **HDCP:** out of scope for the gaming link (PC output is unprotected).
   Protected sources ride either an HDCP pass-through link (ours,
   ADR 0011) or licensed commercial endpoints (ADR 0004).

## Why commercial extenders failed the user (do not rebuild these)

The user tried fiber HDMI/USB extenders and KVMs. Two diagnosed symptoms:

1. **USB wedges over days and needs a physical replug.** The extender
   relays USB timing across the link with no supervisor. USB is a
   tightly timed, host-polled bus; a missed keepalive wedges enumeration
   and nothing recovers it. Selective suspend makes it worse.
2. **Video flickers, goes black, or takes minutes to stabilize.** An
   EDID/HPD handshake and FRL link-training problem, not bandwidth: the
   extender doesn't hold a stable EDID or manage HPD, so the GPU
   re-probes and re-trains repeatedly, and a marginal 48 Gb/s analog
   signal through a cheap cable fails training intermittently.

Common root cause: passive relays of marginal high-speed signals that do
not own session state.

## Design principle

1. **Terminate and regenerate, never relay.** Video: a receiver chip
   terminates the GPU's link locally; a transmitter regenerates a clean
   link at the monitor. Link training happens twice, both across short
   certified cables. USB: a real host enumerates devices at the desk;
   USB timing never crosses the network.
2. **Own the session state.** Synthesized EDID/DPCD and deterministic HPD
   toward the GPU; an auto-reconnect watchdog on both USB ends.
3. **yoke is the supervisor.** Each endpoint runs a yoke agent:
   persistent pairing, keepalives, reconvergence.

## Endpoint families

The mesh is per media family: a consumer can only consume a producer
whose driver-defined `media` it understands. Cross-family pairings
surface as `driver-error` (`PROTOCOL.md` §ann).

| Family | Carries | Hardware | Driver |
|---|---|---|---|
| **DSC link** (ours) | 4K120 4:4:4, unprotected, sub-ms | Closet: LT6911GXD + FPGA + SFP+; desk: FPGA + a DSC-forwarding TX (open) | new, after bench |
| **USB** (ours) | HID + USB3 bulk incl. DisplayLink | NanoPi R76S desk box + VirtualHere; PC-side client | `vhusb` (M3) |
| **Commercial AVoIP** | 4K60, HDCP, all sources | Just Add Power (1G, per-device API); ZeeVee ZyPer4K-XS (10G, needs a ZMP site server: waives requirement 1) | `jap`, `zmp` (candidate) |
| **HDCP pass-through** (ours, ADR 0011) | Protected HDMI 2.0 sources, ciphertext untouched; ≤4K60 4:2:0 or 4K24 on 10G | FPGA with raw TMDS capture + SFP+ per end | new, after bench |
| **Unprotected 4K60** | 4K60, no HDCP, no USB | Blackmagic 2110 IP Mini | `nmos` (candidate) |

Which families the first deployment uses is the open v1-trade decision
in `HANDOFF.md`.

## The HDMI/DP path: DSC link (4K120 on 10G)

```
CLOSET producer                                   DESK consumer
RTX 5090 DisplayPort out                          monitor (DSC-capable) in
 │ short certified cable                            ▲ short certified cable
 ▼                                                  │
LT6911GXD: DP 1.4a (or HDMI 2.1) RX               DSC-forwarding DP TX (open:
 · terminates link training locally                 LT86121GXE decode, a
 · serves SYNTHESIZED EDID + DPCD caps              Lontium DSI→DP part, or
 · HPD state machine with hysteresis                AMD DP TX IP)
 · DSC pass-through (route A) or re-encode (B)      · trains the monitor against a
 ▼ MIPI DSI, ~8 Gb/s                                  local idle pattern
FPGA (e.g. Tang Mega 138K Pro)                      ▲
 · DSI RX → RTP packetizer → 10GBASE-R            FPGA: 10G → elastic buffer →
 ▼ SFP+                                             DSC chunks, pixel clock slaved
UniFi 10G port ════ ~8 Gb/s unicast ════ UniFi 10G port
yoke agent ◄──────────── control plane ───────────► yoke agent
```

### Why DisplayPort, and what the GPU does

On DisplayPort the GPU picks DSC bpp = min(link bandwidth ÷ pixel clock,
the sink's max bpp in DPCD 0x067–0x068). Advertising max 8 bpp and a link
too slow for uncompressed 4K120 yields ~7.96 Gb/s. On HDMI the same GPU
lands at 12 bpp (~11.9 Gb/s), which doesn't fit 10G (`FEASIBILITY.md`
§route A). Route B keeps HDMI viable: advertise **no** DSC and enough FRL
for uncompressed 4K120, then let the LT6911GXD's own DSC encoder
compress to 8 bpp (one generation of DSC, bpp under our control; 9 bpp
is 8.96 Gb/s and needs jumbo frames to fit).

### Session-state ownership (the brain; model it sans-I/O first)

- **EDID and DPCD are synthesized, never passed through.** The closet
  box serves the intersection of (desk monitor caps ∩ receiver-chip caps
  ∩ 10G transport caps): max link rate rewritten to what the receiver
  truly trains; DSC caps set to force the chosen bpp (route A) or cleared
  (route B); DSC slice count, line buffer, and bpp limits no looser than
  the desk monitor accepts, because the GPU's PPS must be decodable at
  the desk; modes pruned to what 10G carries; checksums recomputed.
  Served from local storage at all times, including network-down.
- **HPD with hysteresis and a local-training rule.** Assert when
  (receiver ready ∧ pairing active ∧ desk sink ready), where desk sink
  ready = monitor attached ∧ its EDID/DPCD captured ∧ the desk TX trained
  against a **locally generated idle pattern**. The TX must not need the
  network stream to train, or bring-up deadlocks (bridge TX parts
  typically want input video; an explicit Lontium NDA question).
- **Transient transport loss does not deassert HPD.** The closet box
  holds HPD and the desk freezes or blanks for a bounded window; only a
  deliberate unpair or monitor detach deasserts. Otherwise every network
  blip is a GPU-visible hot-unplug and desktop reshuffle.

### Transport and latency

- Hand-rolled RTP over 10GBASE-R (verilog-ethernet is proven on Xilinx;
  a Gowin 10G MAC is unverified ⚠). ~8 lines of packetizer buffering +
  wire + an elastic receive buffer ⇒ **~50–300 µs one-way** (I), which
  requires **slaving the desk pixel clock to the ingest rate**. A
  frame-resync bring-up mode costs 8.3–16.7 ms (1–2 frames at 120 Hz) and
  is for bring-up only.
- Unicast, consumer-initiated at the media layer: the desk endpoint
  requests the stream from the closet endpoint (the usbip pattern), so
  the control plane is unchanged. Paced; producer and consumer on one
  switch where possible (`HARDWARE.md` §network). The DSC stream is ~80% of a 10G port, so it
  gets a dedicated port and is not mixed with the USB traffic on a 10G
  uplink.
- Acceptance instrument: the photodiode glass-to-glass rig.

### Open questions (top of the Lontium/L2Tek list)

1. LT6911GXD: are DPCD max-bpp and the DSC pass-through / re-encode path
   configurable from firmware; how is compressed data packed on DSI?
2. Desk side: which part re-emits a DSC stream from DSI (or decodes it)
   to a DP or HDMI 2.1 monitor, and does it train without input video?
3. Demo-board availability and price.
4. Windows NVIDIA driver DSC policy on DP matches the open-source code ⚠.
5. LT6911GXD DSI port and lane configuration: a 3-lane 2.5G connector
   carries 7.5 Gb/s, so ~8 Gb/s needs dual-port DSI (or a 4-lane link).

### Fallback video routes

- **Commodity 4K120 (route D):** MS-A2 + Yuan SC750N1-L sending SVT-JPEG-XS,
  MS-A2 receiving: retail parts now, ~15–32 ms added.
- **4K60 commercial:** Just Add Power (1G, per-device) or ZeeVee
  ZyPer4K-XS (10G, sub-100 µs, needs a ZMP server): HDCP, no 4K120.

## The protected path: HDCP pass-through (ADR 0011)

```
Apple TV / Shield ── HDMI 2.0 ──►  CLOSET: FPGA transceivers, raw TMDS
                                    · synthesized EDID (format steering)
                                    · DDC proxy: HDCP messages tunneled
                                      verbatim to the display
                                    ▼ 10G unicast, clock-locked
                                   DESK: FPGA regenerates identical
                                    TMDS timing ──► display (HDCP sink)
```

The bridge forwards encrypted pixels unchanged, and the source
authenticates the real display. Rates, timing, and handshake
requirements are in `../research/FEASIBILITY.md` §HDCP; the rules the
bridge must obey are ADR 0011 clause 3.

- **Format steering.** On 10G the synthesized EDID offers only formats
  whose ciphertext fits: 4K60 at 4:2:0, any 4K24, 1080p to 120 Hz.
  HDCP capability registers are never touched; they belong to the
  display and are read through the tunnel.
- **HPD and loss.** The DSC link's HPD rules apply, but a transient loss
  here cannot be hidden by freezing: the display loses encryption sync
  and the source re-authenticates, a brief blank.
- **Strictly one consumer per producer**, enforced on the producer side
  as for USB (`ARCHITECTURE.md` §what yoke is not): a second display
  cannot decrypt the same session.
- **Hardware:** an FPGA with at least seven ≥6 Gb/s transceivers per end
  (3 TMDS lanes in, 3 out, 1 SFP+) plus TMDS retimer front-ends. Open
  HDMI capture gateware is 1080p-class and abandoned (Aug 2026), so
  HDMI 2.0 rates mean new gateware. Board choice is open ⚠.

## The USB path

### Finding zero: what the WinWing "USB3 video devices" are

| Class | Devices | USB | Traffic |
|---|---|---|---|
| **DisplayLink USB monitor** | "USB3.0 Instrument Display1" (D1, $123.50) and bundles containing it (Top Gun MIP 2 = 3× D1) | USB 3.0, hardware USB2 fallback | Host-compressed DisplayLink **bulk** video, 1024×768@60; est. 50–350 Mb/s per display ⚠ |
| **Self-rendering HID panels** | MCDU, PFP, ICP, UFC, FCU/EFIS, PAP3… | USB 2.0 vendor HID, VID 0x4098 | 64-byte HID reports; KB/s |

⚠ The MCDU-32 screen transport is unconfirmed (possibly DisplayLink).
Day-one bench rule: `lsusb -t` every owned device; reclassify anything
that enumerates SuperSpeed.

Consequences:

- Only the D1 displays need USB3 throughput; worst case 3× D1 ≈ 1.05 Gb/s
  fits 2.5GbE. The desk box needs 2.5GbE, not 10G.
- D1 pixels originate on the PC (DisplayLink driver), so the bulk stream
  must cross the network intact; terminate/regenerate applies to the USB
  *sessions*, not the D1 pixels.
- Bulk and interrupt only, no isochronous. LAN RTT (~0.2–0.5 ms) is far
  inside enumeration timeouts.
- WinWing gear is power-fragile: supply 900 mA+ per port, wire the D1
  aux-5V feeds, disable autosuspend.
- SimAppPro (Windows) needs raw HID access to VID 0x4098 and is the
  firmware updater. **Firmware flashing across the network is forbidden
  by policy** (maintenance mode: direct USB).

DisplayLink operational risks, independent of transport: Windows 11 24H2
broke WinWing MIP DisplayLink drivers (pin the driver, defer feature
updates); DisplayLink conflicts with NVIDIA G-Sync, so **G-Sync + D1
coexistence is a go/no-go bench measurement** before transport work.

### Architecture

```
CLOSET                                   DESK
PC: sim, SimAppPro, DisplayLink,         WinWing D1 + HID panels, HOTAS, kb/m
    VirtualHere client,                   ▲ powered hubs, D1 aux 5V
    yoke PC-side watchdog                 │
 │                                       NanoPi R76S: real local USB3 host
 ▼ 2.5G+ into UniFi  ══ fabric ══         · VirtualHere server
yoke agent ◄──── control plane ────►      · yoke agent + USB watchdog
```

Termination options at the PC, in deployment order:

- **A — VirtualHere (bench now; likely production).** Linux server at the
  desk, Windows client on the PC: the only pairing with full USB3 bulk
  streaming. Throughput is latency-bound (347–700 Mb/s); if 3× D1 exceeds
  it, run one server instance per D1. **USB3 DisplayLink over VirtualHere
  is unproven** (only USB2 DisplayLink is shown working): the load-
  bearing bench question, $0 to answer with the free trial.
- **B — Icron Raven 3204C Pro (contingency, $1,295).** Hardware USB 3.2
  over a switched LAN, all transfer types, no PC software. Hard
  constraints: same subnet, 10GBASE-T at both units, exactly one switch
  between them. **Waiver:** it forfeits requirements 1 and 6 (button
  pairing, sealed session state), so it is excluded from the mesh; if
  chosen, add smart-PDU power cycling driven by the yoke watchdog.
- **C — yoke-native gadget (staged).** USB2/HID works today (raw-gadget +
  usb-proxy, one UDC per device identity). SuperSpeed needs a hand-built
  FunctionFS gadget on an SS UDC (ArmSoM Sige7 / BPI-M7, one board per
  identity); only if A and B fail.

**Watchdogs on both sides.** Desk: keepalives, stall detection,
re-enumeration, per-port power cycle (switched hub ports), PID-change
tolerance (WinWing role switching changes PIDs). PC: a yoke watchdog
agent driving VirtualHere's named-pipe API — health-check, force
detach/reattach on stall, restart the service, alert. Field reports show
the client side wedges too.

**The mesh seam:** the `vhusb` driver maps pairing intents to VirtualHere
attach/release calls, exactly the `usbip` driver pattern. Until it
exists, the USB path is one fixed PC↔desk link.

## Milestones

- **M1 — handshake brain (software, now):** EDID/DPCD synthesis and the
  HPD hysteresis state machine, modeled sans-I/O in the `yoke.media`
  style with offline tests. In parallel, **open the L2Tek engagement**
  (longest lead) with the questions above.
- **M2 — USB over the fabric (~$150 of parts):** R76S + hub +
  VirtualHere trial → `lsusb -t` classification → D1 bandwidth under DCS
  → G-Sync coexistence go/no-go → SimAppPro verdict → both-side watchdog
  soak with forced link drops → 3× D1 scaling → decide A vs B vs C.
- **M3 — integrate behind yoke:** watchdogs both sides; the `vhusb`
  driver makes the USB mesh real.
- **M4 — DSC link:** Lontium samples → DSI capture on the FPGA → 10G RTP
  loopback → desk TX → clock-slaved end to end → photodiode acceptance.
- **M5 — HDCP pass-through:** DDC/HDCP
  2.x message proxy modeled sans-I/O with offline tests → 1080p60
  pass-through on the bench → 4K60 4:2:0 over 10G with a real protected
  source.
