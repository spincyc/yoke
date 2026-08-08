# Research lane detail


Generated 2026-08-08 from the nine-lane research workflow; curated
conclusions live in `HARDWARE.md`.


## hdmi-landscape

Truly bit-transparent HDMI over 1GbE does not exist: 4K60 4:4:4 HDMI is ~12-18 Gbps, so 1GbE systems always compress (JPEG2000/proprietary ~2-20ms sub-frame-to-1-frame, or H.264/H.265 ~100-300ms), while "uncompressed/zero-frame" claims require 10GbE (SDVoE) and even SDVoE lightly compresses 4K60 4:4:4. HDBaseT is disqualified for yoke's architecture: it is point-to-point over Cat cable and does not traverse standard Ethernet switches. The best fit for "no always-on controller" is IPMX (ST 2110 + JPEG-XS + NMOS IS-05): connection state lives in the endpoints, an ephemeral CLI can PATCH IS-05 receivers directly, PTP is optional (async mode), HDCP-over-IP is in the spec, and Matrox ConvertIP SRH gives 1G/2.5G PoE+ endpoints at ~$2k each; a passive snooper can watch multicast/SDP traffic. Pragmatic 1GbE alternatives that switch controller-free via IGMP join/leave plus per-device Telnet/REST are Just Add Power MaxColor (~$1,100-1,300/end, PoE) and Visionary Duet-5 (~2ms, PoE+); Crestron DM-NVX (~$1,500) also works without a control processor via its web UI/REST. SDVoE (ZeeVee ZyPer4K, ~$2,000-3,000/end) is the latency king but its control plane is the SDVoE API service, which in practice means a vendor management server (ZeeVee ZMP), and 10G endpoints generally lack PoE — a poor match for UniFi PoE-first homelab today. In every family, HDCP is handled by the licensed endpoint hardware (repeater/IIA), never by your software, satisfying the transparency requirement.


### Findings

- **[high]** Bit-transparent (uncompressed, unmodified) HDMI 2.0 over 1GbE is impossible: 4K60 4:4:4 needs ~12.5-18 Gbps; every 1GbE product compresses (JPEG2000, JPEG-XS, proprietary, or H.264/H.265). Only 10GbE SDVoE approaches uncompressed, and even it applies light compression for full-rate 4K60 4:4:4.
  - Relevance: Sets the fundamental design constraint for yoke: choose a compression tier per link budget (1G now, 10G later); 'transparent transport' can only mean codec-in-endpoint-hardware, not bit-exact passthrough.
- **[high]** — *ZeeVee ZyPer4K / ZyPer4K-XS; Semtech BlueRiver-based endpoints* ($2,000-3,000 per endpoint (Avendor lists ZyPer4K HDMI 2.0 fiber encoder at $2,699 sale / $2,999 list); ZMP license extra, tiered by endpoint count) SDVoE runs over standard 10GbE switches with IGMP-snooping multicast; switching is IGMP join/leave and 'zero-frame' latency (sub-100us class, ZyPer4K advertises uncompressed 4K60 with no frame buffering). However, the control plane is the SDVoE/BlueRiver API, which vendors ship as an always-running management server (e.g., ZeeVee ZyPer Management Platform on a server/VM); third-party controllers talk through that API service, so SDVoE is NOT naturally controller-free. 10G endpoints are typically fiber-SFP+ or 10GBASE-T with local PSUs, not PoE.
  - Relevance: Best latency/quality and clean multicast semantics, but conflicts with yoke's 'no central controller' requirement and UniFi PoE assumption; viable later on 10G if you accept running (or reverse-engineering around) an SDVoE API host.
- **[high]** HDBaseT is a point-to-point PHY over category cable (HDMI+USB+control+power, up to 100m); it does not use IP or standard Ethernet switches — matrix switching requires a proprietary HDBaseT matrix chassis. Bridges like Matrox ConvertIP SRST exist to gateway HDBaseT into IP.
  - Relevance: Disqualifies HDBaseT for yoke's switched-Ethernet architecture; only relevant as a last-100m extension behind an IP endpoint.
- **[high]** IPMX (AIMS, built on SMPTE ST 2110 + NMOS) is the standards-based family that best matches 'no always-on controller': NMOS IS-04/IS-05 connection management is a REST API on each device, connection state persists in the endpoint, the registry is optional (peer-to-peer/manual DNS-SD modes exist), so an ephemeral CLI can PATCH a receiver's IS-05 /single/receivers/{id}/staged to re-point it at a new multicast SDP. Unlike raw ST 2110, IPMX makes PTP optional (three async/sync modes via RTCP timestamp signaling) and natively specifies HDCP carriage over IP. JPEG-XS (ST 2110-22) gives visually lossless sub-frame (<1ms-class) latency on 1GbE.
  - Relevance: Architecturally the closest match to all yoke requirements: multicast, decoder-side switching by device API, no mandatory controller, passive multicast snooping verifiable, HDCP in endpoint hardware, works on 1GbE now and scales to 10G/2110 later.
- **[medium]** — *Matrox ConvertIP SRH / DRH* (~$1,900-2,300 per unit (UK retailer ~£1,957 inc VAT; US street via B&H)) Matrox ConvertIP SRH is a standalone ST 2110/IPMX HDMI encoder/decoder for 1G/2.5G with PoE+, NMOS IS-04/IS-05, optional JPEG-XS license, ultralow latency; DRH variant has 3x RJ45 for 1G workflows, SRST bridges HDBaseT.
  - Relevance: Concrete production-grade IPMX endpoint you could pair per HDMI producer/consumer and drive with an ephemeral NMOS CLI; price is the main downside vs consumer-tier 1G boxes.
- **[high]** — *Just Add Power MaxColor MC-TX2/MC-RX1/RX2* (~$1,100-1,300 per unit (AVLGEAR lists MC-RX1 receiver at $1,280)) Just Add Power (MaxColor 4K60 series) switches sources controller-free: each encoder owns a unique multicast group, and a decoder changes source by changing its multicast 'channel' via Telnet/API, relying only on IGMP snooping in the switch (their docs describe single-VLAN multicast switching explicitly, no vendor controller required). PoE-powered, gigabit, KVM/USB variants exist.
  - Relevance: Direct existence proof of yoke's exact control model (IGMP join/leave + per-device Telnet API, ephemeral controller) on ordinary gigabit PoE switches; a Home Assistant integration exists, showing the API is scriptable.
- **[medium]** — *Visionary Solutions DuetE-5 / DuetD-5 (also E5200/D5200)* (~$1,200-1,700 per unit (retailers list on request; backordered at Creation Networks)) Visionary Solutions Duet-5 (DuetE-5/DuetD-5) is a 1GbE JPEG2000-based 4K60 4:4:4 encoder/decoder with ~2ms sub-frame latency, PoE+ powered on a single Cat5e/6 drop, Dante/AES67 audio, USB 2.0/KVM, IGMP-snooping multicast (PIM for routed multicast), per-device control (RS-232/IR/CEC over IP) with optional Vision Lite software — a controller is not mandatory for routing.
  - Relevance: Strong 1GbE candidate matching PoE + multicast + low latency + no-mandatory-controller; price unconfirmed so verify with a distributor.
- **[medium]** — *Crestron DM-NVX-360* (~$1,490-1,600 street per unit) Crestron DM NVX (e.g., DM-NVX-360) does 4K60 4:4:4 with Pixel Perfect Processing at <1 frame end-to-end latency over 1GbE, HDCP 2.3, AES67, USB/KVM routing, PoE+ powered, multicast-based; devices are configurable via built-in web UI and REST API without a Crestron control processor (XiO Cloud/DM NVX Director are optional).
  - Relevance: Production-grade widely deployed 1GbE option with USB/KVM in the same box; controller-free operation is possible but Crestron's ecosystem assumes their tooling, so script-first workflows need validation.
- **[medium]** — *WyreStorm NHD-500-TX/RX V2* (~$800-1,200 per unit (street, unconfirmed)) WyreStorm NetworkHD 500 series (JPEG2000, 4K60 4:4:4) achieves 1-frame (~16ms) latency on 1GbE with PoE and fiber options, but the NetworkHD ecosystem's control layer expects the NHD-CTL controller appliance for routing management — weaker fit for controller-free operation.
  - Relevance: Illustrates a common trap: good transport specs but a vendor controller baked into the control plane, which violates yoke's no-central-controller requirement.
- **[medium]** ($250-700 per endpoint) Commodity 1GbE H.264/H.265 encoder/decoder appliances (WyreStorm NHD-100/120 class, AV Access, BZBGear, J-Tech, etc.) are the cheapest tier (~$250-700 per endpoint), use RTSP/multicast, often PoE, but typical end-to-end latency is ~100-300ms and quality is GOP-compressed — acceptable for signage/monitoring, not for interactive KVM or gaming.
  - Relevance: Defines the budget floor and its latency cost; also the tier where a passive multicast snooper (e.g., ffprobe on the RTP stream) is easiest to build since streams are standard codecs.
- **[high]** In all viable families, HDCP is terminated/repeated by the licensed endpoint hardware: SDVoE endpoints pass HDCP 2.2 through the BlueRiver ASIC path, DM NVX endpoints are HDCP 2.3 devices, and IPMX explicitly standardizes HDCP-protected content carriage over the IP network between compliant encoders/decoders (DCP's interface-independent adaptation). Your orchestration software only moves opaque streams and never handles keys — but note HDCP streams are encrypted on the wire, so the debug snooper can verify flow/bitrate/multicast membership, not decoded pixels, on protected content.
  - Relevance: Confirms yoke's compliance posture (software never in the HDCP chain) and sets a realistic expectation for what the passive snooper can show on protected sources.
- **[high]** PoE alignment: the 1GbE tier (Just Add Power, Duet-5, DM NVX, NHD-500) is broadly PoE/PoE+ powered and fits UniFi gigabit PoE switches with IGMP snooping enabled; the 10G SDVoE tier generally is not PoE-powered (SFP+ fiber or 10GBASE-T with local PSU), and 10G PoE switch ports are rare in the UniFi lineup, so a later 10G migration implies local power bricks or niche switches.
  - Relevance: Directly informs yoke's homelab deployment plan: 1GbE PoE endpoints now; budget power/cabling differently if moving to SDVoE-class 10G later.

### Sources

- <https://www.zeevee.com/av-over-ip/zyper4k/>
- <https://products.sdvoe.org/avcat/ctl18883/index.cfm?manufacturer=zeevee&product=zyper4k-sdvoe>
- <https://www.avendor.com/products/zyper4k-video-encoder>
- <https://www.semtech.com/technology/blueriver>
- <https://www.semtech.com/uploads/common/BlueRiver-API.pdf>
- <https://sdvoe.org/blog/the-four-commandments-of-an-sdvoe-network-switch/>
- <https://support.justaddpower.com/kb/article/279-single-vlan-multicast-switching-omega-ultra-and-maxcolor-series/>
- <https://support.justaddpower.com/kb/article/338-single-vlan-multicast-switching-with-2g-series/>
- <https://avlgear.com/collections/just-add-power>
- <https://github.com/bertbert72/homeassistant-justaddpower/blob/0a4d34bb3290824f2d19fceee7fcfd2423674c46/README.md>
- <https://video.matrox.com/en/products/infrastructure/convertip-series>
- <https://video.matrox.com/en/products/infrastructure/convertip-series/convertip-srh>
- <https://screenmoove.com/collections/matrox-convertip>
- <https://www.intopix.com/blogs/post/Do-you-know-IPMX>
- <https://www.avnetwork.com/news/deep-dive-do-you-know-the-difference-between-ipmx-and-smpte-st-2110-and-aes67>
- <https://audioxpress.com/article/internet-protocol-media-experience-ipmx-an-audio-perspective>
- <https://ipmx.io/>
- <https://creationnetworks.net/products/visionary-solutions-packetav-duet-5-encoder-poe-single-port-aes67-dante>
- <https://visionary-av.com/project/duet-5-encoder-decoder/>
- <https://docs.crestron.com/en-us/9496/Content/Topics/Overview/Features-360.htm>
- <https://www.industrialautomationco.com/products/crestron-dm-nvx-360>
- <https://www.4tecdirect.com/dm-nvx-360.aspx>
- <https://www.wyrestorm.com/product/nhd-500-tx-rx-v2/>
- <https://www.wyrestorm.com/blog/what-is-hdbaset-guide-benefits-uses/>
- <https://bzbgear.com/blog/av-over-ip-vs-hdbaset-which-technology-is-right-for-your-next-installation/>
- <https://www.avaccess.com/blogs/guides/what-is-hdmi-over-ethernet/>


## jap-class-deepdive

The controller-less requirement cuts the field sharply. Best fit is Just Add Power (Omega/Ultra/MaxColor, 1GbE, PoE): every encoder streams continuously on a multicast "channel" (channel 0100 = 239.92.1.0), and a decoder switches sources purely by IGMP leave/join, triggered by a Telnet CLI (port 23), an on-device HTTP API ("justAPI", POST /cgi-bin/api/command/channel), or the device web page — no controller exists in the architecture at all, and the API is publicly documented in their KB. Second is Visionary Solutions PacketAV (the "PackedPixels" name in the brief is almost certainly PacketAV E/D4100 and E/D4200 Duet): pure IGMP multicast, decoder retunes by setting STREAM.HOST to a different encoder's address via per-device web UI/API, Vision Lite control software is optional and free — but the full device API is only released to "qualified system integrators" on request. Blustream Multicast is IGMP-based and streams run controller-less, but its documented third-party API terminates in the ACM200 module, so treat it as a semi-fit. WyreStorm NetworkHD (NHD-CTL), AVPro Edge MXNet (CBOX), and ZeeVee ZyPer (ZMP required even point-to-point) all funnel switching through a mandatory controller and are poor fits; budget OREI/J-Tech/generic units are multicast with per-device web GUIs but have weak or undocumented APIs and network-flooding risk. Note all 1GbE options compress (JPEG2000/proprietary "visually lossless") — none are bit-transparent HDMI — and HDCP is terminated in endpoint silicon on all of them, which matches the "software never touches payload" requirement.


### Findings

- **[high]** — *Just Add Power Omega / Ultra / MaxColor (e.g. UL-508POE, MC-TX2/MC-RX2)* (Ultra ~$400-700 street (used 508POE ~$350); MaxColor MC-RX2 ~$1,410 list (dealer pricing gated)) Just Add Power decoders switch sources by IGMP multicast leave/join: each encoder broadcasts continuously on a channel (0-9999) mapped to a multicast IP (channel 0100 = 239.92.1.0 video; 239.93/239.97 audio), and the decoder simply joins the new group. Switch is triggered by Telnet CLI on port 23 ('channel 0100'), on-device HTTP API 'justAPI' on port 80 (POST /cgi-bin/api/command/channel), or per-device web UI. No central controller exists in the architecture — exactly the requested controller-less model — and the KB at support.justaddpower.com is public.
  - Relevance: Strongest architectural match: an ephemeral CLI can telnet/POST to a decoder to re-pair it, state lives on the devices, and a snooper can passively IGMP-join the same 239.92.x.x group to verify.
- **[high]** — *Just Add Power MaxColor MC-TX2/MC-RX2* (~$1,000-1,400/endpoint list) Just Add Power endpoints are PoE-powered (802.3af from the switch), HDCP 2.2/2.3 compliant (handled in endpoint hardware), and Ultra/MaxColor models include USB 2.0 KVM-over-IP routed alongside video; MaxColor does 4K60 4:4:4 + Dolby Vision over 1GbE (proprietary compression) with 10G SFP option. Requires a managed switch with IGMP snooping + querier + jumbo frames (UniFi qualifies). The legacy 2G series used VLAN-reconfiguration switching — buy Omega/Ultra/MaxColor generation for single-VLAN multicast-mode switching.
  - Relevance: Checks every yoke box: PoE, HDCP in hardware, USB extension, single-VLAN IGMP switching on UniFi gear.
- **[high]** — *Visionary PacketAV E4200/D4200* (~$1,200-1,500 list new; used seen at $200-350 on eBay) Visionary Solutions' 1GbE line is branded PacketAV (E4100/D4100, E4200/D4200 'Duet') — 'PackedPixels' appears to be a misremembered name; no such Visionary product exists. Decoders switch by changing STREAM.HOST to the target encoder's IP (encoders default to multicast groups in 225.168.0.0-225.169.255.255 derived from serial number); transport is standard IGMP multicast on 1GbE with jumbo frames, PoE 15.4W, USB-over-IP KVM (E4200 adds USB-C), HDCP with force-on/off options, adaptive bitrate up to ~850 Mbps visually-lossless 4K.
  - Relevance: Genuinely controller-less: Vision Lite matrix software is optional/free convenience only; per-device web UI (admin/admin) holds all state.
- **[high]** — *Visionary PacketAV E4200/D4200* (n/a) Visionary's device API 'providing access to the full range of features' is NOT publicly documented — the manual says it is 'offered to qualified System Integrators, contact support@visionary-av.com'. Telnet-to-RS232 guest mode on TCP 6752 and CEC/GPIO/IR API examples are documented, but programmatic STREAM.HOST switching specifics require obtaining the API doc from Visionary.
  - Relevance: The API gate is the main risk for scripted re-pairing with this brand; JAP's fully public KB is the safer bet for a CLI-driven design.
- **[high]** — *Blustream IP200UHD-TX/RX + ACM200* (~$500-700/endpoint; ACM200 ~$400-500) Blustream Multicast (IP200UHD-TX/RX: 4K HDCP 2.2, PoE, USB/KVM, ~1-frame latency, visually-lossless over 1GbE) runs pure IGMP multicast with fast-leave — streams and front-panel channel up/down source selection work with no controller. However, the documented third-party API (Telnet port 23, RS-232) is hosted on the ACM200 Advanced Control Module which brokers commands to endpoints; device-direct scripting without ACM200 is undocumented (manual config uses a front-panel 169.254.x.x ID scheme).
  - Relevance: Semi-fit: ACM200 is a control bridge, not a stream controller — video keeps flowing if it dies — but the ephemeral-CLI model would depend on it as the API endpoint.
- **[high]** — *WyreStorm NHD-500-TX/RX + NHD-000-CTL* (~$650-825/endpoint; CTL ~$500-800) WyreStorm NetworkHD (100/200/400/500/600 series) decoders use multicast internally, but all third-party control — including matrix switching — is exclusively interfaced through the NHD-000-CTL / NHD-CTL-PRO controller, which hosts the NetworkHD API (Telnet/RS-232 bridge). No documented per-device switching API. NHD-500: 4K60 4:4:4 JPEG2000, 1GbE, HDCP 2.2, PoE+, USB 2.0, Dante option.
  - Relevance: Fails the no-permanent-controller requirement: the CTL must be present for API switching.
- **[high]** — *AVPro Edge MXNet 1G + AC-MXNET-CBOX* (~$700-1,000/endpoint; CBOX ~$800-1,000) AVPro Edge MXNet requires the AC-MXNET-CBOX control box: it 'houses the system API' used by MXnet Mentor and third-party control systems and oversees AV switching, USB/KVM routing, and system management. Endpoints are PoE 1GbE, but there is no documented path to switch a decoder without the CBOX on the network.
  - Relevance: Fails the controller-less requirement outright.
- **[high]** — *ZeeVee ZyPerUHD + ZMP* (~$955 decoder, ~$998-1,383 encoder, plus ZMP license) ZeeVee ZyPer (ZyPer4K 10G, ZyPerUHD 1GbE) requires the ZyPer Management Platform (ZMP) for ALL installations 'including point-to-point' per ZeeVee's own manuals; the open API lives on the ZMP, not the endpoints, and ZMP is licensed by endpoint count. ZyPerUHD is 1GbE, PoE, USB/KVM, HDCP 2.2.
  - Relevance: Fails the controller-less requirement; adds per-endpoint licensing cost on top.
- **[medium]** — *OREI ToVi X4UH-KVM-ETX/ERX* (~$350-500/endpoint) OREI's real AV-over-IP line is 'ToVi' (X1 1080p, X4 4K): unicast/multicast with matrix switching, and claimed control via on-device web interface, Telnet, SSH, and REST API, with an optional X4UH-CB network hub for advanced features (video wall, KVM matrix). API documentation is thin/marketing-level — per-device completeness unverified; OREI's older 'HDMI over IP extender' SKUs are rebadged generic multicast units with no API.
  - Relevance: Possible budget middle ground claiming per-device REST/Telnet, but needs hands-on validation before trusting it for production.
- **[high]** — *J-Tech JTECH-EX-120M class / LKV373A class* ($50-250/unit) J-Tech Digital and generic 'HDMI over IP extender' units (HDbitT/LKV373-class 1080p; 4K H.264/H.265 versions) are multicast: each TX/RX has a web GUI to set IP and multicast group ID, and some H.264 models' streams play directly in VLC (useful for the debug snooper). But: no documented control API (switching = changing group ID via web GUI or IR remote), HDCP handling often out-of-spec (units frequently strip HDCP — a compliance hazard), PoE rare, latency/reliability hobbyist-grade, and units flood traffic when IGMP snooping/querier is absent.
  - Relevance: Fails 'production-grade' and clean-HDCP requirements; only useful as cheap lab mules for protocol experiments.
- **[high]** — *all surveyed 1GbE systems* (n/a) Hard limits common to the lane: no 1GbE product is bit-transparent HDMI — 4K60 4:4:4 is 12-18 Gbps raw, so every 1GbE option (JAP MaxColor, Visionary, Blustream, NHD-500) compresses to 'visually lossless' at ~200-850 Mbps; true uncompressed needs 10GbE SDVoE-class gear, which today mostly ships with mandatory controllers. Also universal: HDCP-protected content only decodes inside each vendor's licensed endpoint pair — a passive snooper can IGMP-join and observe packet flow/bitrate but can never render protected content, which is sufficient for verification-by-traffic-observation.
  - Relevance: Sets correct expectations for yoke: 1GbE = compressed transport; snooper verifies flow, not pixels; HDCP stays entirely in endpoint silicon as required.
- **[high]** — *Just Add Power Ultra/MaxColor* (~$800-2,800 per producer+consumer pair depending on series) Recommendation: Just Add Power Omega/Ultra/MaxColor is the only surveyed ecosystem whose documented switching model IS the target architecture — stateless per-device Telnet/HTTP, IGMP-driven decoder switching on a single VLAN, PoE from UniFi switches (enable IGMP snooping, querier, jumbo frames), USB 2.0 KVM in-band, no vendor daemon anywhere. An ephemeral CLI that POSTs a channel number to a decoder's port 80 fulfills the re-pairing requirement verbatim; Visionary PacketAV is the fallback if its integrator API can be obtained.
  - Relevance: Direct answer to the lane question: which brand tolerates controller-less operation best.

### Sources

- <https://support.justaddpower.com/kb/article/346-multicast-switching-protocol/>
- <https://justaddpower.happyfox.com/kb/article/279-single-vlan-multicast-switching-with-omega-ultra-and-maxcolor-series/>
- <https://support.justaddpower.com/kb/article/330-how-to-telnet-to-a-device-omega-ultra-and-maxcolor-series/>
- <https://avlgear.com/products/just-add-power-mc-rx2-maxcolor-gigabit-poe-receiver>
- <https://www.blackwiredesigns.com/store/just-add-power-maxcolor-usb-2-0-encoder-decoder/>
- <https://www.hdtvsupply.com/justaddpower-vbs-hdip-508poe.html>
- <https://visionary-av.com/wp-content/uploads/2022/10/VIS22-UserManual-ED4200-221013-r.pdf>
- <https://visionary-av.com/project/4200-encoder-decoder/>
- <https://visionary-av.com/wp-content/uploads/2023/04/VISIONARY-ED4100-SpecSheet-230417.pdf>
- <https://www.blustream.co.uk/Attachment/DownloadFile?downloadId=31>
- <https://www.avmat.ch/upload/rama/ACM200%20API%20RevA4%20190423.pdf>
- <https://www.blustream-us.com/acm200>
- <https://www.wyrestorm.com/product/nhd-000-ctl/>
- <https://www.wyrestorm.com/product/nhd-500-tx-rx-v2/>
- <https://www.avalive.com/Wyrestorm/NHD-500-RX-v2/363789/productDetail.php>
- <https://www.bhphotovideo.com/c/product/1812714-REG/avpro_edge_ac_mxnet_cbox_b_mxnet_1g_control_box.html>
- <https://www.manualslib.com/manual/3913789/Avpro-Edge-Ac-Mxnet-Cbox.html>
- <https://www.zeevee.com/av-over-ip/management/>
- <https://www.zeevee.com/wp-content/uploads/2021/05/ZyPerHD-User-Manual.pdf>
- <https://www.avalive.com/ZeeVee/ZYPERUHD-D/262160/productDetail.php>
- <https://www.orei.com/pages/tovi-by-orei-av-over-ip>
- <https://www.orei.com/products/tovi-x4-series-4k-w-kvm-encoder-x4uh-kvm-etx>
- <https://resource.jtechdigital.com/products/791>
- <https://jtechdigital.com/product/400ft-n2n-receiver/>


## usb-over-network

The USB-over-network market splits into two architectures, and neither fully matches yoke's ideal. (a) Transparent hardware extenders (Icron ExtremeUSB family and its ATEN license) carry all USB traffic types including isochronous webcams over ordinary switched gigabit Ethernet with zero host software — but pairing is strictly 1 LEX to 1 REX, established by physically pressing Mode buttons on both units (same L2 subnet required); there is no network API, multicast mechanism, or any way to re-pair remotely, so dynamic many-to-many switching is impossible without a human touching hardware. (b) USB/IP device-server products (Silex DS-700, Digi AnywhereUSB Plus, VirtualHere, Linux usbip) give exactly the dynamic re-pairing yoke wants — any host claims/releases a device over unicast TCP via a scriptable CLI/API with the device-side box itself arbitrating exclusivity (no central controller) — but all require vendor software running continuously on the host side, and isochronous support ranges from fragile to absent. VirtualHere's Device Client (Pi 4/5 in USB gadget mode) is the only way to get a software-free physical host-side endpoint in the device-server model, and it explicitly cannot do webcams or USB3. Candidly: no COTS product today offers transparent, hardware-terminated USB with network-signal-triggered any-to-any switching; also, bit-transparent USB 3.x (5 Gbps) over 1GbE switched Ethernet does not exist — Icron's Raven 3104 does USB3 only point-to-point over dedicated Cat6a/7. The pragmatic yoke path is Icron 2304PoE/GE-LAN if button re-pairing is tolerable, or building endpoints as PoE-powered Linux boards running VirtualHere (API-switchable, $49/server) accepting its isochronous/gadget limits.


### Findings

- **[high]** — *Icron USB 2.0 Ranger 2304GE-LAN* ($460-480) Icron USB 2.0 Ranger 2304GE-LAN extends 4 USB 2.0 ports across an ordinary gigabit switched LAN (Layer 2, LEX and REX must be on the same subnet), with no host software and spec-sheet support for 'All Traffic Types' and 'All Device Types' — i.e., isochronous webcams/audio, HID, and mass storage up to 480 Mbps, up to 30 devices via hubs
  - Relevance: Closest existing product to yoke's transparent-transport requirement for USB: endpoint hardware handles the payload, our software never touches it, and it traverses UniFi-style gigabit switches
- **[high]** — *Icron USB 2.0 Ranger 2304GE-LAN (manual, doc 90-01383-A02)* Re-pairing a 2304GE-LAN LEX to a different REX is possible in the field but ONLY by physically pressing the Mode button on both units (activate pairing mode within a 10-minute window; hold >10 s to unpair). There is no web UI, no network API, no multicast join mechanism — dynamic software-triggered many-to-many switching is not possible
  - Relevance: Directly fails yoke's 'any producer re-paired to any consumer dynamically via network signals' requirement; Icron-class hardware would need a human at both boxes for every switch
- **[medium]** — *Icron USB 2.0 Ranger 2304PoE* ($565) Icron USB 2.0 Ranger 2304PoE is the same class of LAN extender but the units are 802.3at PoE+ powered devices, supplying up to 1.5 A per USB port (3.5 A total) at the remote end, and supports isochronous devices (webcams) over a PoE+ gigabit LAN
  - Relevance: Matches yoke's PoE-powered endpoint model on UniFi PoE switches exactly; same button-pairing limitation presumed (same ExtremeUSB LAN architecture)
- **[high]** — *Icron Raven 3104* ($1,100-1,400) Icron Raven 3104 (USB 3-2-1) provides USB 3.1 Gen 1 (5 Gbps) extension but ONLY point-to-point over dedicated Cat6a/7 up to 100 m — it does not operate through switched Ethernet at all (its RJ45 'LAN' port is just a pass-through convenience). No vendor ships bit-transparent USB 3.x over a 1GbE switched network because 5 Gbps cannot fit in 1 Gbps
  - Relevance: Sets the honest ceiling: yoke's switched-Ethernet USB lane is USB 2.0 (480 Mbps) only unless moving to 10G with future products; USB3 devices will enumerate at high-speed through 2304-class extenders
- **[high]** — *ATEN UEH4102* ($535) ATEN UEH4102 is a licensed ExtremeUSB implementation (4-port USB 2.0 over gigabit LAN, claims support for all peripherals including webcams and audio); Local/Remote units come pre-paired by MAC address with the same fixed 1:1 pairing model — no many-to-many or API-driven switching
  - Relevance: Confirms the transparent-extender category is uniformly 1:1 statically paired regardless of vendor; buying ATEN instead of Icron does not buy switchability
- **[high]** — *Silex DS-700* ($130-200) Silex DS-700 (USB 3.0 device server) lets any PC on the network claim an attached USB device exclusively via the free SX Virtual Link software, including a polite 'request release' handoff between users — dynamic re-pairing is inherent and client-triggered over ordinary unicast TCP, with no central controller. But every host must run SX Virtual Link continuously, and the host side must be a general-purpose computer, not a hardware endpoint
  - Relevance: Cheapest COTS demonstration of the claim/release switching semantics yoke wants, but violates the 'no vendor software on host' spirit unless yoke's consumer endpoint is itself a Linux box running the client
- **[high]** — *Digi AnywhereUSB 2 Plus / 8 Plus / 24 Plus* ($500 (2-port) / $1,200 (8-port) / $3,130 (24-port)) Digi AnywhereUSB Plus (2/8/24-port USB 3.1 Gen 1 hubs, TLS-secured) implements true multi-host arbitration: USB ports are grouped in the hub's local web UI/CLI, each group is claimed by exactly one host running the AnywhereUSB Manager daemon (Windows/Linux), and switching is done by release/claim from the scriptable awusbmanager CLI — the hub itself arbitrates, no always-on central controller needed. Isochronous streaming devices are supported via an enabled-by-default 'high performance isochronous' mode, though through a driver stack, not transparently. No PoE; AC or DC powered
  - Relevance: The most production-grade many-to-many USB-over-IP system (widely used for license dongles in VM farms); its port-group/claim model is a good semantic template for yoke's pairing protocol, but hosts must run Digi's daemon forever
- **[high]** — *Linux usbip* (free) Linux usbip (mainline kernel, usbipd + 'usbip bind/attach/detach') provides free client-driven re-pairing of one exported device to any host over unicast TCP port 3240 and runs fine on PoE-powered Pis/OpenWrt boxes, but has well-documented production defects: client-side hangs and unkillable processes on network disturbance, occasional kernel panics after prolonged use, stuttering isochronous (webcam) transfers under congestion, and no built-in authentication or encryption
  - Relevance: Fails yoke's 'works in the wild' bar as-is; usable only with heavy watchdog/hardening, which argues for VirtualHere or hardware extenders instead
- **[high]** — *VirtualHere USB Server + Client* ($49 per server (one-time); client free) VirtualHere (commercial usbip-class software, $49 one-time per server) runs the server on PoE-powered Raspberry Pis/routers/NAS, is controllable entirely from a command-line/named-pipe client API (USE/STOP USING, 'Auto-Use' auto-claim with 3-second grace), supporting scripted, network-triggered device switching between hosts with no central controller
  - Relevance: Best software candidate for yoke's USB lane if endpoints are small Linux boards: the ephemeral-CLI-triggers-reconfiguration pattern yoke wants already exists here
- **[high]** — *VirtualHere Device Client on Raspberry Pi 4/5* ($49 license + ~$80 Pi + PoE HAT) VirtualHere's Device Client mode turns a Raspberry Pi 4/5 (USB-C OTG gadget mode) into a hardware host-side endpoint: the remote network USB device is re-presented out the Pi's USB-C port to a completely unmodified host machine. Hard limits: high-bandwidth isochronous devices (webcams) do NOT work, no USB 3 SuperSpeed, no low-speed USB 1.0, max 7 endpoints, and only one device at a time
  - Relevance: The only COTS way to combine device-server-style dynamic switching with a software-free physical host port — but its isochronous and USB3 exclusions mean webcams still require the Icron-class transparent extenders
- **[high]** No product on the market combines transparent hardware USB termination (no host software, isochronous-capable) with network-signal-triggered any-to-any switching; the two properties are mutually exclusive in today's COTS offerings
  - Relevance: Core design conclusion for yoke: either accept manual button pairing on Icron-class hardware, accept a claim/release software agent at the consumer endpoint (VirtualHere/Digi model), or design a yoke endpoint that is itself a PoE Linux board bridging one of these — the pairing-switch protocol yoke wants must be built, not bought

### Sources

- <https://www.gdsys.com/manuals/KVM_extenders/USB-Extender/usb-2-0-ranger-2304ge-lan-manual.pdf>
- <https://www.kvm-switches-online.com/00-00376.html>
- <https://www.virtualhere.com/device_client>
- <https://www.virtualhere.com/client_api>
- <https://www.virtualhere.com/node/2852>
- <https://www.virtualhere.com/usb_server_software>
- <https://www.aten.com/global/en/products/usb-solutions/extenders/ueh4102/>
- <https://www.bhphotovideo.com/c/product/1477582-REG/aten_ueh4102_4_port_usb_2_0_cat.html>
- <https://us.rs-online.com/product/l-com/icr2304poe/75557926/>
- <https://www.icronshop.com/usb-2-0-ranger-2304poe>
- <https://www.markertek.com/product/icr-3104/icron-raven-3104-usb-3-2-1-4-port-usb-3-1-over-cat6-7-extender-system>
- <https://www.icron.com/assets/usb-3-2-1-raven-3104-manual.pdf>
- <https://www.silextechnology.com/connectivity-solutions/usb-network-connectivity/ds-700>
- <https://docs.digi.com/resources/documentation/digidocs/90002383/device/anywhereusbplus/content/tasks/isoch-high-performance.htm>
- <https://docs.digi.com/resources/documentation/digidocs/90002383/device/anywhereusbplus/content/tasks/t_install_driver.htm>
- <https://www.amazon.com/Digi-ANYWHEREUSB-Plus-USB-CONNECTORS/dp/B07T8JBN23>
- <https://github.com/cezanne/usbip-win/issues/74>
- <https://forums.gentoo.org/viewtopic-t-1114344-start-0.html>
- <https://www.virtualhere.com/node/417>


## network-igmp

Multicast AVoIP on COTS gigabit switches is a solved, well-documented pattern, and decoder-driven IGMP leave/join is exactly how shipping products (Just Add Power, Visionary, Crestron NVX) do controller-less stream switching — the decoder leaves the old group and joins the new one, and the switch fabric does the rest, which fits yoke's no-central-controller requirement perfectly. The non-negotiable switch configuration is: dedicated AV VLAN, IGMP snooping on that VLAN, exactly one pinned IGMP querier (without it snooping state ages out in ~4 minutes and every stream floods every port), fast-leave (a 1G decoder port cannot hold old+new ~850 Mbps streams during a switch), sane unknown-multicast handling, and jumbo frames end-to-end for JPEG2000-class 850 Mbps endpoints. UniFi supports all of this (Pro AV guidance, per-VLAN snooping/querier/fast-leave, jumbo frames, fw 7.2+ for unknown-multicast controls) but has a real history of IGMP snooping bugs on Gen 2 switches where membership reports are ignored and multicast floods anyway — which is precisely why yoke's passive snooper/validator is valuable, and why Netgear's M4250 AV Line (preconfigured IGMP Plus) exists as the safe fallback. Capacity-wise, UniFi fabrics are non-blocking, so a 24-port gigabit switch happily replicates 20+ simultaneous ~850 Mbps streams internally; the hard limits are one stream per 1G access port and one distinct stream per 1G inter-switch uplink (use the 10G SFP+ uplinks, ~11 streams each, or keep pairs on one switch). PoE is easy: endpoints are ~13 W 802.3af devices, so a 400 W-budget Pro-tier UniFi switch (~$799) powers a full complement, while 95 W budget switches cap out around 6-12 endpoints.


### Findings

- **[high]** IGMP join/leave IS the industry-standard decoder-side switching mechanism with no central controller: a decoder tunes to a source by sending an IGMP Leave for the old multicast group and a Membership Report (Join) for the new one; the switch's snooping table does the actual 'routing'. Just Add Power's Multicast Switching Protocol documents this exactly, including encoding the channel number in the multicast address (e.g., channel 100 = 239.92.1.0, last two octets = channel). This validates yoke's design: an ephemeral CLI only needs to tell the decoder which group to join, and a debug snooper can passively observe by joining the same group or watching IGMP reports.
  - Relevance: Confirms the core no-always-on-controller architecture is proven practice in shipping commercial AVoIP products, and suggests a deterministic group-addressing scheme yoke should copy.
- **[high]** — *UniFi Network application + UniFi switches (fw 7.2+)* An IGMP querier is mandatory on a pure-L2 AV VLAN even though nothing is being routed: without periodic General Queries, snooping memberships age out (~2x125s) and the switch reverts to flooding every stream to every port. Crestron requires a querier on at least one switch (default 125s interval is fine); Ubiquiti's Pro AV guidance says to manually pin a specific switch as querier rather than rely on election (lowest IP wins if several are enabled), because Dante and other AV systems need deterministic querier placement. UniFi exposes per-network IGMP Snooping + querier settings; 'Forward Unknown Multicast Traffic' (drop, or forward to multicast-router ports) and 'Flood Known Protocols' (PTP/SSDP/NTP/SLP/H.323) require switch firmware 7.2+.
  - Relevance: Yoke deployments will silently work for ~4 minutes then flood-crash the LAN if no querier is configured; the yoke CLI or docs must verify querier presence, and a snooper can detect its absence.
- **[high]** Fast Leave (immediate leave) is effectively required on 1GbE AVoIP: at ~850 Mbps per stream a decoder port cannot carry the old and new streams simultaneously during a switch (2 x 850M > 1G), so without fast-leave the port is oversubscribed until the group times out, causing dropped frames/black screen. Crestron lists immediate leave as a mandatory switch feature; Just Add Power's UniFi guide says to enable Fast Leave on the AV VLAN; Visionary documents fast-leave as what makes source switching fast. UniFi switches expose Fast Leave. Caveat: fast-leave assumes one receiver per port, which holds for AVoIP decoders directly attached to switch ports.
  - Relevance: Directly determines yoke's re-pairing latency and whether switching glitches; yoke endpoints should be one-per-switch-port and docs must mandate fast-leave.
- **[high]** — *UniFi USW Gen2 switches* UniFi can be made to work but has a documented history of IGMP snooping defects: community threads report Gen 2 UniFi switches ignoring IGMP membership reports and flooding multicast to all ports in the VLAN (IPTV use case), snooping 'not working' with all VLAN ports showing identical traffic, and behavior differing between Gen 1 and Gen 2 hardware; features are gated on newer firmware/Network-app versions. Just Add Power maintains a dedicated 'Configuring Multicast on Ubiquiti Switches' procedure and warns 'AV traffic is VERY GOOD at crashing an unconfigured network' — configure before plugging in endpoints. UniFi is usable for production AVoIP only with the Pro AV settings applied and validated per firmware release.
  - Relevance: Yoke targets UniFi homelabs; a passive multicast snooper that verifies traffic is actually pruned (not flooded) is exactly the right mitigation for this known failure mode.
- **[high]** Use a dedicated AV VLAN: every commercial vendor (Crestron, Just Add Power, Ubiquiti Pro AV guidance) recommends isolating AVoIP multicast on its own VLAN with IGMP snooping enabled on that VLAN, blocking it from uplinks/ports that don't need it, and optionally 802.1p priority 5-7 for AV vs 0-4 for the rest. Mixing ~850 Mbps multicast into the general LAN VLAN risks flooding Wi-Fi APs and unmanaged segments whenever snooping state is lost.
  - Relevance: Yoke's deployment guide should make a dedicated VLAN the default topology; the snooper needs a port/tap on that VLAN.
- **[high]** — *USW-24-PoE (~$379), USW-Pro-Max-24-PoE (~$799)* (379-799 per switch) Stream capacity math on gigabit UniFi: the switch fabrics are non-blocking (USW-24-PoE: 52 Gbps capacity; USW-Pro-Max-24-PoE: 112 Gbps), so ~20+ simultaneous 850 Mbps multicast streams inside ONE switch are fine — hardware replicates one ingress copy to N egress ports. The real limits are (a) one stream per 1G decoder port, (b) inter-switch uplinks: each DISTINCT multicast group crossing an uplink consumes its full bandwidth, so a 1G uplink carries only one ~850 Mbps stream; Crestron's rule is to allocate 1 Gbps per endpoint across uplinks (e.g., 48 endpoints need 40G+ of uplink). Practical guidance: keep paired producers/consumers on one switch, or use the 10G SFP+ uplinks (~11 streams each) present on USW-Pro models.
  - Relevance: Sets yoke's scaling envelope on 1GbE and justifies the 'possibly 10G later' path for cross-switch pairing.
- **[high]** Jumbo frames are endpoint-dependent, not universal: JPEG2000-class ~500-850 Mbps endpoints (Just Add Power 3G, Visionary Duet/PacketAV) REQUIRE jumbo frames (MTU >= ~8000) on all AV ports and inter-switch trunks — Visionary's whitepaper shows jumbo frames are needed to keep sub-333us latency across multi-hop 1GbE — while lower-bitrate H.264/H.265 endpoints and NDI generally run on standard 1500 MTU. UniFi switches support jumbo frames but it must be explicitly enabled. Mismatched MTU on one trunk silently breaks video.
  - Relevance: Yoke's network-validation tooling should check end-to-end MTU on the AV VLAN, since this is a classic silent-failure misconfiguration.
- **[high]** — *USW-Pro-Max-24-PoE (400W PoE budget)* (~799) PoE is a non-issue with the right switch tier: AVoIP endpoints are typically 802.3af class (Just Add Power MaxColor TX: 802.3af, <= 12.95-15.4 W), so 24 endpoints need at most ~370 W of budget. A USW-Pro-24-PoE or USW-Pro-Max-24-PoE (400 W budget; PoE+/PoE++ ports) powers a full switch of endpoints, whereas a USW-24-PoE (95 W, 16 PoE+ ports) supports only ~6-12 endpoints depending on allocation vs actual draw. Budget switches (USW-Lite class, ~45 W) are inadequate for dense deployments.
  - Relevance: Determines which UniFi SKUs yoke can list as supported for powering producer/consumer boxes without wall warts.
- **[medium]** — *Netgear M4250 series (alternative to UniFi)* (700-1,700) What breaks in practice, ranked: (1) no querier -> flood after snooping timeout; (2) snooping enabled but hardware/firmware ignores reports (UniFi Gen 2 bug reports) -> flood; (3) unknown-multicast default behavior: pre-7.2 firmware dropped unknown multicast and flooded 'known protocols' with no override — dropping can black-hole a stream between encoder start and first join, flooding can melt Wi-Fi; (4) missing fast-leave -> multi-second switching glitches; (5) 1G uplinks accidentally carrying multiple groups; (6) mDNS/SSDP control-plane chatter being flooded independently of AV groups. Purpose-built AV switches (Netgear M4250 'AV Line' with IGMP Plus profiles preconfigured out of the box, ~$700-1,700 depending on model) exist specifically because generic switches get this wrong; M4250 is the fallback recommendation if UniFi misbehaves.
  - Relevance: This is the concrete checklist yoke's snooper/validator should test for, and gives users an escape hatch when UniFi firmware regressions hit.
- **[medium]** IGMPv3/SSM is available but optional: UniFi offers 'Multicast Enhancement (IGMPv3)' allowing receivers to specify source as well as group, which prevents group-address collisions between two producers. Commercial 1GbE AVoIP products mostly run IGMPv2 any-source multicast with vendor-unique group addressing (e.g., 239.x.0.0/16 with channel-derived addresses). Yoke can start with IGMPv2 + a disciplined 239/8 allocation scheme and treat SSM as an enhancement.
  - Relevance: Informs yoke's multicast addressing and IGMP version choice for maximum switch compatibility.

### Sources

- <https://help.ui.com/hc/en-us/articles/18125733726615-Pro-AV-Traffic-Optimization-on-UniFi-Switches>
- <https://justaddpower.happyfox.com/kb/article/357-configuring-multicast-on-ubiquiti-switches/>
- <https://justaddpower.happyfox.com/kb/article/346-multicast-switching-protocol/>
- <https://docs.crestron.com/en-us/9496/Content/Topics/AV-over-IP_Network-Design.htm>
- <https://techspecs.ui.com/unifi/switching/usw-pro-max-24-poe>
- <https://techspecs.ui.com/unifi/switching/usw-24-poe>
- <https://deluisio.com/networking/unifi/2024/08/19/enhancing-multicast-performance-a-guide-to-igmpv3-on-unifi/>
- <https://community.ui.com/questions/Issue-With-IGMP-on-Unifi-Gen-2-Switches/d7cefa3a-c6e7-4637-a272-097aaef6ab03>
- <https://community.ui.com/questions/Unifi-Switches-flooding-specific-multicast-IPTV-Related/976db841-ceae-4b39-82f4-68b3aa280a4f>
- <https://visionary-av.com/wp-content/uploads/2024/10/VIS24-HOW-Jumbo-Frames-v1.1.pdf>
- <https://justaddpower.happyfox.com/kb/article/510-maxcolor-series-power-over-ethernet-poe-specifications/>
- <https://www.netgear.com/assets/campaign/121401/images/m4250-datasheet.pdf>
- <https://www.wyrestorm.com/blog/av-over-ip-igmp-snooping-configuration/>


## hdcp-transparency

HDCP-protected content can legitimately traverse an IP network: DCP LLC publishes the HDCP 2.x "Interface Independent Adaptation" (IIA) spec exactly for this (AES-128-CTR content encryption over any transport including TCP/IP), and licensed AV-over-IP endpoints (SDVoE, HDBaseT, and licensed 1GbE codec boxes) act as HDCP repeaters that terminate HDCP at the encoder's HDMI input and re-authenticate at the decoder's HDMI output — the Ethernet hop is protected by HDCP IIA or an equivalent approved robustness mechanism inside the licensed product pair. Control software that only tells licensed endpoints which decoder subscribes to which encoder is not a "Licensed Product" under the HDCP License Agreement (that term covers products that implement HDCP encryption/decryption/key exchange/authentication), so yoke's control plane needs no DCP license and HDCP remains entirely the endpoints' concern — provided yoke never handles keys/content and never configures endpoints to defeat protection. The practical constraints that do leak into system design: HDCP 2.x IIA enforces a locality check (RTT <= 7 ms, up to 1024 retries — fine on a LAN, fatal over WAN), and repeater topology limits (max 31 downstream devices, depth 4; unlimited only with HDCP Pro endpoints holding an SRM under 3 months old). Cheap unlicensed 1GbE encoder boxes either fail on HDCP sources (black screen, because the source sees no HDCP sink) or silently decrypt-and-strip, which is DMCA 1201 circumvention territory (Warner Bros. and DCP sued LegendSky/HDFury over exactly this; settled out of court) — do not build the product story on them. Finally, nothing on 1GbE is bit-transparent: true bit-transparency requires ~3-18 Gbps, so 1GbE products always re-encode (H.264/H.265/JPEG 2000, 16-300 ms latency); SDVoE on 10GbE is uncompressed or lightly compressed ("zero-frame" latency) and is the closest commercially available thing to transparent HDMI over Ethernet.


### Findings

- **[high]** — *HDCP 2.3 IIA specification (digital-cp.com)* HDCP over IP is explicitly provided for by DCP LLC: the HDCP 2.3 Interface Independent Adaptation (IIA) specification defines HDCP over any interface/transport (TCP/IP, USB, Wi-Fi), using RSA-based AKE with pairing, a locality check, and AES-128-CTR content encryption. It is the mechanism licensed AV-over-IP products use on the network hop.
  - Relevance: Confirms the whole yoke concept is compatible with HDCP when licensed endpoints are used; the Ethernet segment is just another HDCP-protected interface from DCP's perspective.
- **[high]** — *HDCP 2.3 IIA spec section 2.3* HDCP 2.x IIA enforces a locality check: the transmitter's watchdog requires the LC_Init -> LC_Send_L_prime round trip to complete within 7 ms (up to 1024 retry trials allowed). Verified directly in the Rev 2.3 IIA spec text.
  - Relevance: Hard design constraint: HDCP pairing works on a switched gigabit/10G LAN (RTT well under 7 ms) but cannot legally/technically stretch across high-latency links (WAN/VPN). Yoke should document LAN-only scope for protected content.
- **[high]** — *HDCP 2.x repeater rules* Every licensed HDMI-over-IP encoder+decoder pair presents itself to the source as an HDCP repeater. HDCP 2.x repeater topology limits apply: DEVICE_COUNT max 31 downstream devices and DEPTH max 4 levels; exceeding them raises MAX_DEVS_EXCEEDED / MAX_CASCADE_EXCEEDED and the source stops sending protected content. Ethernet switches do not count against the limit; only HDCP-capable devices do.
  - Relevance: For yoke's 1:1 dynamic pairing this is a non-issue (depth ~2-3, few devices), but large multicast fan-out of one protected source to many displays can hit the 31-device cap. Re-pairing a decoder triggers a fresh downstream HDCP authentication, causing a brief re-auth/blank on switch.
- **[medium]** — *HDCP Professional IIA Rev 2.2* HDCP Professional (HDCP Pro, a separate IIA-based spec) removes the 31-device/4-level limits for pro-AV distribution, but only while the repeater holds a System Renewability Message (SRM) less than 3 months old; with a stale SRM it degrades to consumer HDCP 2.2 limits. HDCP Pro endpoints are a niche, higher-cost product category.
  - Relevance: Only matters if yoke ever targets large venues (many displays per protected source). It implies an online SRM-refresh dependency that conflicts slightly with a fully controller-less design.
- **[medium]** (0 (vs $15,000/yr annual fee + per-Device-Key fees if a license were needed)) The HDCP License Agreement (March 2024, read directly) defines a 'Licensed Product' as a product that (a) implements the HDCP Specification — i.e., the 'method for encryption, decryption, key exchange, authentication and renewability' — and (b) is Compliant; 'Licensed Component' similarly covers partial implementations embedding HDCP. Pure control-plane software that never implements any HDCP function, never holds Device Keys, and never touches content plaintext or ciphertext does not meet these definitions, so it needs no DCP adopter license. Orchestrating which licensed decoder subscribes to which licensed encoder (IGMP joins, per-device API calls) is equivalent to what Crestron/Q-SYS/SDVoE-API control systems already do without HDCP licenses.
  - Relevance: Core answer for yoke: HDCP is entirely the endpoint vendors' concern as long as yoke stays strictly control-plane. Caveat: this is a well-supported reading of the license text plus industry practice, not an official DCP statement; the one boundary is that yoke must never direct endpoints to disable/downgrade protection (adopters are barred from circumvention aids, and yoke itself would face DMCA 1201 exposure if it did).
- **[high]** (15,000/yr + key fees) Becoming an HDCP adopter (only needed if yoke ever ships hardware/firmware that implements HDCP) costs a $15,000/yr annual fee to DCP LLC plus volume-tiered Device Key Set fees (roughly $0.01/key at 1M-unit volume, more per-key at low volume); there is no per-unit royalty beyond keys.
  - Relevance: Quantifies why the 'endpoints handle HDCP' architecture is right: staying control-plane-only avoids a $15k/yr floor plus compliance/robustness-rule obligations.
- **[high]** — *generic URayTech-class H.264/H.265 encoders and 'HDMI over IP extender' kits* (40-150 per box) Cheap unlicensed 1GbE HDMI-over-IP boxes (typically $40-150 H.264/H.265 encoder/decoder pairs) handle HDCP in one of two bad ways: most simply have no HDCP receiver capability, so an HDCP-enforcing source (Apple TV, PS5, cable box, most streaming apps) detects a non-HDCP sink and blacks out or downgrades; some grey-market units embed leaked/unauthorized keys and re-emit decrypted content over the LAN in the clear, which is HDCP stripping — a DMCA §1201 circumvention violation in the US, independent of whether any copying occurs.
  - Relevance: Yoke cannot promise 'works with any HDMI source' on cheap 1GbE boxes; protected sources will fail or the deployment carries legal risk. Product guidance: support these boxes for unprotected sources (cameras, PCs, signage) and require licensed endpoints for protected content.
- **[high]** — *HDFury Integral (LegendSky) precedent* The legal risk around HDCP stripping/downgrading is concretely litigated: Warner Bros. and DCP LLC sued LegendSky (HDFury) in 2016 over the Integral 4K60 device that let HDCP 2.2 content flow to HDCP 1.x devices, alleging DMCA and Lanham Act violations; LegendSky argued downgrade-not-strip and countersued for defamation; the dispute settled out of court with undisclosed terms. Current HDfury products remain sold with HDCP-compliance framing, but the case shows studios/DCP will act against devices marketed around HDCP removal.
  - Relevance: If yoke's ecosystem, docs, or marketing ever leans on HDCP-bypassing hardware, it inherits this exposure; conversely, purely routing between licensed endpoints has no analogous case against it.
- **[high]** — *SDVoE (e.g., Black Box MCX-S7, ZeeVee ZyPer4K) vs 1GbE codec boxes* (SDVoE endpoints ~1,000-2,500 each; 1GbE licensed AVoIP ~300-800; HDBaseT extender pairs ~200-500) Genuinely bit-transparent HDMI over 1GbE is impossible: uncompressed 1080p60 RGB is ~3-4.5 Gbps and 4K60 4:4:4 is ~12-18 Gbps. Commercial reality: (a) SDVoE on 10GbE (Semtech BlueRiver) carries formats up to ~9 Gbps uncompressed and applies light (~1.4:1, visually lossless) compression only for 4K60 4:4:4, with sub-frame 'zero latency' — the closest thing to transparent HDMI over Ethernet; (b) all 1GbE products re-encode with H.264/H.265/JPEG 2000 (or JPEG-XS on managed networks), with visually lossy results on fine detail and 16-300+ ms latency plus slow source switching; (c) HDBaseT is near bit-transparent at the TMDS level but is point-to-point over Cat6, not switched Ethernet, and handles HDCP as a licensed pass-through/repeater link with its own HDCP-on-HDBaseT spec (7 ms locality check).
  - Relevance: Directly answers the candor requirement: on yoke's likely 1GbE UniFi network, the AV payload is always a re-encode, never the original bitstream — 'transparent transport' means transparent to yoke's software, not bit-exact. A 10G upgrade plus SDVoE endpoints is the path to effectively transparent video.
- **[medium]** A passive multicast debug snooper is compatible with HDCP compliance: on licensed endpoints the network payload is AES-encrypted (HDCP IIA or vendor-equivalent approved robustness), so the snooper can observe flows, joins/leaves, sequence continuity, and bitrates but cannot (and must not attempt to) decrypt AV payload. Snooping the control/transport headers implicates no HDCP or DMCA issue.
  - Relevance: Validates yoke's debug-snooper requirement: design it to verify transport health from headers/RTP-level metadata only, and treat payload opacity on protected streams as expected correct behavior, not a bug.

### Sources

- <https://www.digital-cp.com/sites/default/files/specifications/HDCP%20Interface%20Independent%20Adaptation%20Specification%20Rev2_3.pdf>
- <https://www.digital-cp.com/sites/default/files/licenses/HDCP%20License%20Agreement_March%2014%202024--FOR%20REVIEW%20ONLY.pdf>
- <https://www.digital-cp.com/faqs>
- <https://www.digital-cp.com/sites/default/files/specifications/HDCP%20Professional%20Interface%20Independent%20Adaptation%20Specification%20Rev2%202_1.pdf>
- <https://support.biamp.com/Tesira/Video/HDCP_with_TesiraLUX>
- <https://tesira-help.biamp.com/Network_Considerations/HDCP_Management.htm>
- <https://www.installation-international.com/case-studies/opinion-protecting-the-content-ecosystem>
- <https://www.digital-cp.com/sites/default/files/specifications/HDCP%20on%20HDBaseT%20Specification%20Rev2_2.pdf>
- <https://www.blackbox.com/en-US/store/product/detail/4K60-Network-AV-Encoder-HDCP-2-2-HDMI-2-0-10-GbE-Fiber/MCX-S7-FO-ENC>
- <https://www.zeevee.com/av-over-ip/zyper4k/>
- <https://www.wyrestorm.com/blog/2023/07/18/the-power-trio-of-av-over-ip/>
- <https://www.avnetwork.com/features/10gb-vs-1gb-manufacturers-take-sides>
- <https://www.digitaltrends.com/movies/legendsky-counterclaim-warner-bros-digital-content-protection/>
- <https://techraptor.net/technology/news/lawsuit-over-4k-content-protection-stripper-settled-out-of-court>
- <https://www.theregister.com/2016/01/05/chipzilla_warner_launch_lawsuit_over_hdcp_cryptobusting_boxen/>
- <https://www.engadget.com/2016-01-04-lawsuit-over-4k-copy-protection-stripping.html>
- <https://aplus-labs.com/wordpress/?p=150&lang=en>
- <https://www.symmetryelectronics.com/blog/hdmi-licensing-costs-explained-annual-fees-royalties-licensing-requirements/>
- <https://www.drhdmi.eu/dictionary/hdcp-strippers-legality.html>
- <https://blinksandbuttons.net/will-an-hdmi-splitter-bypass-hdcp/>


## bandwidth-math

Truly bit-transparent HDMI over 1GbE is impossible: even 1080p60 8-bit RGB is ~3.0 Gbps of active pixels (~3.56 Gbps with blanking), and 4K60 RGB is ~11.9 Gbps (~14.3 Gbps with blanking, i.e. HDMI 2.0's 18 Gbps link), so a gigabit link always implies compression of at least ~4:1 (1080p60) to ~13:1 (4K60 4:4:4). The industry answer on 1GbE is the "visually lossless wavelet" tier — JPEG 2000 and its derivatives (Crestron DM NVX 'Pixel Perfect Processing', WyreStorm NHD-500, Just Add Power, KanexPro) and increasingly JPEG XS — running at 300-900 Mbps with sub-frame latency (<16-33 ms, JPEG XS <1 ms); this is the lowest tier that credibly satisfies "endpoints transport transparently from the user's perspective," including desktop/KVM use, and it is fully compatible with IGMP-multicast-driven switching and endpoint-owned HDCP. H.264/H.265 boxes (10-50 Mbps, 80-250 ms latency) are a tier below and feel like streaming, not a cable. Upgrading to 10GbE buys the SDVoE class: genuinely uncompressed up to 4K30 4:4:4 / 4K60 4:2:0 (~6-7 Gbps) at ~30 µs, and ~1.4:1 "light" visually lossless compression (~8.7 Gbps, <120 µs) for 4K60 4:4:4 — effectively pixel-transparent and latency-transparent, at roughly 1.5-2x the per-endpoint cost plus 10G PoE switching. Recommendation for yoke on UniFi gigabit PoE: target the JPEG2000/JPEG-XS 1GbE tier as the baseline transparency contract, and treat SDVoE/10G as the upgrade path rather than a requirement.


### Findings

- **[high]** Raw active-pixel bitrates (8-bit): 1080p60 RGB = 2.99 Gbps (3.56 Gbps with blanking at 148.5 MHz pixel clock); 4K30 RGB = 5.97 Gbps (7.13 with blanking); 4K60 RGB = 11.94 Gbps (14.26 with blanking, carried on HDMI 2.0's 18 Gbps TMDS); 4K60 4:2:0 = 5.97 Gbps; 4K60 10-bit RGB = 14.93 Gbps. Usable payload is ~0.95 Gbps on 1GbE and ~9.5 Gbps on 10GbE, so NOTHING — not even 1080p60 — fits uncompressed on gigabit, and on 10GbE only up to 4K30 4:4:4 / 4K60 4:2:0 fits uncompressed.
  - Relevance: This is the hard physical constraint that defines yoke's entire design space: 'transparent' on 1GbE can only ever mean 'visually lossless compressed', never bit-exact; the endpoint hardware must compress, which is why payload-agnostic software transport is the right architecture.
- **[high]** — *WyreStorm NHD-120-TX and class peers* ($400-800 per endpoint) H.264/H.265 AVoIP tier (e.g., WyreStorm NetworkHD 100/120 series, ~4K30 4:2:0 max): streams at tens of Mbps but latency is ~80 ms (1080p low-latency mode) to ~150-250 ms (4K / high-quality mode). Not visually lossless on fine text; unsuitable for KVM-style interactive use.
  - Relevance: Rules out the cheapest tier for yoke's 'feels like a cable' requirement; acceptable only for passive signage/monitoring consumers. Also the easiest tier for a snooper to decode, but latency disqualifies it as the primary transport.
- **[high]** — *Crestron DM-NVX-350/360; WyreStorm NHD-500-TX/RX; Just Add Power 3G Ultra (707POE/708POE)* (NHD-500-RX ~$660-810; J+P 3G TX $1,099 MSRP; DM-NVX-360 roughly $1,000-1,800 list (street varies, used far less)) JPEG 2000 / proprietary-wavelet 1GbE tier delivers 4K60 4:4:4 over standard gigabit with 'no perceptible latency' (sub-frame, roughly <1 frame = <16.7 ms; JPEG2000 products typically 1-2 frames) at ~300-900 Mbps per stream. This is what Crestron DM NVX ('Pixel Perfect Processing', an improved JPEG 2000), WyreStorm NHD-500, Just Add Power 3G Ultra, and KanexPro AVO-IPJP2K actually ship in volume; all are PoE, all use IGMP multicast for switching, and HDCP 2.2/2.3 terminates in the endpoint silicon.
  - Relevance: This is the tier that satisfies yoke's transparency requirement on existing UniFi gigabit PoE hardware. It is the production-grade mainstream of pro AVoIP, and its IGMP-join-driven decoder switching matches yoke's no-central-controller requirement exactly.
- **[high]** — *intoPIX TicoXS IP cores; AWS Elemental Live; MainConcept JPEG XS* JPEG XS (ISO/IEC 21122) is formally visually lossless at up to ~10:1 compression with end-to-end latency of a few video lines (<1 ms), and intoPIX/Macnica demonstrated 4K60 4:4:4 10-bit under 1 Gbps over ST 2110 on 1GbE. However, in pro-AV boxes it ships mostly in broadcast/ST 2110-22 gear (AWS Elemental, MainConcept, Semtech-based) — most 1GbE pro-AV matrix products still ship JPEG 2000 as of 2026.
  - Relevance: JPEG XS is the technically best-fit codec for yoke's 1GbE tier (visually lossless + ~zero latency + constant bitrate that engineers cleanly for multicast bandwidth planning), but hardware selection should verify whether a candidate endpoint actually uses XS vs J2K rather than assuming.
- **[high]** — *ZeeVee ZyPer4K; WyreStorm NHD-600-TRX/TRXF* (typically ~$1,200-2,000 per endpoint (vendor-quote pricing; exact figures not published)) SDVoE (10GbE tier) transports uncompressed video up to 4K30 4:4:4 / 4K60 4:2:0 at ~30 µs latency, and 4K60 4:4:4 8-bit via 'light' visually lossless compression at ~8.7 Gbps with <120 µs latency (SDVoE markets 'as low as 100 microseconds'). Shipping products: ZeeVee ZyPer4K/ZyPer4K-XS, WyreStorm NHD-600-TRX (copper) and NHD-600-TRXF (fiber) transceivers.
  - Relevance: Defines exactly what 10G buys yoke: true pixel transparency (uncompressed or ~1.4:1 light compression) and microsecond latency, i.e. genuinely indistinguishable from a cable — at the cost of 10G PoE switching and pricier endpoints. A clean future upgrade tier, not a day-one requirement.
- **[high]** VESA DSC achieves ~3:1 visually lossless compression validated under ISO/IEC 29170-2 flicker testing (observers fail to distinguish original >75% of trials). DSC is a display-link codec (DP/HDMI 2.1 FRL), not a network codec — it appears inside HDMI 2.1 sources/sinks, not in AVoIP boxes.
  - Relevance: Calibrates the term 'visually lossless': the industry-accepted subjective test standard (ISO 29170-2) is the same bar JPEG XS and DSC are judged by. Also warns that an HDMI 2.1/DSC source may hand endpoints a compressed FRL stream that 1GbE-era encoder silicon cannot ingest — cap yoke's supported formats at HDMI 2.0 (18 Gbps) signals.
- **[medium]** Bandwidth planning on gigabit: a 4K60 4:4:4 JPEG2000/XS stream occupies roughly 700-900 Mbps of a 1GbE port, meaning one video stream per producer port with little headroom for USB-over-IP, audio breakaway, and control on the same link; 10GbE (or a second NIC/VLAN) removes this contention. UniFi gigabit PoE switches handle this only if IGMP snooping + querier are correctly enabled, since every stream is multicast at near-line-rate.
  - Relevance: Directly shapes yoke's network design: per-port budget math, the need for IGMP snooping/querier on the UniFi switches (an unsnooped 900 Mbps multicast flood takes down the segment), and why the debug snooper port must itself be able to sink ~1 Gbps when it joins a group.

### Sources

- <https://sdvoe.org/power-of-10/>
- <https://www.wyrestorm.com/product/nhd-600-trx/>
- <https://www.wyrestorm.com/product/nhd-600-trxf/>
- <https://www.intopix.com/jpeg-xs>
- <https://ds.jpeg.org/whitepapers/jpeg-xs-whitepaper.pdf>
- <https://en.wikipedia.org/wiki/JPEG_XS>
- <https://aws.amazon.com/blogs/media/aws-elemental-live-introduces-jpeg-xs-for-low-latency-visually-lossless-contribution-to-the-cloud/>
- <https://www.mainconcept.com/jpeg-xs>
- <https://www.businesswire.com/news/home/20190204005319/en/>
- <https://docs.crestron.com/en-us/9496/Content/Topics/Overview/Features-350.htm>
- <https://docs.crestron.com/en-us/9496/Content/Topics/Specifications/Specifications-350.htm>
- <https://www.avnetwork.com/news/crestron-launches-new-video-codec-for-dm-nvx-line>
- <https://www.crestron.com/Products/Catalog/AV-Over-IP/DM-NVX-AV-Over-IP/Video-Endpoint/DM-NVX-360>
- <https://www.wyrestorm.com/product/nhd-500/>
- <https://www.avalive.com/Wyrestorm/NHD-500-RX/334733/productDetail.php>
- <https://www.svconline.com/products/just-add-power-3gpoe-ultra-hd-over-ip-410185>
- <https://www.justaddpower.com/tag/3g-ultra-hd-over-ip/>
- <https://store.kanexpro.com/products/avo-ipjp2k>
- <https://www.wyrestorm.com/product/nhd-120-tx/>
- <https://www.zeevee.com/av-over-ip/zyper4k/>
- <https://products.sdvoe.org/avcat/ctl18883/index.cfm?manufacturer=zeevee&product=zyper4k-sdvoe>
- <https://en.wikipedia.org/wiki/Display_Stream_Compression>
- <https://vesa.org/vesa-display-compression-codecs/>
- <https://www.rambus.com/blogs/vesa-display-stream-compression-dsc-the-complete-guide/>


## controllerless-prior-art

Controller-less AV routing is well-proven prior art: the dominant pattern is receiver-driven switching, where each encoder streams to a deterministic multicast group and a decoder switches sources purely by IGMP leave/join, triggered by a small API on the decoder itself (Just Add Power's "Multicast Switching" mode works exactly this way via Telnet/HTTP to the decoder; their alternative "VLAN Switching" mode instead reconfigures the switch and is faster but needs switch-write access, making it a worse fit for yoke). Dante proves the ephemeral-controller model at production scale: mDNS/DNS-SD discovery, subscriptions stored on the devices themselves and keyed by device name (not IP/MAC), self-healing across power cycles, with Dante Controller acting only as a transient configuration tool. NMOS IS-04/IS-05 formalizes the same architecture for broadcast: IS-04 has an explicit peer-to-peer mDNS mode (`_nmos-node._tcp` with version-counter TXT records) that removes the registry requirement on a single subnet, and IS-05 puts the connection API on each node with staged/active transaction semantics, so any short-lived client can make a routing change and disappear. A distributed pairing protocol for yoke should borrow: deterministic channel-to-multicast-address mapping, per-device HTTP connection API with staged/activate semantics, name-based persistent subscriptions on the consumer side, mDNS/DNS-SD (not SSDP) advertisement with change counters, and passive observability via the multicast/mDNS traffic itself. Open-source reference implementations exist for both worlds (sony/nmos-cpp, bbc/nmos-web-router, NVIDIA NvNmos, netaudio, teodly/inferno).


### Findings

- **[high]** — *Just Add Power Omega/Ultra/MaxColor AVoIP* Just Add Power supports two switching protocols. Multicast Switching: all devices in one VLAN, each encoder transmits to a channel-derived multicast address, and a decoder changes source by changing its subscribed 'channel', which triggers IGMP leave/join on the decoder itself; commands go directly to the decoder via Telnet port 23 (e.g. 'channel -v 100'), HTTP API (POST /cgi-bin/api/command/channel with {"cmd":"set","channel":100}), or web UI. VLAN Switching: source change is done by reconfiguring the decoder's switch-port VLAN membership to match the encoder's VLAN, which is seamless/instant but requires writing switch config (JADConfig/AMP or control-system drivers).
  - Relevance: Direct commercial precedent for yoke's target architecture: the multicast method needs no central controller and no switch-write access — an ephemeral CLI hitting a per-decoder API is exactly how JAP dealers deploy it. The tradeoff is documented too: IGMP-join switching is 'not instant and seamless' like VLAN switching, so yoke should expect sub-second but not glitch-free source changes on join-based switching.
- **[high]** — *Just Add Power (Flexible Mode)* JAP's multicast addressing is deterministic and self-describing: fixed first octets per service type (defaults video 239.92.0.0, audio 239.93.0.0, USB 239.94.0.0) with the last two octets encoding the channel number 0-9999 (channel 100 -> 239.92.1.0 pattern). Note they carve out separate multicast ranges for video, audio, and USB as parallel services.
  - Relevance: A pure function from (service type, channel/producer ID) to multicast address means a decoder or debug snooper can compute where any stream lives without asking anyone — no registry lookup, no controller. Yoke should adopt an equivalent scheme, including a distinct range for its USB-over-IP service. Also validates treating USB as just another multicast-switched service alongside AV.
- **[high]** — *AMWA NMOS IS-04* NMOS IS-04 does not strictly require a central registry: it defines a peer-to-peer mode where nodes advertise via mDNS service type _nmos-node._tcp with TXT records api_proto, api_ver, api_auth plus per-resource 8-bit version counters (ver_src, ver_flw, ...) that increment on change so controllers detect updates without polling. Nodes SHOULD assume peer-to-peer operation if no Registration API is advertised. Caveats: P2P is 'optional from v1.3', mDNS doesn't cross routed subnet boundaries, and AMWA recommends registered mode for large/resilient fixed installations.
  - Relevance: Standards-grade prior art for registry-less discovery on a flat homelab LAN — exactly yoke's deployment shape. The version-counter-in-TXT-record trick is worth stealing: it lets an ephemeral CLI or snooper know cheaply whether cached device state is stale. The subnet limitation is acceptable for a UniFi single-L2 homelab but should be documented as a constraint.
- **[high]** — *AMWA NMOS IS-05; sony/nmos-cpp; bbc/nmos-web-router; NvNmos* NMOS IS-05 places the connection-management API on each sender/receiver node (not on a controller): a client PATCHes a receiver's staged endpoint with the sender's transport parameters/SDP, then activates (immediate or scheduled) — a staged/active two-phase model. The controller is just an HTTP client that can exit afterward; connection state lives on the endpoints. Open-source implementations: sony/nmos-cpp (C++ registry + node, IS-04/IS-05, Apache-2.0), bbc/nmos-web-router (web routing UI over IS-04/IS-05), rhastie/build-nmos-cpp (containerized registry+controller), NVIDIA NvNmos, AMWA nmos-sender-receiver-framework (GStreamer-based senders/receivers).
  - Relevance: This is the strongest design template for yoke's per-device API: staged/active gives atomic, verifiable re-pairing (write config, then activate; read back /active to confirm), and it is proven interoperable across dozens of broadcast vendors. Yoke could even literally implement IS-04 P2P + IS-05 on its endpoint wrappers to get free tooling (nmos-web-router as the debug UI) instead of inventing a bespoke API.
- **[high]** — *Audinate Dante* Dante's model: devices advertise themselves and their TX channels via mDNS/DNS-SD; Dante Controller is only an ephemeral setup tool — subscriptions are 'saved directly to the devices themselves' and survive power cycles, disconnection, and reconfiguration. Receivers re-resolve their subscribed source by device name (not MAC or IP), so subscriptions self-heal when devices come back with new addresses. Documented failure mode: renaming a device after subscriptions were made breaks the name-based references (e.g. Biamp warns subscriptions are lost after reboot if hostnames were edited out-of-band).
  - Relevance: Dante is the largest-scale production proof (hundreds of thousands of installs) that 'no always-on controller' works in the wild. Yoke should copy: (1) pairing state persisted on the consumer endpoint, (2) pairing keyed to a stable producer name/ID resolved via mDNS at attach time, not to an IP address, and (3) treat rename as a first-class dangerous operation. Note Dante actually needed a central product (Dante Domain Manager) only to cross subnets and add auth — the same pressure yoke will face if it outgrows one VLAN.
- **[medium]** — *mDNS/DNS-SD vs SSDP* For discovery, mDNS/DNS-SD (RFC 6762/6763) is the better foundation than SSDP: DNS-SD gives structured service typing, TXT metadata, and unicast responses to multicast queries, and is what both Dante and NMOS chose; SSDP is tied to the UPnP ecosystem, uses HTTP-over-UDP NOTIFY/M-SEARCH, and is effectively legacy for new AV designs. Both are single-subnet without relays, and both generate meaningful multicast chatter on Wi-Fi (irrelevant on wired PoE). They are not mutually exclusive — a device can advertise via both.
  - Relevance: Settles yoke's discovery-protocol choice: use DNS-SD service types (e.g. _yoke-producer._tcp / _yoke-consumer._tcp) with TXT records for capabilities and state counters. mDNS is also passively observable — the debug snooper can build a full device inventory just by listening, satisfying the passive-verification requirement. UniFi gear additionally has mDNS reflection if multiple VLANs ever appear.
- **[high]** — *SMPTE ST 2110 / NMOS IS-06* In SMPTE ST 2110 practice there are two routing philosophies: receiver-driven IGMP join (receiver sends IGMP join, IGMP-snooping switch forwards the group to that port only) versus SDN/orchestrated (NMOS IS-06 or vendor SDN programs the switches directly for deterministic bandwidth). Facilities needing guaranteed non-blocking behavior use orchestration; smaller systems run plain IGMP snooping + querier. Prerequisites for the IGMP path: exactly one active querier on the VLAN, snooping enabled, and ideally fast-leave, or streams flood/linger.
  - Relevance: Confirms IGMP-driven receiver-side switching is a legitimate production pattern, not a hobbyist shortcut — broadcast plants run it where bandwidth is over-provisioned. For yoke on gigabit UniFi with ~1G streams, the design must verify IGMP snooping + querier configuration on the switch (UniFi supports both) and should document that multiple uncapped streams can oversubscribe 1G uplinks, which is the exact problem IS-06/SDN exists to solve at larger scale.
- **[high]** — *netaudio; teodly/inferno* (0 (open source)) Open-source Dante-adjacent control planes exist from reverse engineering: chris-ritsen/network-audio-controller ('netaudio' on PyPI) is a cross-platform CLI that discovers Dante devices and sets subscriptions without Dante Controller (protocol knowledge derived from packet captures; author calls it guesswork/unreliable); teodly/inferno is a Rust user-space Dante endpoint implementation (mDNS discovery, unicast/multicast audio, PTPv1/v2 via Statime, ~12MB RAM, alpha quality, GPL/AGPL); Bencosterton/dante-network-control is another CLI.
  - Relevance: Two lessons for yoke: (1) an ephemeral CLI that pokes per-device UDP/HTTP endpoints is sufficient to run a whole routed-AV network — this is precisely yoke's proposed control model, already demonstrated against Dante; (2) a cautionary tale — Dante's closed protocol forced years of fragile reverse engineering, so yoke's device API and wire announcements should be openly documented from day one.
- **[high]** — *Just Add Power VLAN Switching* The JAP VLAN-switching method depends on tooling (JADConfig/AMP or control-system drivers) issuing switch CLI commands per source change (e.g. Cisco SG300 'switchport general allowed vlan add 14 untagged'), tying the control plane to specific supported switch models; the multicast method touches only endpoints and is switch-model-agnostic.
  - Relevance: Argues against any yoke design that reconfigures UniFi switches per pairing: it couples the system to switch APIs (UniFi's config API is controller-mediated, the opposite of yoke's no-central-controller requirement) and breaks the 'any switch that does IGMP correctly' portability property. Endpoint-only IGMP switching keeps the switch as dumb, replaceable infrastructure.

### Sources

- <https://support.justaddpower.com/kb/article/346-multicast-switching-protocol/>
- <https://justaddpower.happyfox.com/kb/article/349-vlan-switching-protocol/>
- <https://justaddpower.happyfox.com/kb/article/279-single-vlan-multicast-switching-with-omega-ultra-and-maxcolor-series/>
- <https://specs.amwa.tv/is-04/releases/v1.3.0/docs/3.2._Discovery_-_Peer_to_Peer_Operation.html>
- <https://specs.amwa.tv/is-04/releases/v1.3.3/docs/Discovery_-_Registered_Operation.html>
- <https://specs.amwa.tv/nmos/branches/main/docs/FAQ.html>
- <https://github.com/sony/nmos-cpp>
- <https://github.com/bbc/nmos-web-router>
- <https://github.com/rhastie/build-nmos-cpp>
- <https://github.com/AMWA-TV/nmos-sender-receiver-framework>
- <https://nvidia.github.io/nvnmos/>
- <https://www.getdante.com/support/faq/what-is-dante-discovery/>
- <https://support.biamp.com/General/Networking/Dante>
- <https://studio-tech.com/tech-notes/problem-with-dante-subscriptions-re-establishing/>
- <https://audinateweb.sfo2.cdn.digitaloceanspaces.com/wp-content/uploads/2022/03/dante-information-for-network-admins.pdf>
- <https://github.com/chris-ritsen/network-audio-controller>
- <https://github.com/chris-ritsen/network-audio-controller/wiki/Technical-details>
- <https://github.com/teodly/inferno/>
- <https://github.com/Bencosterton/dante-network-control>
- <https://pypi.org/project/netaudio/>
- <http://www.zeroconf.org/ZeroconfAndUPnP.html>
- <https://utahscientific.com/smpte-st-2110-nmos-control/>
- <https://netinsight.net/blog/what-is-smpte-2110-and-nmos-all-about/>


## sbc-alternative

A DIY endpoint path is technically feasible but only on one platform class: RK3588 boards (e.g. Orange Pi 5 Plus, ~$90-130) have native HDMI-in up to 4K60, a hardware encoder good for 1080p60 with large headroom via rkmpp/GStreamer, and rapidly maturing mainline support (hdmirx V4L2 driver merged ~Linux 6.15, H.264/H.265 decoders merged for Linux 7.0 by Collabora); the Raspberry Pi path is NOT credible for 1080p60 (TC358743 tops out at 1080p50 YUV even on 4-lane compute modules with known kernel/EDID reliability bugs, and Pi 5/CM5 dropped the hardware H.264 encoder entirely). However, three hard problems remain: (1) bit-transparent HDMI is impossible over 1GbE (1080p60 is ~3-4.5 Gbps uncompressed), so any DIY endpoint transcodes, adding roughly 60-150 ms glass-to-glass versus sub-frame commercial JPEG-XS/SDVoE appliances; (2) HDCP is fatal — a DIY box can never be an HDCP-licensed repeater, so protected sources either black-screen or require DMCA-problematic strippers, whereas licensed appliances handle HDCP end-to-end lawfully; (3) PoE, EDID/hotplug robustness, watchdogs and long-run stability are all engineering you must do yourself, and field reports (PiKVM/uStreamer, armbian hdmirx threads) show real regressions. Verdict: for production "works in the wild" use, endpoints should be commercial HDMI-over-IP appliances with yoke as the control plane; an RK3588-based DIY endpoint is a good fit for the passive debug snooper and for lab/secondary links, not as the primary fleet.


### Findings

- **[high]** Uncompressed 1080p60 HDMI is ~3 Gbps of pixel data (~4.5 Gbps TMDS), so truly bit-transparent transport over 1GbE is physically impossible; every SBC/DIY path must compress (H.264/H.265/MJPEG), which contradicts strict payload transparency — the DIY endpoint becomes a transcoder, not a wire.
  - Relevance: Sets the ceiling for the whole DIY lane: on gigabit UniFi switches a DIY endpoint can only ship compressed video, with the quality/latency/HDR-passthrough compromises that implies.
- **[high]** — *Toshiba TC358743 + Raspberry Pi CM4* ($25-50 adapter + $60-90 CM4) TC358743 HDMI-to-CSI on Raspberry Pi cannot do 1080p60: 2 CSI lanes (all standard Pis, Waveshare boards) max out at 1080p50 YUV / 1080p30 RGB; 1080p60 requires a Compute Module with a 4-lane adapter (B102) and is still borderline.
  - Relevance: Kills the cheapest and best-documented Pi capture path for the stated 1080p60 requirement.
- **[high]** — *PiKVM-class TC358743 capture* TC358743 capture has documented production-reliability problems: EDID must be hand-managed per source, corrupted frames at 1080p50 RGB24 on 4-lane, kernel 6.6 regression producing garbage in encoded streams, and unexplained daytime crashes reported by PiKVM/uStreamer users.
  - Relevance: Direct evidence that the most mature DIY HDMI-capture ecosystem still needs babysitting — a red flag for 'works in the wild'.
- **[high]** — *Raspberry Pi 5 / CM5* Raspberry Pi 5 / CM5 (BCM2712) removed the hardware H.264 encoder and H.264 decoder (only an HEVC decoder remains); 1080p60 must be software-encoded on the A76 cores, burning most of the CPU and power budget of a PoE-powered endpoint.
  - Relevance: Undermines the newest Pi generation as a producer endpoint; the decoder gap also hurts it as a consumer endpoint for H.264 streams.
- **[high]** — *Orange Pi 5 Plus / NanoPC-T6 (RK3588)* ($90-130 board) RK3588 is the only credible DIY producer platform: native HDMI RX (4K60 capable), hardware encode rated far beyond 1080p60 (1080p up to ~480fps, 8K30) via Rockchip MPP with working GStreamer/OBS integration; Orange Pi 5 Plus demonstrably streams 4K60 HDMI-in with audio on vendor kernels.
  - Relevance: This is the realistic hardware if yoke ever ships DIY endpoints — one board covers capture, encode, and 2.5GbE.
- **[high]** — *RK3588 hdmirx / VDPU381* RK3588 mainline support is maturing but recent: the Synopsys HDMI RX V4L2 driver (Shreeya Patel/Dmitry Osipenko, Collabora) merged around Linux 6.15; H.264/H.265 hardware decoders merged for Linux 7.0; earlier vendor-kernel HDMI-in had missing ioctls (VIDIOC_G_PARM crashed OBS) and needed device-tree overlay fixes (hpd-gpios) in Armbian.
  - Relevance: Mainline trajectory is good for long-term maintainability, but the churn means a DIY fleet today rides bleeding-edge kernels — a support cost commercial appliances externalize.
- **[high]** — *MacroSilicon MS2109 / MS2130* ($10-25) Cheap UVC dongles are marginal: MS2109 is USB2-limited (1080p30 MJPEG, quirky stereo-audio hack, happens to ignore HDCP — legally gray), MS2130 does honest 1080p60 YUV over USB3 for ~$10-20 but does NOT pass HDCP-protected sources; both are uncontrolled-supply-chain parts with clone/firmware variance.
  - Relevance: Fine for the debug snooper or bench work; not a defensible foundation for a production fleet.
- **[medium]** — *Magewell Eco Capture HDMI 4K M.2 + Pineboards Modulo5 IO PoE+* ($300 capture + $60-100 carrier + $75 CM5) Professional PCIe/M.2 capture (Magewell Eco Capture HDMI 4K M.2, ~$300 new, $60-100 used) plus a CM5/RK3588 carrier is the highest-quality DIY capture route, and PoE+ carriers exist (Pineboards Modulo5 IO PoE+), but you are then assembling a multi-vendor appliance yourself and PoE+ (25 W) budgets get tight with capture + encode.
  - Relevance: Shows the DIY bill of materials converges on commercial-appliance pricing once you demand pro capture, PoE, and an enclosure — eroding the cost argument.
- **[high]** Most RK3588 HDMI-in boards (including Orange Pi 5 Plus) have no PoE; powering from UniFi PoE ports requires 802.3at/bt splitters or add-on modules per endpoint, adding cost, cabling, and another failure point.
  - Relevance: PoE is a stated deployment requirement; DIY endpoints don't get it for free the way purpose-built AV-over-IP encoders/decoders do.
- **[high]** HDCP is the decisive blocker: a DIY endpoint cannot obtain an HDCP repeater license, so protected sources (streaming sticks, Blu-ray, consoles by default) either refuse output or require active HDCP strippers whose sale/use is DMCA anti-circumvention territory; licensed commercial appliances legitimately re-encrypt HDCP hop-by-hop, which matches yoke's requirement that HDCP be the endpoint hardware's job.
  - Relevance: Yoke explicitly wants HDCP handled by endpoint hardware, never software — only licensed commercial endpoints can actually satisfy that for protected content.
- **[medium]** Realistic glass-to-glass latency for a tuned SBC pipeline (HW capture -> HW H.264/H.265 encode -> RTP multicast -> HW decode -> KMS output) is on the order of 60-150 ms; comparable Jetson pipelines measure ~92 ms at 60fps, and untuned software paths exceed 500 ms. Commercial light-compression appliances (JPEG-XS/SDVoE class) achieve sub-frame (<16 ms) latency that no GStreamer H.264 DIY pipeline reaches.
  - Relevance: Determines fitness for interactive uses (desktops, KVM-adjacent, gaming): DIY latency is acceptable for signage/monitoring, marginal for interactive AV.
- **[high]** The multicast architecture yoke wants maps cleanly onto DIY software: GStreamer/FFmpeg RTP over UDP multicast lets consumers re-pair via IGMP join/leave with no central controller, and a passive snooper can subscribe to the same group — this control-plane fit is the strongest argument for at least one DIY component (the snooper).
  - Relevance: Even if endpoints are commercial, an RK3588/Pi box running a GStreamer subscriber is the natural implementation of the required debug snooper, provided appliances emit standard RTP rather than proprietary framing.

### Sources

- <https://forums.raspberrypi.com/viewtopic.php?t=281972>
- <https://forums.raspberrypi.com/viewtopic.php?t=120702>
- <https://www.cnx-software.com/2024/12/21/rockchip-rk3588-mainline-linux-support-current-status-and-future-work-for-2025/>
- <https://github.com/Joshua-Riek/ubuntu-rockchip/issues/830>
- <https://github.com/armbian/build/pull/10172>
- <https://www.cnx-software.com/2022/10/09/10-hdmi-to-usb-3-2-video-acquisition-board-ms2130/>
- <https://forum.videohelp.com/threads/407889-MS2130-chipset-the-new-low-budget-Hdmi-capture-card-killer>
- <https://github.com/awawa-dev/HyperHDR/discussions/612>
- <https://forums.raspberrypi.com/viewtopic.php?t=349669>
- <https://forums.developer.nvidia.com/t/video-streaming-improvement-questions/115181>
- <https://news.ycombinator.com/item?id=38068801>
- <https://forums.raspberrypi.com/viewtopic.php?t=391283>
- <https://obsproject.com/forum/threads/v4l2-multiplanar-support-rockchip-mpp-encoder-rga-for-rk3588-native-4k60-hdmi-input.161205/>
- <https://jellyfin.org/docs/general/post-install/transcoding/hardware-acceleration/rockchip/>
- <https://www.cnx-software.com/2026/02/27/rockchip-rk3588-rk3576-h-264-and-h-265-video-decoders-mainline-linux/>
- <https://www.notebookcheck.net/Orange-Pi-5-Plus-Rockchip-RK3588-SBC-debuts-from-US-89-with-dual-2-5-Gigabit-Ethernet-and-HDMI-2-1-connectivity.717236.0.html>
- <https://www.youtube.com/watch?v=wV2eLz7stlI>
- <http://www.drhdmi.eu/dictionary/hdcp-strippers-legality.html>
- <https://gizmodo.com/how-hollywood-can-use-the-law-to-force-you-to-upgrade-y-1750966255>
- <https://github.com/raspberrypi/linux/issues/6322>
- <https://github.com/raspberrypi/linux/issues/4098>
- <https://github.com/pikvm/pikvm/issues/1434>
- <https://www.magewell.com/products/eco-capture-hdmi-4k-m2>
- <https://interfacinglinux.com/2025/04/18/magewell-eco-m-2-hdmi-capture-card/>
- <https://www.electronics-lab.com/pineboards-raspberry-pi-cm4-cm5-carrier-boards-brings-poe-and-m-2-nvme-support-to-raspberry-pi-cm5/>
- <https://www.collabora.com/news-and-blog/news-and-events/kernel-615-divide-and-conquer.html>
- <https://www.collabora.com/news-and-blog/news-and-events/mainline-video-capture-and-camera-support-for-rockchip-rk3588.html>
- <https://patchwork.kernel.org/project/linux-media/patch/20240216094922.257674-5-shreeya.patel@collabora.com/>
