# Chipset and power review (2026-11-04)

Status: desk research and schematic reading on 2026-11-04, before any
board arrived. It answers two questions: are the selected chips right for
the two USB boxes and for IR, and can the computer-side box run without
its PoE HAT? The decisions it supports are in
[USB-ENDPOINT.md](../design/USB-ENDPOINT.md) and ADRs
[0021](../decisions/0021-userspace-usbip-client-and-raw-gadget-presentation.md),
[0022](../decisions/0022-gadget-worker-process-and-metadata-driver.md),
[0025](../decisions/0025-esp32-rmt-ir-endpoint-itach-subset.md) and
[0026](../decisions/0026-esp32-endpoint-health-recovery-and-storage.md).

Flags: **V** verified on a primary source; **I** inferred; **U**
uncertain. **⚠** marks a bench-unverified claim (see `AGENTS.md`).

## Verdict

- **USB boxes: keep the RK3566 Radxa ZERO 3E for both.** The OTG port is a
  mainline DWC3 limited to high speed. At this size it is a stronger USB
  device controller than dwc2 (Raspberry Pi) or MUSB (Allwinner). The
  board also has gigabit Ethernet and a separate USB 3 host port for the
  device side.
- **Where the risks are.** They sit in the board power path, the
  undocumented HAT and the generic proxy, not in the SoC.
- **IR: neither the Radxa nor an Arduino Mega.** Use an ESP32 RMT endpoint.
- **Computer-side box: drop the PoE HAT.** Power it from the computer
  through the OTG port.

## RK3566 USB controllers

| Claim | Flag | Source |
|---|---|---|
| The OTG port is `usb_host0_xhci`, a DWC3 at `fcc00000`; `rk3566-base.dtsi` limits it to `high-speed`, `rk356x-base.dtsi` sets `dr_mode = "otg"` | V | [rk3566-base.dtsi](https://github.com/torvalds/linux/blob/master/arch/arm64/boot/dts/rockchip/rk3566-base.dtsi), [rk356x-base.dtsi](https://github.com/torvalds/linux/blob/master/arch/arm64/boot/dts/rockchip/rk356x-base.dtsi) |
| Rockchip describes the RK3566 DWC3 OTG as USB 2.0 only, 480 Mbps | V (third-party mirror of the vendor guide) | [RK356x USB developer guide](https://yvcherl.github.io/newpdf/Common/USB/Rockchip_RK356x_Developer_Guide_USB_CN.pdf) |
| Mainline dropped `dr_mode="peripheral"` for ZERO 3W/3E because OTG already serves gadgets; a ZERO 3W Ethernet gadget is reported working with a Mac and iPad | V | [patch](https://lists.infradead.org/pipermail/linux-rockchip/2024-August/049715.html), [forum](https://forum.radxa.com/t/overlay-otg-host-mode-not-working-on-zero-3w/24124) |
| Device-endpoint count and TX FIFO RAM are unpublished; DWC3 supports up to 16 bidirectional endpoints | U | [dwc3 docs](https://docs.kernel.org/driver-api/usb/dwc3.html); read the real budget from debugfs on the board ⚠ |
| DWC3 has no low-speed device mode, so low-speed keyboards and mice are presented at full speed | I | Implemented in `yoke.usb.descriptors` |
| The Raw Gadget kernel docs call it "strictly a debugging feature"; it supports FULL and HIGH speed, no hubs, and incomplete isochronous | V | [kernel docs](https://docs.kernel.org/usb/raw-gadget.html), [uapi header](https://github.com/torvalds/linux/blob/master/include/uapi/linux/usb/raw_gadget.h), [raw-gadget](https://github.com/xairy/raw-gadget) |
| dwc3 names its gadget `dwc3-gadget`; Raw Gadget binds by that driver name, separately from the device name `fcc00000.usb` | V | `drivers/usb/dwc3/gadget.c`, `drivers/usb/gadget/legacy/raw_gadget.c` (found by the cold review; fixed in ADR 0021's amendment) |
| USB 2.0 §9.2.6.4: no-data standard requests within 50 ms; each data packet within 500 ms. Real hosts wait about 5 s | V / I | [USB 2.0](https://www.usb.org/document-library/usb-20-specification). Descriptors are cached at import for this reason |

## Power path and the PoE HAT

**OTG port wiring (V, ZERO 3E schematic v1.2 sheet 18)**
([schematic](https://dl.radxa.com/zero3/docs/hw/3e/radxa_zero_3e_v1200_schematic.pdf)):
- Type-C VBUS connects straight to `+5V_INPUT`, the same net as 40-pin
  header pins 2/4. It passes FB120/3A and R107 to `VCC_SYS`, with no
  diode or load switch.
- CC1/CC2 carry only 5.1 kΩ pull-downs, so the port is a sink only.
- `USB_OTG0_VBUSDET` is derived from `VCC_SYS`, and ID is not connected.

**Consequences (I)**
- With a PoE HAT fitted, the HAT's 5 V and the computer's VBUS are tied
  together, so current can flow either way.
- Without the HAT, the computer's port powers the board directly; this is
  the Pi Zero gadget pattern.
- Because VBUS-detect is always asserted, unplugging the computer looks
  like suspend rather than disconnect.

**USB 3 host port (V, sheet 14).** An SY6280AAC with R70 = 4.7 kΩ limits
it to about 1.45 A (I = 6800/R), which is enough for a 500 mA peripheral
([datasheet](https://olimex.com/Products/Components/IC/SY6280/resources/SY6280AAC.PDF)).

**RA023 PoE HAT**
- **V:** Radxa says 802.3af/at. Power is carried on pins 1/2/3/6 only
  (Mode A). PoE does not work without the copper pillars and screws, which
  carry the PoE voltage
  ([docs](https://docs.radxa.com/en/zero/zero3/accessories/3e-poe-hat)).
- **V:** users report failures on spare-pair injectors and success on
  802.3af/at switches
  ([forum](https://forum.radxa.com/t/unable-to-power-zero-3e-using-poe/22882)).
- **U:** output current and isolation are not published. A non-isolated
  HAT would bond PoE return to a PC chassis through USB ground. Olimex
  documents damage from that on its non-isolated ESP32-POE
  ([forum](https://www.olimex.com/forum/index.php?msg=39434)).

**Board draw (U).** One forum measurement of an idle ZERO 3E with gigabit
Ethernet reports about 1.44 W
([FreeBSD forum](https://forums.freebsd.org/goto/post?id=721931)). Radxa
recommends a 5 V/2 A adapter.

| Computer port | Budget | Computer-side box (no peripheral load) |
|---|---|---|
| USB 2 Type-A | 2.5 W (500 mA) | Enough at idle, marginal under proxy load ⚠ |
| USB 3.x Type-A | 4.5 W (900 mA) | Expected fine ⚠ |
| USB-C | 7.5–15 W | Comfortable |

**Decision (owner, 2026-11-04).** The computer-side box has no HAT and
draws power from the computer. Accepted costs:
- the box reboots with the computer's port power;
- the device is absent at BIOS unless the port stays powered while the
  computer is off;
- the suspend-current limit is exceeded.

The device-side box keeps the HAT because it powers the peripheral.
Measure the draw at boot, idle and load before relying on USB 2 ports.

## Alternatives considered

| Option | Device controller / network | PoE and size | For | Against |
|---|---|---|---|---|
| **ZERO 3E (selected)** | DWC3 high speed / GbE | HAT (device side only) | Mainline, every gadget API, USB 3 host port | HAT specs unpublished |
| Raspberry Pi 4/5 + PoE HAT | dwc2 on the power Type-C / GbE | 85 × 56 with fan | Raw Gadget tested on it | 8–9 endpoint limit ([PiKVM](https://docs.pikvm.org/usb/)), same backfeed, large |
| CM4/CM5 on a PoE carrier | dwc2 / GbE | carrier-sized | PiKVM precedent | dwc2 limits |
| Orange Pi Zero 3 | MUSB / GbE | none | cheap | about 4–5 endpoints ([sunxi](https://groups.google.com/g/linux-sunxi/c/iRo6w8Y0xDA)) |
| ROCK 3C / RK3568 boards | high speed; RK3568 SuperSpeed OTG (I) / GbE | 85 × 56 | path to USB 3 | larger |
| Luckfox Pico (RV1106) | USB 2 OTG (U) / 100 Mb | 50 × 50 | tiny | about 11 MB/s cap, 256 MB RAM |
| ESP32-P4 boards | HS OTG / 100 Mb | integrated | small, cheap | whole proxy firmware from scratch |
| i.MX RT1170 | 2 × HS OTG / 2 × GbE ([NXP](https://www.nxp.com/assets/block-diagram/en/i.MX-RT1170.pdf)) | custom board | real-time | large firmware effort |
| Icron ExtremeUSB / Ranger | proprietary | some PoE | works | **fails the open-protocol requirement** |

No small board with built-in gigabit PoE and a separate OTG port was
found (U).

## IR silicon

| | RK3566 `pwm-ir-tx` | ESP32 RMT | AVR Timer3 (ADK, Mega) |
|---|---|---|---|
| Carrier | hardware PWM | hardware carrier, any frequency and duty | hardware-capable, but ADK fixes 38 kHz |
| Pulse timing | mainline `pwm-rockchip` is not atomic, so the driver uses its sleeping `usleep_range` path, which logs "TX will not be accurate" (V: [pwm-ir-tx.c](https://github.com/torvalds/linux/blob/master/drivers/media/rc/pwm-ir-tx.c), [pwm-rockchip.c](https://github.com/torvalds/linux/blob/master/drivers/pwm/pwm-rockchip.c)) | hardware-timed at 1 µs ticks, independent of network load (V: [ESP-IDF RMT](https://docs.espressif.com/projects/esp-idf/en/v5.5/esp32s3/api-reference/peripherals/rmt.html)) | busy-wait in 4 µs steps, blocks the CPU for the whole frame |
| Codes | raw | raw, Pronto, loop count for repeats | NEC only |
| Learning | GPIO timestamps | RMT receive with glitch filter | NEC decoder only |
| Cost | kernel tuning; jitter under USB load likely (I) | none extra | second MCU and serial protocol; 101 × 53 mm; 5 V logic |

`gpio-ir-tx` is accurate, but it keeps interrupts disabled for a whole
frame (V: [gpio-ir-tx.c](https://github.com/torvalds/linux/blob/master/drivers/media/rc/gpio-ir-tx.c)).
That is unacceptable beside a USB proxy.

**PoE ESP32 boards (V, read 2026-11-04)**
- Olimex ESP32-POE-ISO: 3000 VDC isolation
  ([Olimex](https://www.olimex.com/Products/IoT/ESP32/ESP32-POE-ISO/)).
- M5Stack Unit PoE-P4: 64 × 24 × 20 mm, 802.3at
  ([CNX](https://www.cnx-software.com/2026/02/27/m5stack-unit-poe-p4-a-tiny-poe-powered-esp32-p4-development-kit-with-mipi-dsi-csi-and-usb-c-connectors/)).

The firmware reference targets an ESP32-S3 + W5500. Prefer an isolated
PoE variant for installed endpoints, and check flash size: the 0.2.0
partition table needs 16 MiB.

**Interoperability.** An honest subset of the Global Caché iTach API on
TCP 4998 lets existing clients drive Yoke IR endpoints:
- openHAB's Global Caché binding;
- Home Assistant's `itach` integration
  ([API v1.5](https://www.globalcache.com/files/docs/API-iTach.pdf),
  [openHAB](https://www.openhab.org/addons/bindings/globalcache/),
  [Home Assistant](https://www.home-assistant.io/integrations/itach/)).

Thermite's integration currently accepts only a genuine iTach IP2IR. It
needs a model allowance before it can drive a Yoke IR endpoint.

## Bench tests that decide

1. **Safety, unpowered then on PoE.**
   - PoE contacts to board ground (isolation).
   - HAT 5 V under a 1 A host-port load.
   - On the computer-side box, the USB current draw at boot, idle and
     proxy load.
2. **Single-board USB.**
   - Confirm `raw_gadget` exists.
   - Read the UDC endpoint budget from
     `/sys/kernel/debug/usb/fcc00000.usb/`.
   - Enumerate a low-speed keyboard, full-speed audio, high-speed storage
     and a composite webcam on Windows and macOS.
   - Confirm `480M` and measure throughput.
3. **IR timing.** Scope the ESP32 RMT carrier and frame timing under TCP
   load and during an OTA upload. Procedure in
   [IR-ENDPOINT.md](../design/IR-ENDPOINT.md).
