# Hardware research: what exists, what to buy, what to avoid

> **USB selection update:** the current product uses two Radxa ZERO 3E
> hardware boxes and targets USB 2 first. See the
> [agreed USB design](../design/USB-ENDPOINT.md). Earlier NanoPi/VirtualHere
> and USB3 recommendations in this dated study are not the selected design.


Status: live-web research 2026-09-24 (four lanes: commercial endpoints,
USB, 4K120 compression path, commodity capture). The verdict these
support is in `FEASIBILITY.md`; the endpoint design in
`../design/EDGE-DEVICES.md`. Prices were read on 2026-09-24 unless
flagged, and move fast; re-check before ordering.

Flags: **V** verified on a primary source; **S** search snippet only;
**I** inferred. **⚠ marks a claim that is bench-unverified**: code that
encodes one must isolate it and its tests must state the assumption
(`AGENTS.md`).

## 1. Commercial HDMI-over-IP endpoints

### 4K120

Only one vendor claims it: **BZBGEAR XLink-ONE XTREME** (launched
InfoComm June 2026). HDMI 2.1 FRL5/6 in, 4K120 4:4:4 **over 1GbE** with
a proprietary codec (≥26:1, so not transparent), "<1 frame", HDCP 2.3,
USB 3.1 claimed, PoE, 132×75×20 mm, 12 W, $899, "Shipping Q4 2026",
unavailable at retail; no public device API (V,
[product](https://bzbgear.com/product/bg-ipgear-xtreme-4k-uhd-hdmi-over-ip-multicast-transceiver-with-seamless-matrix-switching-multiview-kvm-usb-3-0-poe-support/),
[retail](https://avlgear.com/products/bzbgear-bg-ipgear-xtreme-4k-hdmi-2-1-av-over-ip-transceiver-with-matrix-switcher)).
Watch item: bench one pair when it ships ⚠.

Everything else checked tops out at 4K60 (HDMI 2.0 class): Semtech
BlueRiver/SDVoE (newest AVP1000/2000/2000T; the 2026 roadmap has no
4K120), Just Add Power, Crestron NVX, Extron NAV, Lightware, Kramer,
Atlona, WyreStorm, AVPro MXNet, Aurora, Matrox, Magewell, Blackmagic 2110,
Adder, IHSE Draco, Black Box Emerald (V/S). G&D reaches 4K120 only over
dedicated fiber, not switched IP (S). "HDMI 2.1" on a spec sheet (e.g.
ASPEED AST1530) does not imply 4K120 (S).

### Smallest 10G endpoints (4K60 4:4:4, SDVoE)

SDVoE compresses 4K60 4:4:4 at ~1.3:1 (visually lossless) and sends 4K30
4:4:4 and 4K60 4:2:0 uncompressed; latency <100 µs (V,
[sdvoe.org](https://sdvoe.org/technology/specifications/)). USB is 2.0.

| Unit | Size | Power | USB | HDCP | Price |
|---|---|---|---|---|---|
| **ZeeVee ZyPer4K-XS** | 199×116×47 mm (1.08 L) | ~8 W, 10G PoE | 2.0 | 2.2/2.3 | $1,536–1,689 (S) |
| ZeeVee ZyPer4K-XSE | 199×136×48 mm (1.30 L) | ~13 W, 10G PoE+ | HID or full 2.0 | 2.3 | $1,860 (S) |
| AVPro MXNet 10G TCVR | 215×140×25 mm (0.75 L) | 9.1 W, 802.3af | 2.0 | — | — |
| Aurora IPX-TC3A-Pro | 175×102×28 mm (0.49 L) | 12.9 W, PoE | 2.0 / ExtremeUSB | 2.2 | **discontinued** |
| SDVoE Decora wall plates (Liberty DigiIP 6000, Aurora WP3-Pro, ZeeVee XS-WP) | in-wall | PoE+ | 2.0 (Icron) | — | Liberty decoder $1,121 |

Sources: [ZyPer4K-XS datasheet](https://www.zeevee.com/wp-content/uploads/2024/10/ZyPer4K-XS-Z4KDECC3XS-Data-Sheet.pdf),
[ZyPer4K-XSE datasheet](https://www.zeevee.com/wp-content/uploads/2025/09/zyper4k-XSE-datasheet.pdf),
[AVPro](https://avproglobal.com/products/ac-mxnet-10g-tcvr),
[Aurora](https://auroramultimedia.com/products/ipx-tc3a-pro).

### Control model: who can be routed per device?

| Family | Control | Fit for yoke |
|---|---|---|
| **SDVoE (ZeeVee)** | Every SDVoE system needs an API server; clients talk to it, not to endpoints (V, [Semtech](https://blog.semtech.com/how-sdvoe-enables-interoperability-in-pro-av)). ZeeVee's ZMP runs on a NUC or VM and is licensed per endpoint; its Telnet API (port 23) is public: `join <enc> <dec> video|fastSwitched|genlocked`, `join … usb` (V, [ZMP API manual](https://www.zeevee.com/wp-content/uploads/2024/06/zmp-api-manual-3.2x.pdf)). The BlueRiver endpoint protocol is not public. | A site ZMP server that per-device wardens would drive (`zmp` driver candidate). **ZMP is a central controller: using it waives the no-controller rule.** Whether video keeps flowing with ZMP down is unverified ⚠. |
| Aurora (SDVoE) | "Built-in control server" + Telnet API in the endpoint (V) | Per-device, but box models discontinued |
| **Blackmagic 2110 IP Mini** | NMOS IS-04/IS-05 on the device (V) | Per-device (`nmos` driver candidate). $345–429, 10G, PoE++, µs latency; **refuses HDCP sources, no USB** (V) |
| Crestron DM NVX (1G) | REST on the device: `/Device/StreamReceive/Streams/x/` (V) | Per-device |
| Matrox ConvertIP (1G/2.5G, IPMX) | NMOS IS-04/IS-05 + REST on the device, PoE (V) | Per-device (`nmos`) |
| Adder ALIF4001 | "Self-managed distributed KVM switch" without the AIM manager (V) | Per-device; 4K120 not listed |
| **Just Add Power (1G)** | See below | Per-device, the most yoke-native model |
| Lightware UBEX, Extron NAV, WyreStorm NHD | Central manager required (S/V) | Excluded |

**Just Add Power** (the `jap` driver's target, ⚠ bench-unverified): every
encoder streams continuously to a multicast group derived from its
channel number (video 239.92.0.0 base; channel 0100 → 239.92.1.0; audio
239.93.x.x; USB 239.94.x.x). A decoder re-pairs by being told a channel:
it IGMP-leaves the old group and joins the new one. API on each device:
Telnet port 23 (`channel 0100`) or HTTP justAPI
(`POST /cgi-bin/api/command/channel`); state persists on the decoder.
802.3af PoE, HDCP 2.2/2.3, USB 2.0 KVM in-band. MaxColor tops out at 4K60
4:4:4 over **1G only** (the earlier "10G SFP option" claim is refuted, V,
[MC-TX1 spec](https://www.justaddpower.com/wp-content/uploads/2023/02/jp-mc-tx1-spec.pdf)).
Sources: [multicast switching](https://support.justaddpower.com/kb/article/346-multicast-switching-protocol/),
[single-VLAN switching](https://justaddpower.happyfox.com/kb/article/279-single-vlan-multicast-switching-with-omega-ultra-and-maxcolor-series/),
[Telnet](https://support.justaddpower.com/kb/article/330-how-to-telnet-to-a-device-omega-ultra-and-maxcolor-series/).
Risks: command vocabulary per firmware, re-pair glitch duration,
in-band USB behavior on channel change ⚠.

## 2. USB over IP

### Hardware extenders and hubs

| Product | USB path | Network constraints | Re-pairing | Size | Price |
|---|---|---|---|---|---|
| **Icron Raven 3204C Pro** | Hardware, driverless; USB 3.2 Gen1; all four transfer types; ≤30 devices | Same subnet; **≥10GBASE-T; exactly one switch** between LEX and REX | Mode button only; no API | LEX 165×100×35 mm, REX 197×100×35 mm | $1,295 (V, Markertek) |
| Aten KX9980T/R (Mar 2026) | USB 3.2 Gen1 incl. isochronous, in KVM-over-IP | 10G; USB 3.2 works only when managed by CCKM (central manager) | CCKM | T 215×163×42 mm | ~$3,118 per TX (S) |
| Digi AnywhereUSB 2/8 Plus | Driver-based; USB 3.1 Gen1 ports; isochronous | 8 Plus: 10GbE; routable | Host client CLI `CONNECT/DISCONNECT` | 2 Plus 118×74×30 mm | 8 Plus ~$1,375–1,834 (S) |
| SEH utnserver ProMAX | Driver-based; 4× USB 3.2 Gen1 | 2.5GbE | UTN Manager | — | — |

Sources: [Raven manual](https://www.syscomtec.com/_produktbereich/source/Artikel/Artikel%209504/icron-usb-3-2-1-raven-3204c-pro-manual.pdf),
[Raven price](https://www.markertek.com/product/icr-3204c-pro/icron-raven-3204c-pro-4-port-usb-3-2-1-656-feet-200m-10gbe-lan-or-328-feet-100m-cat-6a-7-switch-extender-system),
[Aten KX9980T](https://www.aten.com/us/en/products/kvm/kvm-over-ip-matrix-system/kx9980t/),
[Digi guide](https://docs.digi.com/resources/documentation/digidocs/pdfs/90002383.pdf).
Black Box Emerald and Adder carry USB 2.0 only.

### Software

- **VirtualHere** (the mesh path). USB3 "including bulk streaming" is
  fully implemented only for **Linux server → Windows client** (V,
  [node/3280](https://www.virtualhere.com/node/3280)). Throughput is
  latency-bound: 347 Mb/s measured by a user; "up to about 700 Mbps"
  with bulk streaming per the developer (V,
  [node/4243](https://www.virtualhere.com/node/4243)). DisplayLink
  probably does not use bulk streams, so expect the low end (I). $49 per
  server, perpetual, hardware-bound, unlimited devices; free trial shares
  one device (V). arm64 server builds exist. Windows client API: named
  pipe `\\.\pipe\vhclient` with `LIST`, `USE,<addr>`,
  `STOP USING,<addr>`, `AUTO USE …` (V,
  [client_api](https://www.virtualhere.com/client_api)). The "Device
  Client" (network → gadget) is Raspberry Pi 4/5 only, USB2, one device
  (V).
- **Linux usbip.** vhci-hcd has supported SuperSpeed since 2017 (V);
  10/20 Gb/s devices are rejected pending a June 2026 patch (V); vhci
  fixes are still landing (I). Windows client: usbip-win2 (V). No modern
  throughput figures found.
- **DisplayLink over USB-over-IP:** only a USB2 DisplayLink display (MIMO,
  17e9:02ff) is shown working, via VirtualHere in 2019 (V). **USB3
  DisplayLink over any of these is unproven** ⚠.

### Gadget side (presenting devices to a PC from a Linux box)

- **raw-gadget** (mainline since 5.7): **no SuperSpeed** (issue #61, open
  since 2023; the uapi takes no SS companion descriptor, V); limited
  isochronous; cannot emulate a hub, so **one UDC = one device identity**.
  `usb-proxy` (raw-gadget + libusb) is local-only.
- **FunctionFS** supports SuperSpeed descriptors, so a hand-built
  configfs/FFS gadget on dwc3 can run at SS, but each cloned device's
  descriptors must be written by hand (V header, I feasibility).
- **SuperSpeed UDCs:** RK3588/RK3588S dwc3 OTG0 (USB-C, Gen1) — treat
  RK3588 boards as having **one** practical SS UDC; RK3576 Type-A device
  mode (documented for Android ADB only); Raspberry Pi 5 is USB2 only;
  CIX P1 is host-only.

### Small Linux boards (USB endpoint candidates)

No small ARM board has both native 10GbE and a SuperSpeed UDC.

| Board | Size (mm) | Network | USB host | SS device port | Price, stock |
|---|---|---|---|---|---|
| **FriendlyElec NanoPi R76S** (RK3576) | 62.5×62.5×29 cased | 2× 2.5GbE | 1× USB3-A | Type-A device mode, Linux unverified | $75–120, in stock |
| Radxa E52C | 66×66 | 2× 2.5GbE | 1× USB3 | no | $55, sold out |
| NanoPi R6C / R6S | 62×90 | 2.5G+1G / 2×2.5G+1G | 1× USB3 + 1× USB2 | as R76S | $149 / $212 |
| Orange Pi 5 Max | 89×57 | 1× 2.5GbE | 2× USB3 + 2× USB2 | unclear | $174.99 |
| Radxa ROCK 5C | 86×56 | **1GbE only** | 1× USB3 + 2× USB2 | yes (Type-A OTG) | $69+, out of stock |
| **ArmSoM Sige7 / Banana Pi M7** | 92×62 | 2× 2.5GbE | 1× USB3 + 1× USB2 | **yes, USB-C 3.0 OTG + separate PD power** | $209.95–255, thin stock |
| FriendlyElec NanoPC-T6 | 110×80 | 2× 2.5GbE | 1× USB3 + 2× USB2 | yes, USB-C | $193, in stock |
| Radxa ROCK 5B+ / 5T | 100×75 / 110×82 | 1× / 2× 2.5GbE | USB3 | yes, USB-C | sold out |
| NXP FRDM-IMX95-PRO | 100×90 | 10GbE via SlimSAS + 2× 1GbE | 1× USB2 | USB3-C, dual-role unverified | $300, pending |

Sources: [R76S](https://wiki.friendlyelec.com/wiki/index.php/NanoPi_R76S),
[Sige7](https://docs.armsom.org/armsom-sige7),
[NanoPC-T6](https://wiki.friendlyelec.com/wiki/index.php/NanoPC-T6),
[ROCK 5C OTG](https://forum.radxa.com/t/does-rock-5c-support-usb-otg-peripheral-mode/25090).

## 3. 4K120 ingest, capture, and compute

### Lontium FRL receivers (the only merchant 4K120 ingest)

Merchant FRL-to-pixel silicon is essentially Lontium alone; datasheets,
init sequences, firmware, and demo boards are **NDA-gated via L2Tek
(UK)**, and no public Linux driver exists. Chips are ~$10–25 (Aug 2026
estimate).

| Part | Role |
|---|---|
| **LT6911GXD** | HDMI 2.1 FRL5 + DP 1.4a in; DSC pass-through on MIPI DSI; own DSC 1.2a encoder (1/2/4/8 slices, 1/16-bpp steps). QFN88, "Preview" (V). The route-A/B part. |
| LT6911GX / GXC | FRL4 in; GX's DSI can carry a "compressed pixel stream" (V) |
| LT7911UXE | FRL5 in; one AliExpress demo board seen (Aug 2026) |
| LT86121GXE | "Paired" HDMI 2.1/DP 1.4a repeater with DSC encoder + decoder (V); candidate desk-side DSC decoder, input is HDMI/DP not MIPI |

Fallback ingest: Bitec FMC HDMI 2.1 card (€595) + Bitec or AMD HDMI 2.1
IP (quote-only). Bitec's IP offers an optional DSC data path and supports
Efinix Titanium (V).

### Capture devices

| Device | Interface | At 4K120 the host gets | Raw max | Price |
|---|---|---|---|---|
| Elgato 4K X | USB 3.2 Gen2 | **MJPEG** | 4K60 NV12 | $249.99 |
| AVerMedia GC575 / GC553G2 | PCIe Gen3 x4 / USB 3.2 Gen2 | **MJPEG** | 4K60 | ~$270–300 |
| Magewell (all) | PCIe / USB / M.2 | no 4K120 product | 4K60 | $300+ |
| **Yuan SC750N1-L** | PCIe Gen3 x8, low profile | raw (RGB24/YUY2/NV12/P010 listed; which work at 120 is unstated) | raw | $1,399 |
| **Blackmagic DeckLink 8K Pro G2** | PCIe Gen3 x8, **two slots + PSU power** | raw 4:2:0/4:2:2/4:4:4, 8–12-bit; broadcast timings only listed ⚠ | raw | $855 |
| Deltacast DELTA-hmi-e 40 | PCIe Gen3 x8 | raw, FRL3–6, no DSC | raw | — |

Sources: [Elgato 4K X](https://www.elgato.com/us/en/p/game-capture-4k-x),
[GC575](https://www.avermedia.com/product-detail/GC575),
[SC750N1-L](https://www.yuan.com.tw/product/6),
[DeckLink 8K Pro G2](https://www.blackmagicdesign.com/products/decklink/techspecs/W-DLK-37).
The Elgato 4K X is also the cheapest FRL 4K120 sink **instrument**.

### FPGA boards and codec IP

| Board | Relevant I/O | Price, size |
|---|---|---|
| **Sipeed Tang Mega 138K Pro** (Gowin GW5AST, 138k LUT4) | 2× SFP+; 8× 12.5G SERDES; 2 MIPI connectors × 3 lanes × 2.5G hard D-PHY (7.5 Gb/s each: an ~8 Gb/s DSC stream needs both, i.e. dual-port DSI ⚠) | ~$195–239 (S); module 70×50 mm. Cheapest board for the Lontium DSI path; Gowin 10GBASE-R MAC unverified ⚠ |
| AMD KR260 (K26 SOM, 4 GTH 12.5G) | 1× SFP+ | $349 (S); SOM 77×60 mm |
| ALINX AXU5EVB-P (ZU5EV) | 2× SFP+, 2-lane MIPI, HDMI in/out | $1,288; 200×111 mm |
| Efinix Ti375 N1156 kit | 4× SFP+, 3 hard MIPI RX | $2,493.75 (S); the N484 package (18×18 mm) suits a custom board |
| Lattice CertusPro-NX Versa | 2× SFP 10G; soft D-PHY | — |

| Codec IP | Licensing | Notes |
|---|---|---|
| Rambus (ex-Hardent) DSC 1.2b enc/dec | netlist, quote | sub-line; AMD and Intel parts |
| Bitec DSC 1.2a | quote | sub-line |
| intoPIX TicoXS (JPEG XS) | quote; SKU IPX-TICO-XS-4K-120-444 | ~0.1 ms; AMD/Altera/Lattice |
| IHSE/Fraunhofer JPEG XS | quote | 4K60 enc ≈ 42k LUT on UltraScale+ |
| JPEG XS patent pool (Vectis) | **$5 per 4K instance**, lowest tier (V) | applies to any JPEG XS product |
| Open source | **no DSC or JPEG XS HDL exists** | a C11 DSC decoder bit-exact vs VESA (golden model): [dscdecode](https://github.com/zaclanzon/dscdecode) |

Zynq US+ EV VCU tops out at an aggregate 4K60, so it cannot do 4K120 (S).

### Small x86 boxes (commodity route D)

| Box | Size | 10G | Capture attach | 4K120 out | Price |
|---|---|---|---|---|---|
| Minisforum MS-A2 (Ryzen 9 9955HX, 16 Zen5) | 1.78 L | 2× SFP+ | PCIe 4.0 x8 half-height (SC750N1-L fits) | HDMI 2.1, USB-C DP | $799 barebone |
| Minisforum MS-01 | 1.78 L | 2× SFP+ | same slot | HDMI 2.0 only | $679 barebone |
| Beelink GTi15 Ultra | 1.39 L | 2× 10GBASE-T | USB4 only | DP 1.4a, USB4 | $1,249 |
| ASUS NUC 15 Pro | ~0.5 L | via Thunderbolt 10G adapter | USB only | via TB4/DP only | — |

SVT-JPEG-XS is x86-only (no NEON), so there is no open JPEG XS decoder
for ARM or Apple silicon (V). Intel's Media Transport Library does ST
2110-22 with JPEG XS at 4K120 and is validated on E810/E830 NICs (V).

## 4. The network (UniFi)

Facts from Aug 2026 research that still hold; a 4K120 DSC stream is
~8 Gb/s, so the 10G rules below are tight, not generous.

1. Dedicated AV VLAN; IGMP snooping on; **exactly one pinned querier**
   (without periodic queries snooping ages out in ~4 min and streams
   flood); fast-leave; jumbo frames on every AV port and trunk (one
   mismatched trunk silently breaks video).
2. **Unicast for v1 high-rate flows.** A mis-snooped ~8 Gb/s multicast
   flood takes down every port it reaches. Multicast returns with the
   many-endpoint mesh, after a flood-containment drill. Unicast must be
   consumer-initiated at the media layer (the consumer's driver requests
   the stream from the producer endpoint, as usbip does). Having the
   producer *agent* pick a destination would contradict `PROTOCOL.md`
   ("producers are passive") and needs an ADR first.
3. **Capacity:** one 4K120 DSC stream fills ~80% of a 10G link. Keep
   producer and consumer on one switch, or give inter-switch links more
   than 10G. Single flows cannot be split across LACP members. Video and
   D1 USB traffic both flow closet→desk (~8 + ~1 Gb/s), so they cannot
   share one 10G uplink comfortably.
4. **Buffers are shallow** (4–8 MB on aggregation switches), so the
   producer must pace.
5. UniFi Gen 2 switches have documented IGMP snooping defects ⚠; the
   Netgear M4250 (AV line, IGMP preconfigured) is the escape hatch.
6. UniFi 25G SFP28 exists only on aggregation switches (Pro-Aggregation
   ~$970 in runout, Pro-XG-Aggregation $2,696). The Enterprise-AV line has
   10G access + 100G uplinks, no 25G access (Aug 2026).
7. 10GBASE-R has no mandatory FEC, so the 25G FEC risk flagged in Aug
   2026 does not apply at 10G.

## 5. Shortlists (pending the v1 trade decision in `HANDOFF.md`)

| Need | Smallest viable pick | Why |
|---|---|---|
| USB, device end (desk) | NanoPi R76S + VirtualHere server ($49) + powered hub | Smallest in-stock board with 2× 2.5GbE; ~1 Gb/s D1 worst case fits |
| USB, PC end | VirtualHere Windows client (no hardware) | The only full-USB3 VirtualHere pairing |
| USB SS gadget (bench only) | ArmSoM Sige7 / BPI-M7, one per device identity | SS OTG + separate power |
| 4K60 + HDCP mesh, no controller (buy) | Just Add Power MaxColor (1G, compressed) | Per-device API; `jap` driver exists |
| 4K60 + HDCP on 10G (buy, waiver) | ZeeVee ZyPer4K-XS, or SDVoE wall plates | <100 µs, PoE, public API, but needs a ZMP site server |
| Unprotected 4K60 mesh (buy, per-device API) | Blackmagic 2110 IP Mini + separate USB path | NMOS on device, $345–429 |
| Protected 4K60 4:2:0 / 4K24 on 10G (build, ADR 0011) | FPGA with ≥7 transceivers ≥6 Gb/s + TMDS retimers per end; board open ⚠ | HDCP pass-through (`FEASIBILITY.md` §HDCP) |
| 4K120 near-transparent (build) | Closet: LT6911GXD (NDA) + Tang Mega 138K Pro; desk: Tang Mega + a DSC-forwarding TX (open question) | Route A/B in `FEASIBILITY.md` |
| 4K120 commodity (~15–32 ms) | MS-A2 + SC750N1-L (send); MS-A2 (receive) | Retail parts, x86 JPEG XS; Zen5 throughput not benchmarked (I) |
| Bench instruments | HDFury Dr HDMI 8K ($199, EDID/HPD scripting), Elgato 4K X (FRL sink), photodiode glass-to-glass rig | Aug 2026 picks, still valid |

## 6. yoke drivers

| Driver | Target | Mechanism | Status |
|---|---|---|---|
| `sim` | tests | in-memory | v1 |
| `igmp` | open Linux consumers | kernel IGMP join | v1 |
| `usbip` | Linux USB endpoints | usbip attach/detach | v1 |
| `jap` | Just Add Power decoders | Telnet/justAPI `channel N` | v1, ⚠ bench-unverified |
| `stream` | software media pipeline | out-of-process pipeline | v1 |
| `vhusb` | VirtualHere clients | named-pipe client API | candidate (USB mesh) |
| `nmos` | Blackmagic 2110, Matrox, IPMX | IS-05 PATCH on the device | candidate |
| `zmp` | ZeeVee SDVoE via a ZMP sidecar | Telnet `join` | candidate |
| DSC-link endpoint | our 4K120 endpoints | register/API of our gateware | candidate, after bench |
| pass-through endpoint | our HDCP pass-through endpoints | register/API of our gateware | candidate |
