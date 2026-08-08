# Hardware research: what to buy, what to avoid, what is impossible

Status: researched 2026-08-08 (nine-lane web research + synthesis; per-lane
detail and sources in `lanes.md`). Bench verification still pending for
every claim marked ⚠ in Risks.

## The three hard truths

1. **Bit-transparent HDMI over 1GbE is impossible.** 4K60 4:4:4 HDMI is
   12–18 Gb/s raw; every 1GbE product compresses (JPEG2000/proprietary
   wavelet, ~300–900 Mb/s, sub-frame latency). "Transparent" therefore
   means: licensed endpoint silicon encodes/decodes and handles HDCP;
   yoke software never touches media (which is ADR 0002/0004 exactly).
2. **USB 3.x over switched Ethernet does not exist** in any product tier.
   USB over the mesh is a USB 2.0 (480 Mb/s nominal) service, period.
3. **HDBaseT does not traverse Ethernet switches** (point-to-point PHY
   over Cat cable). It is out, except as a last-100 m extension behind an
   IP endpoint.

## HDMI mesh: recommendation

**Primary: Just Add Power (Ultra / MaxColor families).** The only
surveyed ecosystem whose documented architecture *is* yoke's design:

- Every encoder streams continuously to a multicast group derived from
  its channel number (video 239.92.0.0-base; audio 239.93.x.x; USB
  239.94.x.x; e.g. channel 100 → 239.92.1.0).
- A decoder re-pairs by being told a channel number — it IGMP-leaves the
  old group and joins the new one. The switch's IGMP snooping does the
  routing. No controller, no switch reconfiguration, no encoder
  involvement.
- The API is on each device: Telnet port 23 (`channel 0100`) or on-device
  HTTP "justAPI" (`POST /cgi-bin/api/command/channel`), publicly
  documented; state persists on the decoder across power cycles.
- All units 802.3af PoE; HDCP 2.2/2.3 in endpoint silicon; Ultra/MaxColor
  carry USB 2.0 KVM in-band (switches with the channel).
- Prices: Ultra ~$400–700 street (used 508POE ~$350); MaxColor (4K60
  4:4:4) ~$1,000–1,410 list, dealer-gated. Avoid the legacy 2G series
  (VLAN-switching generation).

**Fallback: Visionary Solutions PacketAV Duet (E/D4200, DuetE-5/D-5)**
(~$1,200–1,500; PoE, USB KVM, IGMP multicast, controller optional) —
conditional on obtaining their integrator-gated device API document
first. Note their default multicast range (225.x) may need remapping
into a disciplined 239/8 plan.

**Standards-based premium: IPMX (Matrox ConvertIP SRH/DRH,
~$1,900–2,300, PoE+).** NMOS IS-04/IS-05 REST on the device itself,
connection state persists in the endpoint, registry optional
(peer-to-peer mDNS), JPEG-XS sub-frame latency, HDCP-over-IP in the
spec. ~2× the cost, but the same control code scales from 1G JPEG-XS to
10G/25G uncompressed ST 2110 — the most future-proof driver target.

**Disqualified:** WyreStorm NetworkHD (NHD-CTL controller mandatory),
AVPro MXNet (CBOX mandatory), ZeeVee ZyPer/SDVoE (ZyPer Management
Platform required for all installs — the SDVoE API ships as an always-on
vendor service), generic $50–700 H.264/H.265 boxes (100–300 ms latency,
HDCP-noncompliant → legal exposure per ADR 0004, not production-grade).

## USB mesh: recommendation

No COTS product combines transparent hardware USB termination with
network-triggered any-to-any switching. Assemble it:

- **KVM-follows-video** (the common case): JAP Ultra/MaxColor in-band
  USB 2.0 rides the same channel switch. Default path. ⚠ bench-verify
  re-pair semantics and isochronous behavior.
- **Static transparent links** (webcam pinned to one host): Icron
  ExtremeUSB 2304GE-LAN (~$470) / 2304PoE (~$565, PoE+) — full hardware
  USB 2.0 incl. isochronous over switched 1GbE, but pairing is 1:1 via
  physical buttons. Model these in yoke as fixed pairings.
- **Dynamically switchable USB**: PoE-powered Linux endpoint boards
  running usbip (our `usbip` driver) or VirtualHere ($49/server,
  scriptable client API, no central controller). Limits: effectively no
  isochronous (webcams) over usbip in practice, no USB 3.
- **Multi-host appliance alternative**: Digi AnywhereUSB Plus
  ($500–3,130) where consumers are PCs able to run its daemon.

## Network prerequisites (UniFi)

All checkable, and `yoke snoop` exists precisely to catch the silent
failures:

1. Dedicated AV VLAN (every vendor mandates it).
2. IGMP snooping on that VLAN.
3. **Exactly one pinned IGMP querier** — without periodic queries,
   snooping state ages out in ~4 minutes and every stream floods every
   port (the classic "worked for 4 minutes then melted the LAN").
4. Fast-leave — a 1G decoder port cannot carry old+new ~850 Mb/s streams
   during a switch (2×850M > 1G); without it every re-pair glitches for
   seconds. One endpoint per switch port.
5. Jumbo frames (MTU ≥ ~8000) on all AV ports and trunks — JPEG2000-class
   endpoints require it; one mismatched trunk silently breaks video.
6. Capacity: fabric is non-blocking; limits are one stream per 1G access
   port and one distinct group per 1G inter-switch uplink — keep pairs on
   one switch or use 10G SFP+ uplinks (~11 streams each).
7. PoE budget: endpoints ~13 W 802.3af/at. USW-Pro-Max-24-PoE (400 W)
   powers a full complement; 95 W switches cap at ~6 endpoints.
8. ⚠ UniFi Gen 2 switches have documented IGMP snooping defects
   (membership reports ignored → VLAN flooding). Bench-verify the exact
   model+firmware; Netgear M4250 (AV Line, IGMP preconfigured) is the
   escape hatch.

## HDCP position (confirms ADR 0004)

Licensed encoder+decoder pairs present as an HDCP repeater; the Ethernet
hop uses HDCP 2.x IIA (AES-128-CTR, the DCP-sanctioned HDCP-over-IP
mechanism). Under the HDCP License Agreement (March 2024), software that
only tells licensed endpoints which group to join — never holding keys or
content — is not a "Licensed Product" and needs no license; this is how
Crestron/Q-SYS/SDVoE control software operates. It is a well-supported
reading plus universal industry practice, not an official DCP statement.
Design consequences: HDCP IIA locality check needs ≤7 ms RTT (LAN-only
for protected content); repeater caps of 31 devices/depth 4 bound
protected-source fan-out; re-pairing triggers HDCP re-auth (brief blank);
the snooper sees encrypted payload on protected streams and verifies
flow/IGMP/bitrate only — correct behavior, not a bug. Red line enforced
by policy: yoke never instructs an endpoint to disable or downgrade
protection, and HDCP-stripping boxes are excluded.

## 10G path

SDVoE is genuinely near-transparent (~30 µs, uncompressed to 4K30 4:4:4)
but fails the no-controller requirement today and lacks PoE. The paths
that preserve yoke's architecture: JAP MaxColor's 10G SFP option (same
Telnet/HTTP + IGMP model), or IPMX endpoints where only the transport
tier changes and the NMOS IS-05 control code is identical. Cheap move
now: 10G SFP+ uplinks between switches while endpoints stay 1G.

## What this means for yoke drivers

| Driver | Target | Mechanism | Status |
|---|---|---|---|
| `sim` | tests | in-memory | v1 |
| `igmp` | reference/lab consumers | kernel IGMP join | v1 |
| `usbip` | switchable USB endpoints | usbip attach/detach | v1 |
| `jap` | Just Add Power decoders | Telnet/justAPI `channel N` | v1 (offline-tested against scripted transcripts; ⚠ bench-verify) |
| `nmos` | IPMX (ConvertIP etc.) | IS-05 staged/active PATCH | v2 candidate |
| `vhusb` | VirtualHere clients | client CLI api | v2 candidate |

## Risks / bench-verification queue

- UniFi IGMP snooping defects per model+firmware (fallback: M4250). ⚠
- JAP justAPI surface on current firmware; MaxColor/Ultra feature matrix
  and street pricing (dealer-gated). ⚠
- Re-pair glitch duration (JAP docs: multicast switching "not instant and
  seamless") + HDCP re-auth blank — defines user-facing latency. ⚠
- JAP in-band USB semantics on channel change (arbitration, iso). ⚠
- Visionary/Crestron controller-free claims unvalidated (API docs gated). ⚠
- Latency marketing vs reality (Visionary "~2 ms") — needs glass-to-glass
  measurement. ⚠
- Snooper throughput: sinking a ~900 Mb/s group needs NIC/CPU headroom
  (RK3588/2.5GbE class, not a Pi Zero). ⚠
- Whether vendor streams are standard RTP (deeper passive verification)
  or proprietary framing (flow/bitrate only). ⚠
