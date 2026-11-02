# ESP32 Ethernet firmware updates

Status: normative updater API; software implementation and compilation are
verified separately from the **NOT RUN** hardware acceptance checks below.
See [ADR 0019](../decisions/0019-esp32-local-ethernet-firmware-updates.md).

`firmware/esp32` is an ESP-IDF reference app for the Waveshare ESP32-S3-ETH
with its PoE module (ESP32-S3-POE-ETH, SKU 28771; verify the purchased board
matches this model). It contains a reusable `yoke_ota` component. It brings
up wired Ethernet and a firmware updater only. **IR receive/transmit,
routing, and YWP/1 are not implemented by this app.** The Python agent
remains a separate Linux program using only the standard library.

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
disposable workspace. TLS uses the provisioned local time floor, advances
with uptime, and saves it before restart and on boot. Time spent powered off
is unknowable without an RTC; expiration is therefore not a strong offline
revocation mechanism. `clock_source: provisioned-floor` exposes this limit.
Use a correct local clock when provisioning; certificate failures require
physical recovery, never an unauthenticated network clock-setting API.

OTA changes only `ota_0`/`ota_1`, `otadata`, and the updater's bounded status
journal in `otatrust`. `nvs` is reserved for application configuration,
identity and pairing and is never erased by the updater. Application schema
changes must remain readable by both slots during rollback; this updater
does not migrate schemas. The 16 MiB layout reserves two 6 MiB app slots,
64 KiB application NVS, and 64 KiB trust NVS; firmware upload is limited to
the inactive slot size. Bootloader/partition-table/NVS uploads are rejected
as app images. Network updates cannot change partition geometry.

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
| `GET /v1/status` | JSON protocol version, running app/version/slot, selected slot, image limits, state, last error, last attempted digest/version, local clock source |
| `PUT /v1/firmware`, `Content-Type: application/octet-stream`, `X-Image-SHA256: <64 lowercase hex>` | Stream raw `yoke-ir-updater.bin`; require complete digest match, valid ESP32-S3 app, matching project name; stage inactive slot, select boot slot, reply 202, restart |

Successful 202 means `pending-reboot`, not application health. A lost response
has an unknown outcome: reconnect and inspect status/version before retrying.
Reboot is scheduled after accepting a valid image even if the response is
lost. HTTP 400 means invalid metadata, 409 means unconfirmed current image
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
availability. Call `yoke_ota_confirm_health()` only after loading existing
configuration/identity/pairing, initializing application workers and local
IR hardware, and passing local self-tests. A missing peer or unplugged
Ethernet cable must not prevent a locally healthy application passing.
Do not call confirmation merely because `app_main` ran. Fatal boot errors
may call `yoke_ota_fail_health()`; otherwise timeout reboots into the last
confirmed image. A reset/crash before confirmation also triggers ESP-IDF
bootloader rollback. A confirmed healthy image is not automatically rolled
back for an unrelated later runtime fault.

The minimal app confirms its narrow updater-only health after validating
trust, initializing preserved application NVS, installing W5500 and starting the
HTTPS listener. It does not wait for DHCP/link/manager and does not claim
IR hardware health. The component owns no IR pairing or application tasks.

## Primary sources

The [Waveshare product documentation](https://docs.waveshare.com/ESP32-S3-ETH)
describes the 16 MiB flash, W5500 Ethernet and optional PoE module. The
[vendor Ethernet examples](https://docs.waveshare.com/ESP32-S3-ETH/Development-Environment-Setup-Arduino)
give MOSI 11, MISO 12, SCLK 13, CS 14, INT 10 and RESET 9. These are isolated
in Kconfig and **bench-unverified here**; other similarly named Waveshare
boards use different pins. The implementation uses Espressif's
[HTTPS server](https://docs.espressif.com/projects/esp-idf/en/v5.5.6/esp32s3/api-reference/protocols/esp_https_server.html)
and [OTA/rollback APIs](https://docs.espressif.com/projects/esp-idf/en/v5.5.6/esp32s3/api-reference/system/ota.html).

## Hardware acceptance — NOT RUN

Compile and host tests cannot prove Ethernet wiring, PoE supply behavior or
flash recovery. Before installing an actual IR application, bench-test:

1. USB first flash and unique trust provisioning; missing/wrong certificate
   rejection, server identity rejection, valid authenticated status/upload.
2. Successful version change over W5500/PoE, confirmation after real local
   health checks, and preservation of identity/configuration/pairing bytes.
3. Truncated upload, wrong digest, wrong chip/project, oversize and power
   interruption during write: previous image still boots.
4. New image crash, timer timeout, and power interruption before health
   confirmation: last confirmed image boots and status shows rollback.
5. Unplugged Ethernet/absent manager: local application health can still
   confirm; normal operation does not acquire a pairing lease.
6. USB recovery with both app slots damaged, and operator certificate
   rotation without erasing application NVS. Never run `erase-flash` as a
   normal update step.
