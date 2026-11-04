# ADR 0026: ESP32 endpoint health, recovery, storage and boot-security path

Date: 2026-11-04. Status: accepted; hardware validation pending.

## Context

The 2026-11-04 cold review of the ADR 0019 updater found defects that matter
once the firmware becomes an IR endpoint:

1. Health was confirmed right after the HTTPS listener started, before link
   or IP, so a broken network stack could become the rollback baseline.
2. A confirmed image that failed to bring up Ethernet sat idle forever.
3. The status journal had no version; any size change failed boot.
4. The partition table had no core dump or IR storage, and geometry cannot
   change after commissioning.
5. The task watchdog did not panic and the server task was not watched.
6. Status lacked reset reason, uptime, boot count, heap low-water mark and
   core dump presence.
7. The clock floor was documented as advancing with uptime but was saved
   only at boot, confirmation and upload.
8. Flash writes during OTA disable the cache under the RMT ISR, and the
   device rebooted two seconds after replying 202 regardless of IR activity.
9. Device-key protection and secure boot were deferred without a recorded
   path.

## Decision

- **Health gate.** A pending image confirms only when trust, storage, the
  W5500 driver, the HTTPS listener, the IR self-test and the IR listener
  (or a disabled IR API) are all up, and either an IPv4 address was acquired
  or the link was observed down (an explicit link-down event, or no link-up
  within 10 s of driver start). A cable-less bench boot therefore cannot be
  bricked by rollback, and a broken network stack on a cabled network rolls
  back. A local failure rolls back immediately. The decision is the pure
  function `yoke_ota_health_decide()`.
- **Recovery.** Ethernet start is retried with backoff inside a boot. A
  partial start whose driver cannot be uninstalled is not retried in place
  and leads to a restart instead. After the retries, a confirmed image
  restarts with a backoff that grows across restarts
  (RTC memory, capped at five minutes). Trust or storage failures still fail
  closed: no network services and no restart loop; a pending image rolls
  back.
- **Journal.** The record is evidence, not trust. It is an explicit
  little-endian layout under the NVS key `journal2` with a version and a
  compatibility floor. Unknown or corrupt records are discarded with a
  status note and never fail boot. Version 0.1.0's `journal` record is read
  once for migration and never rewritten, so a rollback to 0.1.0 still finds
  the record it wrote.
- **Partitions** (16 MiB, fixed at USB commissioning): existing offsets are
  unchanged; add `coredump` (256 KiB), `ircodes` (1 MiB NVS, reserved for
  stored IR codes) and `spare` (2.56 MiB raw, reserved), filling the flash.
- **Watchdog.** The task watchdog panics after 15 s, which leaves a core dump
  in flash. The idle tasks, `app_main` and the IR listener task are
  subscribed. The HTTPS server task blocks in `select()`, so a probe queued
  to it every 2 s (at most one outstanding) feeds a watchdog user only when
  that task actually runs. The deadline-bounded TLS handshake loop and each
  upload chunk also feed it. Uploads erase flash sector by sector, and
  rejected requests close without draining their body.
- **Status** adds `reset_reason`, `uptime_s`, `boot_count`, `heap_free`,
  `heap_min_free`, `coredump_present`, `journal_version` and an `ir` object.
  `DELETE /v1/coredump` acknowledges a dump after it has been retrieved over
  USB.
- **Clock floor** is saved every hour, before planned restarts, at boot, at
  confirmation and on upload, so the documentation's claim is true.
- **OTA and IR.** The RMT TX/RX ISRs, the encoder and the callbacks are
  cache-safe (IRAM, DRAM data, inline atomics), so IR continues during flash
  writes. Every planned restart and every rollback first calls a restart
  guard that finishes the IR frame in flight, refuses new `sendir`, waits for
  idle (bounded to the maximum frame plus 0.5 s) and only then forces the
  outputs idle. An unplanned panic detaches the IR pins from RMT before the
  panic handler runs, so a core dump cannot be accompanied by replayed IR.
- **Boot security path (decision note).** Flash encryption and Secure Boot
  v2 stay deferred; no eFuse is burned by this firmware or its tooling. The
  intended order is: (1) freeze the partition layout above and confirm the
  bench procedure; (2) generate an offline RSA-3072 Secure Boot v2 signing
  key outside the repository and sign app images in release builds, keeping
  the mutual-TLS upload as the authorization step; (3) enable flash
  encryption in release mode together with encrypted NVS (HMAC- or
  flash-encryption-based NVS keys) so the device key in `otatrust` is not
  readable from a desoldered flash; (4) enable Secure Boot v2 and the
  anti-rollback eFuse counter only after a recovery procedure for bricked
  units is written. Each step is irreversible per device and needs its own
  ADR before it ships.

## Consequences

- Devices running the 0.1.0 layout must be reflashed over USB once. No
  device has been commissioned, so no field migration exists.
- A new image confirms later than before (after IP or a 10 s link window).
  The 60 s rollback deadline is unchanged.
- The journal format can grow without breaking older readers that share the
  compatibility floor.
- Watchdog panics are visible as `reset_reason: task-watchdog` and
  `coredump_present: true` over the authenticated status API.
- Hardware validation is still required; see the bench procedures in
  [ESP32-UPDATES.md](../design/ESP32-UPDATES.md) and
  [IR-ENDPOINT.md](../design/IR-ENDPOINT.md).
