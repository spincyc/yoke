# 0019 — ESP32 local Ethernet firmware updates

Date: 2026-11-02. Status: accepted; hardware validation pending.

The ESP32-S3 IR endpoint needs updates after its initial USB installation.
It cannot host the CPython agent. Add a separate ESP-IDF firmware project
and reusable updater, without changing the Linux runtime dependency rule
(ADR 0008) or YWP/1. The reference app brings up W5500 Ethernet and the
updater; it does not implement IR routing or advertise an operational IR
endpoint.

Accept an explicit HTTPS upload authenticated by mutual TLS using a
physically provisioned, per-device trust partition. A pinned self-signed
operator certificate authorizes firmware replacement. There is no shared
default key, discovery enrollment, central manager, cloud, broker, remote
shell, or unrequested download. Third-party clients use the same documented
HTTP interface. Espressif TLS and OTA libraries perform cryptography and
image validation. A caller-supplied SHA-256 binds the intended image to the
upload; it is not a publisher signature or secure-boot policy.

Write only the inactive application slot, verify the complete digest and
ESP image, then select it for reboot. Preserve bootloader, partition table,
identity, trust, configuration, and pairing partitions. Enable bootloader
rollback. New firmware must explicitly confirm application health before a
bounded local timer expires. No live producer, manager, or network service
is required to pass the application health check. The reference app tests
only its own updater readiness, never claims IR behavior, and documents the
application hook required when an IR implementation is added.

TLS handshakes and firmware body reception have absolute deadlines enforceable
inside a partial TLS record. The pinned SDK's blocking handshake requires
a narrow, exact-source-checked build override; upload sockets have an
independent shutdown watchdog. Wired-only operation must enable and retain
primary hardware entropy before TLS seeds/reseeds. The reference app owns
the SAR ADC entropy source; applications using ADC/RF must explicitly
provide a compatible continuous entropy source instead.

The board has no trusted battery-backed time in this reference design.
Commissioning supplies a local wall-clock floor, which advances with uptime
and is retained across boots. TLS chain, key, and certificate validity checks
remain enabled; expiry is evaluated against that local clock, which cannot
account for time spent powered off. Certificate revocation/clock refresh is
an explicit physical reprovisioning operation. No external CA or NTP server
is required. This limitation is visible in status and documentation.

No eFuses are burned. Secure boot, flash encryption, physical key extraction
resistance, signed publisher releases, and hardware anti-downgrade are
separate future commissioning decisions, not implied security guarantees.
An authorized updater can deliberately install an older compatible image.
The implementation and bench acceptance contract are normative in
[ESP32-UPDATES.md](../design/ESP32-UPDATES.md).
