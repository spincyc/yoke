# 0027 — Tiered USB product line and a native C data path

Date: 2026-11-05. Status: accepted. Hardware behavior remains bench-unverified (⚠).

## Context

The Python gadget worker (ADRs 0021/0022) proved the USB data path in
simulation and in a real-kernel VM. The owner then raised three
concerns, recorded here with their outcomes:

- **Python cannot carry USB 2.0 at line rate on the target CPUs.** The
  device side already uses kernel C (`usbip-host`). The computer-side
  worker is Python. It moved about 93 MB/s on an x86 VM, which is likely
  10–15 MB/s on a Cortex-A55 (inference).
- **Linux is not needed for every box.** It costs boot time and power,
  matters most on the bus-powered computer side, and adds an image to
  maintain.
- **Cost and size matter.** An i.MX RT1170 evaluation kit (about $217,
  large) plus a custom 4-layer PCB was rejected as the tier-2 vehicle for
  now.

USB/IP stays the open data-plane protocol in every tier, so any tier
interoperates with stock Linux `usbip` at either end.

## Decision

**1. Three product tiers, each with one USB role per box.**

| Tier | Devices | Hardware | Network | Data path |
|---|---|---|---|---|
| 1 | HID, game/flight controllers, serial, MIDI, audio (low and full speed) | ESP32-S3 + WIZnet W5500 (full-speed USB OTG on GPIO19/20) | 10/100 over SPI | C firmware, no OS |
| 1.5 | High-speed-compatible devices, about 11 MB/s ceiling | ESP32-P4 (HS OTG with PHY, 10/100 EMAC), e.g. M5Stack Unit PoE-P4 | 10/100 | Same C firmware as tier 1, plus the compression extension |
| 2 | Full USB 2.0 | Radxa ZERO 3E (RK3566 DWC3 device controller, GbE); PoE HAT on the device side only | GbE | Kernel `usbip-host` on the device side; **native C gadget worker** on the computer side, replacing the Python worker in the data path |
| 3 | USB 3.0 | none selected | 5–10GbE | Research only |

**2. One portable C core shared by every tier.**
- **Sans-I/O.** The core does no I/O and no allocation in hot paths; each
  platform (Linux, ESP-IDF) supplies small backend operation tables.
- **Placement.** It lives under `native/`, and each library builds both
  as a plain CMake library and as an ESP-IDF component.
- **Modules:**
  - USB/IP wire codec;
  - computer-side presentation (descriptor cache and rewrite, control
    relay, endpoint pumps);
  - device-side export (a USB/IP server over a host-controller backend);
  - YWP/1 frames and node.
- **Licensing.** The Python implementation is the executable reference.
  Dependencies must be MIT/BSD/Apache-compatible: TinyUSB (MIT), the
  ESP-IDF USB Host library (Apache-2.0), LZ4 (BSD-2). **No GPL code
  enters firmware**, and Linux kernel USB code is not copied.

**3. Compression is an optional, negotiated, documented extension to
USB/IP.**
- **Fallback.** Plain USB/IP stays the fallback, and a peer that does not
  negotiate the extension sees standard USB/IP.
- **Order of levers.** Latency hiding (pipelining, BOT read-ahead and
  write-behind) comes before compression.
- **Encodings.** Zero/repeat-block elimination, then adaptive LZ4 with an
  incompressible bypass, then a block cache. Tiny interrupt transfers are
  never compressed.
- **Decisions rest on evidence.** `usbmon` traces of real devices,
  replayed offline, decide which levers ship.

**4. Verification is software-first and real-kernel where possible.**
- **C cores.** They compile on the host and run against the existing
  Python simulator (`tests/usbsim`), the YWP/1 vectors and mixed
  Python/C simnet scenarios.
- **Tier-2 worker.** It runs in the QEMU/KVM real-kernel loopback
  (`make verify-usb-kernel`).
- **Device-side export core.** It is exported to the guest kernel's
  `vhci-hcd` from a simulated device backend.
- **Limits.** Platform glue that cannot run off-target is build-verified
  only, and its bench steps are listed.

**5. Process boundaries are unchanged.**
- The Python agent and control plane stay as they are (ADRs 0022–0024).
- The native worker speaks the ADR 0022 control-socket schema, so the
  `usb-gadget` driver supervises either worker.
- MCU boxes implement YWP/1 in C. That needs no protocol change, because
  ADR 0023's transmitted-byte authentication makes YWP/1 implementable
  outside Python.

## Consequences

- **Tier 1 and 1.5 boxes** boot in under a second and draw under 1 W, so
  the computer-side box is present at BIOS (inference; to be measured).
- **Tier 2** keeps Linux for gigabit Ethernet and the DWC3 controller. A
  minimal image with a pinned kernel is follow-up work; it removes the
  stock-kernel Raw Gadget availability risk.
- **The Python worker becomes the reference and test oracle.** Divergence
  between the Python and C workers is a test failure, not a fork.
- **Tier 1.5 throughput is data-dependent.** Its claims are stated as
  "high-speed compatible, about 11 MB/s floor, faster on compressible
  data".
