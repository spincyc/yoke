# ADR 0025: IR endpoint on ESP32 RMT with an iTach API subset

Date: 2026-11-04. Status: accepted; hardware validation pending.

## Context

Yoke routes IR as well as USB and HDMI. ADR 0019 added an ESP32-S3 + W5500
PoE reference firmware that brought up Ethernet and authenticated updates
but did not transmit IR. A cold review weighed three hosts for IR output:

- **Radxa Linux boxes** (`pwm-ir-tx`): on Rockchip the driver times marks
  with an inaccurate sleep path. Carrier and mark/space accuracy depend on
  scheduler load on the box that also carries USB/IP and media.
- **Arduino Mega coprocessor**: common IR libraries are effectively NEC-only
  with a fixed 38 kHz carrier, and they add a second firmware and serial link.
- **Standalone ESP32 PoE endpoint**: the RMT peripheral generates the carrier
  and the mark/space timeline in hardware, from 15 to 500 kHz.

Existing controllers, including Thermite's openHAB Global Caché binding,
already speak the Global Caché iTach TCP API (port 4998). Implementing a
small, honest subset of it lets them use a Yoke endpoint with no Yoke-specific
client.

## Decision

1. IR runs on the standalone ESP32-S3 PoE endpoint using RMT, with one TX
   channel per IR connector (Kconfig: 1-4 outputs, connectors `1:1..1:N`)
   and optional RMT RX learning. It does not run on the Radxa boxes or a
   Mega coprocessor.
2. The IR data plane is a subset of the
   [iTach API v1.5](https://www.globalcache.com/files/docs/API-iTach.pdf) on
   TCP 4998: `getdevices`, `getversion`, `sendir` (15-500 kHz, repeat 1-50,
   odd offset 1-383, durations in carrier cycles, compressed A-O pairs),
   `completeir` sent only after physical completion, `busyIR` to other
   connections, `stopir`, the smooth-repeat rule, `ERR_` codes per the
   specification, and optional `get_IRL`/`stop_IRL`. Identity is honest:
   `getversion` returns `Make=Yoke;Model=yoke-ir;Revision=<version>`, never
   an iTach model or firmware number, and there is no AMXB beacon.
3. The API is unauthenticated cleartext, as the iTach API is. It is
   therefore opt-in (Kconfig default plus NVS override, off by default),
   LAN-scoped (peers must share the Ethernet subnet unless an explicit CIDR
   allowlist is configured), rate-limited per connection, and bounded in
   line length, connections, output buffering, queue depth and frame
   duration. Learning is off by default.
4. Administration and OTA stay on the mutual-TLS HTTPS server (port 443).
   `PUT /v1/ir/config` changes the API/learning/allowlist settings;
   `GET /v1/status` reports IR state. Only the pinned operator certificate
   can enable the cleartext API.
5. IR has its own listener task, separate from and at a higher priority than
   the HTTPS server task, so a stalled TLS handshake or upload cannot block
   IR. Both tasks are watched by the task watchdog.
6. The protocol core (parser, state machine, compiler, learner) is pure C
   with no ESP-IDF dependency. Host tests compile it unchanged behind real
   localhost TCP sockets, with a virtual clock and a model of the RMT channel
   memory that runs the target encoder code.

## Consequences

- Repeats are decided by the encoder at frame boundaries, not by the RMT
  hardware loop counter: the smooth-repeat rule needs a count that can be
  reset mid-transmission. The final gap of every frame is spread over enough
  same-level symbols that the decision is taken while the previous frame is
  on the pin. A key held down therefore overshoots by at most one frame
  (`repeat + 1` frames after the last identical command).
- Everything in the ISR path is IRAM and DRAM only, so IR continues during
  OTA flash writes (`CONFIG_RMT_TX_ISR_CACHE_SAFE`). Planned restarts first
  finish the frame in flight (see ADR 0026).
- Deliberate deviations from iTach behaviour are documented in
  [IR-ENDPOINT.md](../design/IR-ENDPOINT.md): repeat values above 50 return
  `ERR_..,012` rather than being clamped, only module 1 is accepted, a
  second different command queues behind the frame in flight (queue depth
  1), and learning ends on any command from the learning connection or after
  60 seconds.
- Amends ADR 0019, whose reference application "does not implement IR
  routing": the same firmware project now is the IR endpoint. The updater
  contract of ADR 0019 is unchanged. Endpoint pairing over YWP/1 remains
  future work; this API is a local device interface, not a Yoke control
  protocol.
- Pins, emitter polarity, carrier accuracy on silicon and the learning
  sensor are bench-unverified (⚠) until the hardware procedure in
  IR-ENDPOINT.md passes.
