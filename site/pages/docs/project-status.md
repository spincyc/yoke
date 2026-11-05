<span id="what-you-can-use-today" class="legacy-anchor" aria-hidden="true"></span>
# Development status

Each USB link uses **two boxes**: one beside the USB device and one
connected by USB to the user's computer. The product is a tiered line:

| Tier | Boxes | For |
|---|---|---|
| 1 | ESP32-S3 | Keyboards, mice, controllers, serial |
| 1.5 | ESP32-P4 | High-speed-compatible devices, about 11 MB/s |
| 2 | Radxa ZERO 3E | Full USB 2.0 |

See [ADR 0027](../../../doc/decisions/0027-tiered-usb-product-line-and-native-c-data-path.md),
the [USB design](../../../doc/design/USB-ENDPOINT.md) and the
[tier 1 design](../../../doc/design/USB-TIER1.md). In every tier, the
complete USB connection has not been built and bench-tested.

## USB work

| Part | Status |
|---|---|
| Hardware platform | Radxa ZERO 3E selected; one board in each box |
| Case | R6 judged too large; compact R7 PoE and computer-side cases printed 2026-11-04; physical fit and cooling not yet tested |
| Pairing | Software saves pairings, restores them after restart, and handles lost connections |
| Device-side firmware | USB/IP export with data-plane admission and a ZERO 3E install recipe; not yet run on the board |
| Computer-side firmware | Tier 2: native C USB/IP-to-gadget worker (the Python worker is the reference); verified in simulation and a real-kernel VM; not yet run on hardware |
| Tier 1/1.5 firmware | ESP32-S3/P4 USB box with both roles and C pairing; builds with zero warnings and passes host tests; self-test image ready to flash |
| Computer-side power | Powered by the user's computer through the OTG port; no PoE HAT on that box |
| End-to-end tests | Still required, through both boxes and a USB cable to a separate computer |

The computer-side box presents the remote device through its own USB
port with Linux Raw Gadget. That path passes an end-to-end software
simulation and a real-kernel loopback test in a virtual machine
(`make verify-usb-kernel`). Neither establishes that the USB product works
on real boards. The older Linux USB/IP import adapter remains a development tool
that ends inside a Linux kernel. The product does
not require the user's computer to run Linux; supported devices and
computer compatibility still need testing.

## Other work

| Area | Status |
|---|---|
| HDMI | Dedicated boxes planned; existing appliance and GStreamer adapters are available for software development |
| IR | ESP32 PoE endpoint firmware with RMT output and an iTach-compatible API, verified in host simulation; bench tests and the two-jack pass-through remain |
| Monitoring | Status tools, terminal monitor, Prometheus, and JSON export available |
| Reference media | Synthetic frames, packetization, FEC, jitter, and socket tests available; jpegxs and vc2 are ratio models, not codec implementations |

Earlier USB 3 and 10GbE studies are [research](../engineering/research.md),
not requirements for the first USB prototype. Thermite and other managers
are optional for all Yoke boxes.

[USB hardware](../usb.md) · [USB firmware work](../../../doc/design/USB-ENDPOINT.md#firmware-status) ·
[Run software tests](../start.md)
