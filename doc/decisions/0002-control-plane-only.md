# 0002 — Yoke is a control plane; the media plane is endpoint hardware

Date: 2026-08-08. Status: accepted.

Transparent transport of HDMI (up to 18 Gb/s TMDS) or USB over switched
Ethernet cannot be done in our software on PoE-class hardware, and any
software path that decodes video would put us inside the HDCP boundary.
Commercial AV-over-IP encoders/decoders and USB-over-IP extenders already
transport these signals with endpoint silicon, multicast media streams,
and licensed HDCP handling.

Yoke therefore implements only the control plane: discovery, pairing
state, convergence, and observability. Media transport is delegated to a
per-device **driver** that translates "consume producer P" into the
device's native mechanism (IGMP join, device API call, `usbip attach`).
The reference `igmp`/`usbip` drivers make the system real on open Linux
endpoints; appliance drivers are thin additions informed by
`doc/research/HARDWARE.md`.

Consequence: "do not interpret the signals" is satisfied absolutely — no
yoke code path ever receives media bytes.
