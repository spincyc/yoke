<span id="what-you-can-use-today" class="legacy-anchor" aria-hidden="true"></span>
# Development status

The USB product uses **two Radxa ZERO 3E hardware boxes**: one beside the
USB device and one connected by USB to the user's computer. **USB 2 is
first.** The [USB design](../../../doc/design/USB-ENDPOINT.md) records that
choice. The complete USB connection has not been built and bench-tested.

## USB work

| Part | Status |
|---|---|
| Hardware platform | Radxa ZERO 3E selected; one board in each box |
| Case | Two prints rejected by the owner; rectangular case with slot joint and M3 heat-set fasteners being redesigned |
| Pairing | Software saves pairings, restores them after restart, and handles lost connections |
| Device-side firmware | Linux USB/IP export adapter exists; board integration and real device tests remain |
| Computer-side firmware | Physical USB device/gadget bridge is unfinished |
| End-to-end tests | Still required, through both boxes and a USB cable to a separate computer |

The current Linux USB/IP import adapter is a software development tool.
It ends inside a Linux kernel. It does not replace the computer-side
hardware box or establish that the USB product works. The product does
not require the user's computer to run Linux; supported devices and
computer compatibility still need testing.

## Other work

| Area | Status |
|---|---|
| HDMI | Dedicated boxes planned; existing appliance and GStreamer adapters are available for software development |
| IR | Hardware planned; transmission, learning, and the two-jack pass-through mode remain unfinished |
| Monitoring | Status tools, terminal monitor, Prometheus, and JSON export available |
| Reference media | Synthetic frames, packetization, FEC, jitter, and socket tests available; jpegxs and vc2 are ratio models, not codec implementations |

Earlier USB 3 and 10GbE studies are [research](../engineering/research.md),
not requirements for the first USB prototype. Thermite and other managers
are optional for all Yoke boxes.

[USB hardware](../usb.md) · [USB firmware work](../../../doc/design/USB-ENDPOINT.md#firmware-status) ·
[Run software tests](../start.md)
