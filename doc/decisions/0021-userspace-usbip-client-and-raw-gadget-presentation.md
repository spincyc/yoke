# 0021 — Userspace USB/IP client with gadget presentation

Date: 2026-11-04. Status: accepted for the USB 2 prototype; hardware
behavior is bench-unverified (⚠).

## Context

The computer-side ZERO 3E box must appear to the user's computer as the
remote USB device, through its own OTG port (DWC3 at `fcc00000`, high-speed
capable). The existing USB/IP consumer (ADR 0014) imports the device into the
box's own kernel through `vhci-hcd`, so the peripheral enumerates inside the
box and the computer sees nothing. Owner decision (2026-11-04): this box is
powered by the computer through the same OTG port; there is no PoE HAT on
the computer side.

## Decision

1. **Wire: documented USB/IP 1.1.1.** A userspace client in the worker
   speaks [USB/IP](https://docs.kernel.org/usb/usbip_protocol.html):
   OP_REQ_IMPORT, CMD_SUBMIT/RET_SUBMIT and CMD_UNLINK/RET_UNLINK. It talks
   directly to the stock device-side `usbipd`/`usbip-host`. No `vhci`
   import happens, so the computer-side kernel never binds the device. The
   transport stays open and interoperable. The client honors the stock
   stub's requirements: correct `devid`, endpoints of the current
   configuration only, a positive interval for interrupt URBs (from
   `usb_fill_int_urb` rules), and the documented `0xffffffff`
   `number_of_packets` for non-ISO URBs.
2. **Presentation: a backend interface, Raw Gadget first.** `GadgetBackend`
   (`yoke.usb.gadget`) abstracts events, endpoint zero, endpoint
   enable/disable/I/O, halt, CONFIGURE, VBUS draw and cancellation.
   The first backend drives Linux
   [Raw Gadget](https://docs.kernel.org/usb/raw-gadget.html) through stdlib
   `fcntl.ioctl`, so the stdlib-only runtime is preserved (ADR 0008).
   Constants and layouts are copied from `include/uapi/linux/usb/raw_gadget.h`;
   tests check them against values compiled from that header.
   - The kernel calls Raw Gadget a debugging feature. Its availability and
     behavior in the Radxa kernel are isolated in `yoke.usb.rawgadget`
     and marked ⚠.
   - GadgetFS or a native worker can replace it behind the same interface.
3. **Exact USB semantics before throughput.**
   - Descriptors are fetched once at import and served from a cache, so
     standard GET_DESCRIPTOR requests meet the USB 2.0 §9.2.6.4 deadlines
     without a network round trip. Unknown descriptors are forwarded and
     their answers, including STALL, are cached.
   - SET_CONFIGURATION and SET_INTERFACE quiesce affected pumps, are
     forwarded, then enable local endpoints and call CONFIGURE.
   - SET_ADDRESS and device/endpoint status are local.
   - Class and vendor requests are forwarded. A short control reply ending
     on a packet boundary carries a zero-length packet.
   - Without a size hint a pump moves one (high-bandwidth) packet per URB.
     A bulk transfer boundary on a packet multiple is otherwise invisible to
     the receiving side.
   - IN pumps keep several URBs outstanding and coalesce full completions
     into one gadget write; the UDC re-packetizes them identically.
   - Bulk-Only Transport (BOT) interfaces take exact transfer sizes from
     each CBW. This is the only payload inspection, and it stays inside
     the worker.
   - A remote STALL is reproduced locally: write any partial data, unlink
     later URBs, halt the endpoint, then clear the remote halt. The
     computer's own CLEAR_FEATURE is answered by the UDC.
   - OUT packets are written through one URB at a time by default. The UDC
     has already acknowledged a packet when the device refuses it, so the
     computer sees that STALL on its next OUT transaction. A larger window
     pipelines OUT but delays the STALL by up to the window.
   - The worker connects over IPv4 from the computer-side box's announced
     control-VLAN address, which the producer's data-plane admission
     requires (lane/ctl).
4. **Speeds and power.**
   - Full-speed devices are presented at full speed and high-speed at high
     speed. Low-speed devices are presented at full speed: their
     control/interrupt descriptors are already valid there, so only
     `bMaxPacketSize0` changes to the UDC's 64.
   - Configurations declare bus power, no remote wakeup (Raw Gadget has no
     wakeup ioctl), and the box's own draw. The default is 500 mA ⚠ until
     measured.
   - The BOS LPM bit is cleared, since link power management belongs to
     the local UDC.
5. **Explicit rejection.** The worker reports a clear rejection reason
   instead of presenting a device it cannot reproduce:
   - super-speed devices;
   - hubs;
   - isochronous or extra control endpoints;
   - low-speed bulk;
   - endpoint counts or numbers beyond the configured UDC budget, refined
     by EPS_INFO;
   - a computer link slower than the device.
6. **Bus events.**
   - Bus reset and disconnect stop pumps, unlink URBs and disable endpoints.
   - A remote reset is requested through the stub's port-reset request.
     This happens once per dirty period and runs in the background, so
     cached enumeration is not delayed.
   - Suspend and resume are tracked.
   - Remote loss ends the session; the owner closes the gadget, so the
     computer sees a disconnect, then retries the import.
7. **One device per gadget.** A composite device is presented whole. Hubs
   and multiple unrelated devices are future work.

## Consequences

- Any computer works without Yoke software, because it sees an ordinary
  USB device. Compatibility still needs per-device testing.
- Control OUT requests with data are acknowledged when their data arrives
  (UDC status-stage behavior). A remote STALL of such a request can only
  be counted and logged (`late_out_stalls`).
- Packet-per-URB forwarding is correct for every bulk protocol but is
  bounded by per-packet overhead. Class-specific sizing (BOT today) or a
  native worker removes that limit.
- Per-transfer deadlines are not configurable in the computer's host
  stack. A forwarded standard no-data request (SET_CONFIGURATION,
  SET_INTERFACE) must complete within 50 ms, which bounds usable round-trip
  time for those requests. The simulation enforces and reports it.

## Alternatives considered

- **`vhci` import plus a libusb-to-GadgetFS proxy.** This is the
  [USBProxy](https://github.com/nesto-software/USBProxy) model. It binds
  the device to the box's kernel first, adds a second USB stack hop, and
  needs native libusb, which breaks the stdlib runtime. Rejected.
- **A fixed HID gadget (configfs).** Robust for keyboards and mice, but
  not generic. It remains a possible separate backend.
- **A Yoke-specific transport.** Rejected: management and USB transport
  must stay open and interoperable (`doc/design/USB-ENDPOINT.md`).

## Amendment (2026-11-04, cold review)

An independent review found two hardware-blocking defects that the first
simulation encoded as assumptions. The code was corrected, and the
simulation now models the kernel behavior. A real-kernel loopback test
(`make verify-usb-kernel`: QEMU/KVM, dummy_hcd, usbip-host, Raw Gadget)
exercises the production worker.

- **Gadget name.** `usb_raw_init.driver_name` is compared with
  `usb_gadget.name` (`dwc3-gadget`, `dummy_udc`), not the UDC device name;
  RUN otherwise fails with EBUSY. The name is inferred from the UDC's
  platform driver, with an override.
- **Cancellation.** CPython's `fcntl.ioctl` retries EINTR when no Python
  handler raised, and handlers never run off the main thread, so SIGUSR1
  could not cancel a blocked ioctl and teardown could not be confirmed.
  - Raw Gadget ioctls now go through ctypes (stdlib, GIL released, called
    exactly once), and the dispatch never retries.
  - The VM test's `fcntl` negative control reproduces the failure.
- **Host clear-halt.** dwc3 returns started requests with -EPIPE when the
  host clears an endpoint's halt. The pump forwards the clear to the device
  and retries the I/O.
- **Remote stall.** The remote halt is cleared once per local halt. An IN
  endpoint that stalls again is parked until host activity, or a backoff of
  up to 2 s, so it never spins.
- **Unlink ordering.** RET_UNLINK may precede a late RET_SUBMIT (stub_tx
  order) or carry the URB's own status. The client keeps tombstones and
  never drops the connection for it.
- **Control direction.** An IN request with wLength 0 travels as an OUT URB,
  as Linux expects.
- **BOT sizing.** A CBW is parsed only when no data phase is planned.
