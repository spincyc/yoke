# Yoke implementation handoff

Updated 2026-11-05. The USB product is now a **tiered line** (ADR 0027).
Every tier shares one portable C core, and everything is verified **in
software only**: simulation, differential tests against the Python
reference, fuzzing, and a real-kernel VM loopback. **Nothing has run on
the boards yet.** The ZERO 3E boards, the PoE HAT and the ESP32-S3
DevKitC + W5500 parts arrived 2026-11-05. Their bench sessions decide
what works.

## Tiers

| Tier | Hardware | Firmware/software | Status |
|---|---|---|---|
| 1: HID, controllers, serial, MIDI (low/full speed) | ESP32-S3 DevKitC N16R8 + W5500 (computer side bus-powered; device side USB-A breakout on GPIO19/20) | `firmware/esp32-usb` (ADR 0030) on `native/yusb` + `native/ywp` | Builds with zero warnings; 182 firmware host tests; self-test image ready to flash |
| 1.5: high-speed compatible, about 11 MB/s floor | ESP32-P4 (e.g. M5Stack Unit PoE-P4) | Same app, FILL+LZ4 compression (ADR 0028) | Build only; no hardware |
| 2: full USB 2.0 | Radxa ZERO 3E; PoE HAT on the device side only | Kernel `usbip-host` on the device side; native C `yoke-gadget-worker` on the computer side (Python worker kept as reference) | VM: about 3x less CPU per MiB than Python; bench next |
| 3: USB 3.0 | none | research only | — |

## State at a glance

| Area | State | Evidence | Next |
|---|---|---|---|
| Portable USB/IP core `native/yusb` | Codec, descriptor cache, computer-side presentation, device-side export with a per-URB pool, fencing, exact footprints, YX extension (ADRs 0027, 0028) | usbsim scenarios on both workers, Python differential tests, 7 fuzz targets, TSan, QEMU/KVM real-kernel loopback (native and Python workers, export core through vhci-hcd) | Bench on the ZERO 3E; usbmon traces of real devices to set YX defaults |
| Pairing in C `native/ywp` | Wire-identical YWP/1 frames and node, CPython-exact numbers, NVS blob (ADR 0029) | 27 vectors, C/Python differential decode, lockstep against `Node`, every simnet scenario in four mixes with byte-identical traffic, real-socket interop with `yoke agent` | Target timing on ESP32 (`/v1/status` `ywp.cost`) |
| ESP32 USB box | Computer role (TinyUSB mirror) and device role (USB Host Library export), YWP/1 pairing, SNTP or forward-only clock PUT, PSRAM | `python3 firmware/verify.py --offline` (182 host tests, S3/P4/IR images) | Bench procedure in `doc/design/USB-TIER1.md` |
| Computer-side USB bridge (Linux) | Native worker plus the Python reference over Raw Gadget (ADRs 0021, 0022) | as above | Seven-step bench procedure in `doc/design/USB-DATAPATH.md` |
| Device-side USB (Linux) | Stock usbipd/usbip-host; data-plane admission (ADR 0024); `packaging/zero3e/install.sh device` | Offline packaging tests | Bench: bind, admission ruleset, peripheral power |
| Control plane | Clock-robust admission, transmitted-byte auth, explicit open mode (ADR 0023); `node.py` compares opaque JSON by canonical bytes and keeps admitted consumers' rows (ADR 0029) | `make verify`, `make simulate` | — |
| IR | ESP32-S3/W5500 RMT endpoint, iTach subset on TCP 4998 (ADRs 0025, 0026); shares `firmware/components` with the USB box | `firmware/verify.py` | Bench procedure in `doc/design/IR-ENDPOINT.md` |
| Housing | R7 compact PoE and bare boxes, printed 2026-11-04 | [print record](hardware/usb-housing/r7/print.json) | Fit, seam and HAT-gap bands, cooling |

## Hardware decisions and their reasons

- **ZERO 3E (RK3566) for both USB boxes.** Its OTG port is a mainline
  DWC3 limited to high speed, a better device controller than dwc2 (Pi)
  or MUSB (Allwinner) at this size. The Raw Gadget driver name for it is
  `dwc3-gadget`, and the worker infers it from sysfs.
- **The computer-side box drops the PoE HAT.** That removes HAT-to-PC VBUS
  backfeed and any PoE-return-to-chassis bonding. Accepted costs:
  - The box reboots with the computer's port power, so the device is
    absent at BIOS unless the port stays powered while the computer is off.
  - USB 2 Type-A ports (500 mA) are marginal under load.
  - The suspend-current limit is not met.
- **The device-side box keeps the RA023 PoE HAT.** It powers the
  peripheral. The HAT takes PoE on pins 1/2/3/6 only (Mode A), so use
  switch ports, not spare-pair injectors. HAT output current and isolation
  are unpublished. Radxa's pillars and screws carry PoE voltage; keep them
  installed and insulated.
- **IR is on an ESP32 RMT endpoint, not the Radxa or a Mega.** Rockchip
  `pwm-ir-tx` uses the sleeping path, which upstream calls inaccurate.
  ADK's AVR code is NEC-only at a fixed 38 kHz. For new hardware, prefer
  an isolated-PoE board; Thermite needs a model allowance before it can
  drive a Yoke IR endpoint (follow-up).

## Remaining risks to settle on the bench

- Raw Gadget availability in the Radxa kernel (`CONFIG_USB_RAW_GADGET`).
  Also check dwc3 EPIPE and deferred-start behaviour on the RK3566, and the
  UDC endpoint budget (debugfs).
- Control OUT with data is acknowledged before the remote device answers,
  so a remote STALL of SET_REPORT/SET_LINE_CODING reaches the host as
  success. See the per-class table in USB-DATAPATH.md.
- Generic bulk OUT without a class hint is slow (one packet per URB);
  mass storage uses BOT sizing.
- **Data-plane admission:** it trusts the source IPv4, so it does not
  stop on-path attackers, and payload is unencrypted. **Site key:** YWP/1
  keeps one site-wide key; any key holder can author intents.
- The iTach API is unauthenticated cleartext, so it is off by default and
  LAN-scoped.

## Workspace and publication

All three clones live under the `thermite/ir` workspace on `feature/ir`.
Do not change branches or work in canonical clones. Publish authorized
changes with `git push -u origin feature/ir`.

Housing print records live in `hardware/usb-housing/r7/print.json` and
Lacuna's `tools/print/dispatch-log.jsonl`. R6 and earlier jobs are
retired; never resend a retired job.

## Verification

```sh
make verify               # format, lint, mypy --strict, tests, docs
make simulate             # control fault campaign
make verify-usb-kernel    # opt-in real-kernel USB loopback (QEMU/KVM, no root)
make verify-firmware      # pinned ESP-IDF build + firmware host tests
make native-usb fuzz-usb tsan-usb   # native worker build, fuzzers, ThreadSanitizer
make ywp-fuzz ywp-bench   # native YWP/1 fuzzing and host cost
make benchmark-control benchmark-usb benchmark-usbtrace firmware-bench firmware-usb-bench
```

On a ZERO 3E: `make native-usb NATIVE_CC=gcc`, then
`packaging/zero3e/install.sh computer --native-worker <path>`. Capture
real-device traffic with `tools/usbtrace/capture.py` and replay it with
`tools/usbtrace/replay.py` to choose YX defaults (`native/README.md`).

Simulated and host-model numbers are software evidence only. None of them
establishes hardware compatibility, throughput, or IR timing on silicon.

## Read in this order

| Document | Purpose |
|---|---|
| `doc/design/USB-ENDPOINT.md` | Two-box USB 2 product and power decisions |
| `doc/design/USB-DATAPATH.md` | Gadget data path, simulation, kernel test, bench procedure |
| `doc/design/PROTOCOL.md`, `ARCHITECTURE.md` | Normative YWP/1 and current structure |
| `doc/decisions/0021-*` … `0026-*` | 2026-11-04 decisions |
| `doc/decisions/0027-*` … `0030-*` | 2026-11-05: tiers, native C cores, YX extension, ESP32 USB box |
| `native/README.md`, `native/ywp/README.md` | Native cores: build, test, sizing, deliberate subsets |
| `doc/design/USB-TIER1.md`, `firmware/esp32-usb/README.md` | ESP32 USB box design, wiring, flashing and bench procedure |
| `doc/design/DEPLOYMENT.md`, `packaging/zero3e/` | Box install recipe |
| `doc/design/IR-ENDPOINT.md`, `ESP32-UPDATES.md` | IR endpoint and its updater |
| `doc/design/USB-HOUSING.md`, `hardware/usb-housing/r7/` | Enclosure revisions and R7 |
| `doc/research/CHIPSETS.md`, `REVIEW-2026-11-04.md` | Chipset/power evidence and the dispositions of every review finding |

The agent never receives media or USB payload, and the HDCP boundary in
ADR 0011 is unchanged. For each new field failure, keep a deterministic
regression test alongside the fix. Protocol changes start in PROTOCOL.md
and an ADR.
