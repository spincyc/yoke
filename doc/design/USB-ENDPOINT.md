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

- Two Radxa ZERO 3E boards, with one physical USB connection at each end.
- USB 2 first. Earlier USB 3 and 10GbE research does not set requirements
  for this prototype.
- Ethernet between the boxes. The bare board does not supply a completed
  PoE product; power wiring and any PoE adapter need separate validation.
- The [enclosure](USB-HOUSING.md) is being redesigned after two failed
  prints: rectangular shell, slot joint, recessed M3 screws and heat-set inserts.

## Firmware status

The pairing software, stored state, monitoring and Linux USB/IP adapter
exist. The current USB/IP adapter can export devices and import them into
a Linux kernel. **That adapter alone does not complete this hardware
product.**

The computer-side box still needs the USB device/gadget bridge that
presents the remote peripheral through its physical USB port. That work,
USB device compatibility, recovery and end-to-end hardware testing remain
unfinished. Do not replace the computer-side box with a Linux application
host in product documentation or drawings to match the current adapter.

The first hardware test must cover the complete path above, including the
second box and its USB cable to a separate computer. A successful Linux
USB/IP import is a backend test, not completion of that test.

The [backend reference](../../site/pages/reference/backends.md#usbip)
describes the existing adapter. The older
[10G/USB3 study](EDGE-DEVICES.md) and [hardware survey](../research/HARDWARE.md)
remain historical research; their NanoPi/VirtualHere recommendations do
not replace the selected ZERO 3E design.
