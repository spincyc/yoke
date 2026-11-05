# ADR 0030: USB box firmware on ESP-IDF (tiers 1 and 1.5)

Date: 2026-11-05. Status: accepted; hardware validation pending.

## Context

ADR 0027 puts tier 1 (ESP32-S3 + W5500) and tier 1.5 (ESP32-P4, internal
EMAC) on one C firmware with no OS, carrying the portable native cores
(`native/yusb`, `native/ywp`). The IR endpoint (ADRs 0019, 0025, 0026)
already solved updates, health, recovery and the watchdog on ESP-IDF, but
in one application. Three questions needed an answer before code:

- how the second application shares that work without copying it;
- how a stock USB device stack can present an arbitrary remote device's
  descriptors and relay its control requests;
- how far the USB Host Library matches the host-controller model of the
  device-side export core.

## Decision

1. **Shared components, two applications.** `yoke_ota` (moved unchanged
   apart from three small public helpers), `yoke_eth` (W5500 or P4 EMAC,
   board pin profiles in Kconfig ⚠) and `yoke_node` (the ADR 0026 boot
   lifecycle) live in `firmware/components`. `firmware/esp32` (IR) and
   `firmware/esp32-usb` (USB box) both use them; the IR image keeps its
   Kconfig symbols, values, partitions and tests. The health input
   `ir_ok` becomes `app_ok`.
2. **One USB role per boot.** The role (device-side: USB host;
   computer-side: USB device) comes from Kconfig, is overridden by NVS and
   is changed over the mutual-TLS admin API (`PUT /v1/usb/config`), which
   restarts the box. Both roles use the same DWC2 controller and PHY.
3. **Computer role: esp_tinyusb with an application class driver.**
   esp_tinyusb 3.0.0 (TinyUSB 0.21.0 bundled) is pinned by hash in a
   per-target lock file. One class driver claims the whole configuration,
   allocates a FIFO per endpoint address and relays every request TinyUSB
   lets through, holding the host off (NAK) until the reply. The device,
   configuration and string callbacks of esp_tinyusb serve one static
   configuration with short ASCII strings, so the link wraps them
   (`--wrap`, as the IR panic hook does) to serve the remote bytes
   verbatim. The backend exposes the Raw Gadget operation model the
   presentation core already uses. What TinyUSB cannot relay is recorded,
   not worked around: standard device requests are answered locally from
   the cached descriptors, SET_CONFIGURATION is acknowledged before the
   remote sees it, and a STALL after OUT data is late (as with Raw Gadget).
   A patched `usbd.c` could defer that status too; it was rejected for now
   because a maintained SDK patch costs more than the one late-STALL class
   it would fix.
4. **Device role: the USB Host Library.** One device on the root port, no
   hubs. Its limits stay visible in the backend contract: cancelling a
   transfer flushes its endpoint, IN transfers are whole packets, the
   library chooses the configuration.
5. **Hardware self-tests ship with the backends** so each box verifies its
   wiring without the network data path: a CDC-ACM echo presented through
   the mirror driver (with deliberately deferred control replies) and an
   enumeration logger with report polling.
6. **Tier 1.5 builds from the same source**: the P4's high-speed OTG port
   and EMAC, build-verified only until hardware exists.
7. **Configured means bridging.** A box with a YWP/1 node id and a site key
   (at least 32 bytes, used byte for byte like the agents' PSK file) or an
   explicit open mode runs the USB/IP bridge: the device box as a producer
   with ADR 0024 admission (USB/IP server on TCP 3240), the computer box as
   a consumer importing the paired producer's device. Without them it runs
   its self-test, so no extra mode switch exists. A corrupt saved pairing
   state stops pairing until the explicit `rebase_sequence` recovery.
8. **One task per core.** Each sans-I/O core runs in exactly one task: the
   USB/IP core in its backend's task (TinyUSB's, or the USB host client
   task), which also moves its nonblocking sockets; the YWP/1 node in a
   network task that only reports socket readiness to the USB task. No core
   is called under a lock from two tasks, and no backend is called outside
   its task.
9. **A real wall clock before pairing.** YWP/1 sequences are Unix time, and
   the provisioned clock floor loses a box's off-time, so a bridging box
   runs SNTP (DHCP option 42 and a configurable server) or accepts a
   forward-only reading over the admin API, and creates its node, or
   rebases, only once the clock is synchronized. Synchronization survives
   software restarts in RTC memory; SNTP is unauthenticated (a LAN trust
   limit, as ADR 0024's address admission).
10. **PSRAM on the S3.** The tier 1 module's octal PSRAM takes malloc's
    large allocations (TLS buffers, the YWP/1 node, the presentation
    core's state), so internal RAM stays for USB/IP sessions and DMA. A
    board without PSRAM still boots on internal RAM, with the margins of
    USB-TIER1.md.

## Consequences

- The firmware gate (`firmware/verify.py`, `make verify-firmware`) builds
  three images and runs `tests/firmware_usb`; CI time grows accordingly.
- The esp_tinyusb pin and the TinyUSB version check must be reviewed
  together on any upgrade: the relay depends on usbd.c's request copy,
  control stages and the DWC2 ISO allocate/activate behaviour.
- Late STALLs (OUT data, SET_CONFIGURATION) and uncached string requests
  differ from a directly attached device; the presentation core already
  counts the first and caches the others at import.
- The ⚠ items (pins, VBUS from the 5V pin, DWC2 limits, P4 port roles and
  silicon revision) are listed with a bench procedure in
  [USB-TIER1.md](../design/USB-TIER1.md).
- The native cores must fit an S3's internal RAM; their bounds are sized
  for it at build time (and, for native/yusb, from policy at create time).
  The device box serves one import at a time.
- TinyUSB 0.21 runs the P4's high-speed PHY at high speed only, so a
  tier 1.5 computer box presents high-speed devices; low- and full-speed
  devices need a tier 1 computer box.
- A box on a network with neither NTP nor operator access does not pair.
