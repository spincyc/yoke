# Yoke implementation handoff

Updated 2026-11-04. Software for both USB boxes and the IR endpoint is
implemented and verified **in software only**: a pure-software simulation
and, for USB, a real-kernel loopback in a VM. **Nothing has run on the
boards.** The ordered ZERO 3E boards and PoE HAT were expected
2026-11-05. The first bench session decides whether this works.

## State at a glance

| Area | State | Evidence | Next |
|---|---|---|---|
| Computer-side USB bridge | Userspace USB/IP client + Raw Gadget presentation in a separate worker (ADRs 0021/0022) | `tests/usbsim` end-to-end simulation; `make verify-usb-kernel` (QEMU/KVM, dummy_hcd + raw_gadget + usbip-host, 8 MiB checksummed I/O at 480M, detach, remote loss, usbipd restart) | Seven-step bench procedure in `doc/design/USB-DATAPATH.md` |
| Device-side USB | Stock usbipd/usbip-host; data-plane admission limited to the authenticated paired consumer (nftables, ADR 0024); `packaging/zero3e/install.sh device` | Offline packaging tests; `systemd-analyze verify` | Bench: bind, admission ruleset, peripheral power |
| Control plane | Clock-robust admission, bounded sequence floor, transmitted-byte auth with vectors, journal logging, explicit open mode (ADR 0023) | `make verify`, `make simulate` | — |
| Computer-side power | Powered by the computer through the OTG port; no PoE HAT (owner, 2026-11-04) | Schematic: Type-C VBUS feeds `+5V_INPUT`, sink-only CC | Measure draw at idle/boot/load with a USB meter; prefer USB 3/USB-C ports |
| IR | Standalone ESP32-S3/W5500 PoE endpoint, RMT output, opt-in iTach subset on TCP 4998, mutual-TLS admin/OTA (ADRs 0025/0026) | `make verify-firmware` (132 host tests, waveform model), target build 616,016 B | Bench procedure in `doc/design/IR-ENDPOINT.md`; one USB reflash for the 0.2.0 partition table |
| Housing | R7: compact PoE device box 81.4 × 36.25 × 41.7 mm and bare computer box 81.4 × 36.25 × 18.5 mm sharing one base | Independent cold review (second pass after a joint FAIL); one plate submitted 2026-11-04 20:29:59 UTC and verified PRINTING ([print record](hardware/usb-housing/r7/print.json)) | Unpowered fit, seam/HAT-gap bands, cable seating, ten service cycles, powered cooling |

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
make benchmark-control benchmark-usb firmware-bench
```

Simulated and host-model numbers are software evidence only. None of them
establishes hardware compatibility, throughput, or IR timing on silicon.

## Read in this order

| Document | Purpose |
|---|---|
| `doc/design/USB-ENDPOINT.md` | Two-box USB 2 product and power decisions |
| `doc/design/USB-DATAPATH.md` | Gadget data path, simulation, kernel test, bench procedure |
| `doc/design/PROTOCOL.md`, `ARCHITECTURE.md` | Normative YWP/1 and current structure |
| `doc/decisions/0021-*` … `0026-*` | 2026-11-04 decisions |
| `doc/design/DEPLOYMENT.md`, `packaging/zero3e/` | Box install recipe |
| `doc/design/IR-ENDPOINT.md`, `ESP32-UPDATES.md` | IR endpoint and its updater |
| `doc/design/USB-HOUSING.md`, `hardware/usb-housing/r7/` | Enclosure revisions and R7 |
| `doc/research/CHIPSETS.md`, `REVIEW-2026-11-04.md` | Chipset/power evidence and the dispositions of every review finding |

The agent never receives media or USB payload, and the HDCP boundary in
ADR 0011 is unchanged. For each new field failure, keep a deterministic
regression test alongside the fix. Protocol changes start in PROTOCOL.md
and an ADR.
