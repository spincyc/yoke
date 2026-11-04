# ESP32 Ethernet firmware updates

Status: normative updater API; software implementation and compilation are
verified separately from the **NOT RUN** hardware acceptance checks below.
See [ADR 0019](../decisions/0019-esp32-local-ethernet-firmware-updates.md).

`firmware/esp32` is the ESP-IDF IR endpoint for the Waveshare ESP32-S3-ETH
with its PoE module (ESP32-S3-POE-ETH, SKU 28771; verify the purchased board
matches this model). It contains a reusable `yoke_ota` component (this
document) and the `yoke_ir` component: RMT IR output behind an opt-in iTach
API subset, specified in [IR-ENDPOINT.md](IR-ENDPOINT.md) and
[ADR 0025](../decisions/0025-esp32-rmt-ir-endpoint-itach-subset.md).
Health, recovery, storage and watchdog behaviour follow
[ADR 0026](../decisions/0026-esp32-endpoint-health-recovery-and-storage.md).
YWP/1 pairing is not implemented by this firmware. The Python agent remains
a separate Linux program using only the standard library.

No manager, Internet connection, broker, discovery, pairing lease, external
CA, or NTP service is required. A locally chosen DHCP server is the default
address source; a static IPv4 configuration is also available in Kconfig.
The serial log prints the address. No network scanning or automatic trust
enrollment occurs. Third-party programs have exactly the same HTTPS API as
curl or any management UI.

## Trust and storage

USB commissioning writes the `otatrust` NVS partition with a unique server
private key, self-signed server certificate, the exact self-signed operator
client certificate authorized for this device, and a local Unix-time floor.
Private keys are generated locally using OpenSSL, never committed or baked
into an application image. The client key stays on the operator machine.
Missing or malformed trust fails closed; firmware never erases or creates
trust automatically. TLS requires the client certificate; the client must
verify the provisioned server certificate and hostname/IP SAN. No insecure
HTTP listener is created and verification must not be disabled.

The physical provisioning operation is the authority boundary. The pinned
operator certificate is full firmware-administrator authority, not an IR
pairing credential. Rotation/revocation requires USB reprovisioning in this
version. Protect trust images as secrets and retain a backup outside this
disposable workspace. TLS uses the provisioned local time floor, which
advances with uptime and is saved hourly, before every planned restart, at
boot, at health confirmation and on upload. Time spent powered off
is unknowable without an RTC; expiration is therefore not a strong offline
revocation mechanism. `clock_source: provisioned-floor` exposes this limit.
Use a correct local clock when provisioning; certificate failures require
physical recovery, never an unauthenticated network clock-setting API.

OTA changes only `ota_0`/`ota_1`, `otadata`, and the updater's bounded status
journal and boot counter in `otatrust`. `nvs` is reserved for application
configuration, identity, pairing and IR settings (`yoke_ir` namespace) and is
never erased by the updater. Application schema changes must remain readable
by both slots during rollback; this updater does not migrate schemas.

The journal is evidence, not trust. It is stored as an explicit versioned
little-endian record under the key `journal2`, with a compatibility floor so
newer firmware can append fields. An unknown or corrupt record is discarded
with a status note (`error`) and never fails boot. The unversioned `journal`
record of firmware 0.1.0 is read once for migration and never rewritten, so
a rollback to 0.1.0 still finds the record it wrote.

The 16 MiB layout, fixed at USB commissioning:

| Partition | Offset | Size | Use |
|---|---|---|---|
| `nvs` | 0x9000 | 64 KiB | application settings, identity, IR settings |
| `otatrust` | 0x19000 | 64 KiB | trust, clock floor, journal, boot count |
| `otadata`, `phy_init` | 0x29000 | 12 KiB | boot selection, PHY data |
| `ota_0`, `ota_1` | 0x30000, 0x630000 | 6 MiB each | app slots |
| `coredump` | 0xc30000 | 256 KiB | panic/watchdog core dump (ELF) |
| `ircodes` | 0xc70000 | 1 MiB | NVS reserved for stored IR codes |
| `spare` | 0xd70000 | 2.56 MiB | raw reserve for future firmware |

Firmware upload is limited to the inactive slot size. Bootloader,
partition-table and NVS uploads are rejected as app images. Network updates
cannot change partition geometry; the 0.1.0 layout (no core dump or IR
storage) must be reflashed over USB, and no device has been commissioned
with it.

## API contract

All paths require TLS client authentication. Unknown methods/paths fail.
Request bodies use an exact positive Content-Length (no chunked uploads).
Only one upload is processed at a time. Body reception has an independent
120-second absolute watchdog which shuts down its socket even while TLS is
blocked inside a partial record. A 5-second socket idle timeout also applies;
it alone would not bound a slowly dribbled TLS record. Hash/image validation
after body reception adds bounded local processing time. Failed/incomplete uploads
abort the inactive slot and leave the selected boot image unchanged.

The original decimal Content-Length header is checked against the slot
limit and parsed length. This prevents ESP-IDF's 64-bit parser length from
being silently narrowed to a smaller 32-bit target length.

TLS handshakes have a 10-second absolute deadline. The pinned ESP-IDF v5.5.6
server wraps a blocking TLS call with a timeout that cannot interrupt a
dribbled record. The project therefore generates a build-local replacement
of that one SDK function: a nonblocking handshake loop with a monotonic
deadline, a scheduler yield, and restoration of the original socket flags.
`prepare_tls.py` checks the entire upstream source's SHA-256 before creating
the override; changing SDK source fails configuration until reviewed. The
SDK checkout and HTTPD's TLS transport hooks remain unchanged. The same
`handshake_deadline.h` code is tested against real mbedTLS on loopback.

The wired-only reference app enables `bootloader_random_enable()` before
any certificate/key validation or TLS operation, and retains that SAR ADC
entropy source for all TLS seeds/reseeds. It owns the ADC for its full
lifetime. An application integrating the reusable updater must establish
and retain an Espressif-supported true entropy source before
`yoke_ota_init()` and throughout server operation; ADC sampling cannot run
concurrently with the reference entropy setup. Do not silently disable the
source when adding IR analog input. See Espressif's
[random-number guidance](https://docs.espressif.com/projects/esp-idf/en/v5.5.6/esp32s3/api-reference/system/random.html).

| Request | Result |
|---|---|
| `GET /v1/status` | JSON protocol version, running app/version/slot, selected slot, image limits, state, last error, last attempted digest/version, local clock source, `journal_version`, `reset_reason`, `uptime_s`, `boot_count`, `heap_free`, `heap_min_free` (low-water mark), `coredump_present`, and an `ir` object (see IR-ENDPOINT.md) |
| `PUT /v1/firmware`, `Content-Type: application/octet-stream`, `X-Image-SHA256: <64 lowercase hex>` | Stream raw `yoke-ir-updater.bin`; require complete digest match, valid ESP32-S3 app, matching project name; stage inactive slot, select boot slot, reply 202, restart once IR output is idle |
| `DELETE /v1/coredump` | Erase a stored core dump after retrieving it over USB (`idf.py coredump-info`); reply with `coredump_present` |
| `PUT /v1/ir/config`, `Content-Type: application/json` | IR API/learning/allowlist settings; see IR-ENDPOINT.md |

Successful 202 means `pending-reboot`, not application health. A lost response
has an unknown outcome: reconnect and inspect status/version before retrying.
Reboot is scheduled two seconds after accepting a valid image, even if the
response is lost. It first lets the IR frame in flight finish, refuses new
`sendir` with `busyIR` and waits, bounded by the maximum frame plus 0.5 s.
The image is erased and written sector by sector, so no single multi-second
erase stalls the server task. The RMT ISR and encoder are cache-safe (IRAM),
so IR output continues while flash is written. HTTP 400 means invalid metadata, 409 means unconfirmed current image
or update already pending, 413 oversized, 415 wrong content type, and 500
means validation/storage/receive error; bounded JSON `error` gives detail.
Errors close the connection so unread bodies cannot become later requests.
There is no arbitrary URL fetch, command execution, or remote health-confirm
endpoint. A digest checks transport/content intent; the authenticated client
authorizes installation. No separate publisher signature is claimed.

States are `idle`, `receiving`, `verifying`, `pending-reboot`,
`pending-verify`, `healthy`, `rolled-back`, and `error`. A durable journal
records selected image digest/version and target slot before boot selection;
after restart, running/target slot and ESP-IDF image state distinguish a
pending verification from rollback. Errors retain the previous good image.
Status and serial logs show versions and errors. Upload processing is
synchronous, so a second status request waits for upload completion; transient
progress states are visible in serial logs. This is not a progress stream.

## Application integration and rollback

Call `yoke_ota_init()` early in boot, then start the HTTPS server with
`yoke_ota_start()` after network setup. On `ESP_OTA_IMG_PENDING_VERIFY`,
the component starts a 60-second health deadline independently of network
availability. Do not call confirmation merely because `app_main` ran. Fatal
boot errors call `yoke_ota_fail_health()`, which rolls back a pending image
and leaves a confirmed one running; otherwise the deadline reboots into the
last confirmed image. A reset or crash before confirmation also triggers
ESP-IDF bootloader rollback. A confirmed image is not automatically rolled
back for a later runtime fault. `yoke_ota_set_restart_guard()` registers the
application's pre-restart hook (IR quiesce); `yoke_ota_set_status_hook()`
adds status fields; `yoke_ota_server()` lets the application register more
mutual-TLS handlers; `yoke_ota_save_clock_floor()` is called hourly.

The endpoint confirms a pending image only when all of the following are up:
trust, storage, the W5500 driver, the HTTPS listener, the IR self-test, and
the IR listener (or a disabled IR API). In addition, either an IPv4 address
was acquired, or the link was observed down. Observed down means an
explicit link-down event, or no link-up within 10 s of driver start. A
cable-less bench boot therefore confirms, and a broken network stack on a
cabled network rolls back. The rule is the pure function
`yoke_ota_health_decide()`, which is host-tested.

Recovery: Ethernet start is retried with 1, 2 and 4 s backoff. A confirmed
image then restarts, with a delay that doubles across consecutive failed
boots (kept in RTC memory) up to five minutes. Trust or storage failures
fail closed: no network services and no restart loop. A pending image rolls
back instead.

The task watchdog panics after 15 s and leaves an ELF core dump in the
`coredump` partition. It watches the idle tasks, `app_main` and the IR
listener task. The HTTPS server task blocks in `select()`, so an esp_timer
probe queues work to it every 2 s, and that work feeds a watchdog user only
when the task actually runs it. At most one probe is queued, so session-close
requests always fit in HTTPD's small control queue. Uploads feed the user
once per received chunk. The TLS handshake wait loop also feeds it, through
a weak hook in the generated ESP-TLS override: each handshake is bounded by
its own 10 s deadline, so an unauthenticated peer stalling handshakes back
to back cannot starve the watchdog. Rejected requests close the connection
instead of draining an unread body at the peer's pace. A panic first parks
the IR pins (see IR-ENDPOINT.md).

## Primary sources

The [Waveshare product documentation](https://docs.waveshare.com/ESP32-S3-ETH)
describes the 16 MiB flash, W5500 Ethernet and optional PoE module. The
[vendor Ethernet examples](https://docs.waveshare.com/ESP32-S3-ETH/Development-Environment-Setup-Arduino)
give MOSI 11, MISO 12, SCLK 13, CS 14, INT 10 and RESET 9. These are isolated
in Kconfig and **bench-unverified here**; other similarly named Waveshare
boards use different pins. The implementation uses Espressif's
[HTTPS server](https://docs.espressif.com/projects/esp-idf/en/v5.5.6/esp32s3/api-reference/protocols/esp_https_server.html)
and [OTA/rollback APIs](https://docs.espressif.com/projects/esp-idf/en/v5.5.6/esp32s3/api-reference/system/ota.html).

## Boot security path

Secure Boot v2 and flash encryption remain deferred and no eFuse is burned.
The intended order, with an ADR per irreversible step, is in
[ADR 0026](../decisions/0026-esp32-endpoint-health-recovery-and-storage.md):
freeze this layout, add an offline Secure Boot v2 signing key and signed
release images, enable release-mode flash encryption with encrypted NVS for
`otatrust`, and only then enable secure boot and the anti-rollback counter.

## Hardware acceptance — NOT RUN

Compile and host tests cannot prove Ethernet wiring, PoE supply behavior or
flash recovery. IR-specific checks are in [IR-ENDPOINT.md](IR-ENDPOINT.md).
Bench-test:

1. USB first flash and unique trust provisioning; missing/wrong certificate
   rejection, server identity rejection, valid authenticated status/upload.
2. Successful version change over W5500/PoE, confirmation after real local
   health checks, and preservation of identity/configuration/pairing bytes.
3. Truncated upload, wrong digest, wrong chip/project, oversize and power
   interruption during write: previous image still boots.
4. New image crash, timer timeout, and power interruption before health
   confirmation: last confirmed image boots and status shows rollback.
5. Unplugged Ethernet/absent manager: a pending image confirms after the
   10 s link window; with a cable and no DHCP it rolls back at 60 s; normal
   operation does not acquire a pairing lease.
6. USB recovery with both app slots damaged, and operator certificate
   rotation without erasing application NVS. Never run `erase-flash` as a
   normal update step.
7. Ethernet failure on a confirmed image (hold W5500 in reset): retries,
   restart backoff growing to five minutes, recovery when released.
8. Task watchdog: a wedged task panics, the core dump is present in status
   and retrievable over USB, and `DELETE /v1/coredump` clears it.
9. Rollback from 0.2.0 to 0.1.0 finds its own `journal` record intact (only
   on a bench unit flashed with the 0.2.0 layout).
