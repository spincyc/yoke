# USB hardware endpoint

This is the agreed USB product design. The prototype uses **two Radxa ZERO
3E boards**, one in each Yoke box. **USB 2 is the first deliverable.**

## Physical connection

```text
USB device — USB — Yoke box ⇄ Ethernet switch ⇄ Yoke box — USB — computer
                  device side                  computer side
```

The device-side box connects to the USB peripheral and acts as its USB
host. The computer-side box connects by USB to the user's computer and
presents the remote device to it. Requests and responses travel in both
directions through the boxes and the Ethernet network.

The computer is outside the two-box system. It does not run the Yoke
endpoint software. The product does not require that computer to run
Linux. Linux inside a ZERO 3E box is a firmware implementation choice.
Supported USB device types and computer compatibility still need testing;
this design is not a claim that every USB peripheral already works.

## Pairing and management

Each box runs independently. The computer-side box stores its chosen
device-side box. That pairing survives a restart or a lost connection and
changes only when a pair or unpair command changes it. An unavailable box
leaves the saved pairing in place while the system waits for it to return.

Thermite can manage the boxes, but is optional. The command-line tool or
another manager can use the documented protocol. Management and USB
network transport must use open, published protocols that others can
implement. There must be no undocumented control commands or backdoor
management paths. Closing a manager does
not clear a pairing. Configuration and status must remain documented and
available without a proprietary management service.

## Prototype scope

- Two Radxa ZERO 3E boards with one physical USB connection at each end.
  The device-side box uses its PoE HAT. Owner decision (2026-11-04): the
  computer-side box has no PoE HAT and is powered by the computer through
  the same OTG port that presents the device. Its declared draw (default
  500 mA) and its pre-enumeration draw must be measured ⚠.
- USB 2 first. Earlier USB 3 and 10GbE research does not set requirements
  for this prototype.
- Ethernet and PoE power between the boxes and a compatible switch. Preserve
  Radxa's conductive mounting screws, insulated copper pillars and GPIO
  connection; powered operation still needs physical validation.
- The [enclosure](USB-HOUSING.md) must accommodate the complete board/HAT
  assembly of the device-side box, including its cooling parts. The
  computer-side box needs no HAT. Enclosure requirements: rectangular shell, freely nesting
  halves, recessed M3 screws, heat-set inserts and unobstructed ventilation.
  R7 provides compact PoE (device-side) and bare (computer-side) first
  articles sharing one base; physical fit and powered cooling remain NOT
  RUN. R6 was superseded as too large; R5 is retired.

## Firmware status

The pairing software, stored state, monitoring and the Linux USB/IP adapter
exist. The computer-side data path now exists in software. It is
[documented in USB-DATAPATH.md](USB-DATAPATH.md) and covered by ADRs 0021
and 0022:

- A separate worker imports the remote device with the open USB/IP protocol,
  directly from the device-side box's stock `usbipd`.
- It presents the device through the box's OTG port with Linux Raw Gadget.
- The agent's `usb-gadget` driver routes it with metadata only.

It is verified only in a pure-software simulation. That simulation
covers keyboard, mouse, mass storage, serial (CDC-ACM), composite,
full-speed-only, stalling and disconnecting device models, with Linux-,
Windows- and macOS-style enumeration. **No hardware has been tested.**
Raw Gadget availability in the Radxa kernel, DWC3 device-mode behavior,
power draw and real-computer compatibility remain open bench items (⚠).

The first hardware test must cover the complete path above, including the
second box and its USB cable to a separate computer. A successful Linux
USB/IP import or a passing simulation is a backend test, not completion of
that test. Do not replace the computer-side box with a Linux application
host in product documentation or drawings.

The [backend reference](../../site/pages/reference/backends.md#usbip)
describes the existing adapter. The older
[10G/USB3 study](EDGE-DEVICES.md) and [hardware survey](../research/HARDWARE.md)
remain historical research; their NanoPi/VirtualHere recommendations do
not replace the selected ZERO 3E design.
