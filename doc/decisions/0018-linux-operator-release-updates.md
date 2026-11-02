# 0018 — Operator-controlled Linux release updates over Ethernet

Date: 2026-11-02. Status: accepted.

An onboard Linux endpoint must remain maintainable without Thermite, a
controller, or a cloud service. Use the host's explicitly provisioned
OpenSSH/SFTP administration access to transfer an approved release and run
a local, root-owned maintenance tool. Do not add an unauthenticated HTTP
listener, a vendor credential, or update authority to YWP/1. The YWP site
PSK does not grant operating-system update permission. Any SSH/SFTP client
and any conforming artifact builder have the same interface.

The normative Linux update contract is [UPDATES.md](../design/UPDATES.md).
An operator supplies a SHA-256 digest acquired through their trusted release
approval path. The installer verifies the entire artifact and its bounded,
strict manifest and wheel before installation. It installs a dependency-free
pure Python wheel into a new, versioned virtual environment, offline, using
the host Python's bundled ensurepip. It never runs a source build or fetches
dependencies on the endpoint. Optional dependencies marked only for the
`dev` extra may remain in wheel metadata; that extra is never selected.
Unconditional dependencies or other marker expressions are refused.
Staging cannot replace an existing release.

Only explicit activation stops the endpoint and atomically changes
`/opt/yoke/current`. Config, key, identity, pairing, sequence reservations,
and driver ownership journals remain outside release directories. Updates
do not commission, reset, expire, migrate, or restore those files. Only the
existing durable state format is supported; a future incompatible migration
requires its own reversible design and operator procedure.

The service uses standard systemd `Type=notify`, with `NotifyAccess=main`
(local startup semantics in ADR 0013).
READY is sent after local control initialization and the durable startup
barrier. Successful start plus a short stable active window establishes
local control readiness; it does not establish peer presence, media flow,
USB application behavior, or physical output. No health check sees media
payload. An unhealthy candidate is stopped and the previous release is
restarted. Failure to recover is reported as `recovery-required`.

A durable journal precedes activation. A fixed maintenance script outside
`current`, and a boot recovery unit ordered before the agent, restore the
previous release after an interrupted transaction. This includes a crash
after link replacement but before health confirmation. A running system's
recovery command stops the service before restoring it; offline recovery is
reserved for the boot unit or an operator who has already stopped it.

This updates Yoke application software, not the bootloader, kernel, Linux
image, USB firmware, or third-party appliances. A corrupt filesystem, lost
SSH access, or an incompatible external OS/toolchain still needs an
out-of-band recovery path. The Radxa ZERO 3E install, power-cut behavior,
service readiness, and physical device behavior remain bench-unverified.
