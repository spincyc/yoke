# 0024 — USB/IP data-plane admission and ZERO 3E device-box privileges

Date: 2026-11-04. Status: accepted for the Linux reference backend and the
ZERO 3E packaging. Real kernel, udev, systemd and nftables effects are
⚠ bench-unverified; every decision below is exercised offline.

## Context

A cold review found four USB/IP backend defects:

1. **Unauthenticated data plane.** Stock `usbipd` on the device-side box
   accepts TCP 3240 from any LAN host, so anyone could import the paired
   keyboard. YWP/1 producers never learn which consumer is theirs.
2. **Unrunnable service.** The sample unit ran `User=yoke` with
   `NoNewPrivileges` and `ProtectSystem=strict`, but `usbip bind/unbind`
   write root-only sysfs attributes and `usbip attach` writes vhci sysfs
   plus a connection record under `/var/run/vhci_hcd`. Granting only the
   sysfs half makes every attach leave a record-less port, which ADR 0014
   treats as RECOVERY_REQUIRED.
3. **Recovery wedge.** When an owned import vanished (unplug, TCP reset)
   while an unrelated record-less vhci port existed, the driver attributed
   that port to the vanished import and refused every re-attach forever —
   contradicting ADR 0014's promise that unrelated unknown ports never
   block ordinary routes.
4. **Polling cost.** Producer health ran `usbip list -r 127.0.0.1` (a
   process spawn plus a daemon round trip) every second.

## Decision

### Producer-side admission

A producer core configured for admission derives an **admission set** from
the control plane: each consumer whose latest accepted, authenticated
announce claims this producer (`pairing.producer` is this node, same
`kind`), that is neither timed out nor departed, paired with the IPv4
source address of its accepted frames. Replayed and duplicate frames are
never accepted, so they cannot bind an address. A consumer's admitted
address is set, or changed, only after two consecutive accepted frames
from the same source; a consumer's normal commit (ack, event, announce)
satisfies that within milliseconds. The core emits an `Admission` effect
whenever the set changes, and an empty one at startup so a restarted
producer never inherits stale permission.

The runner forwards the set to an optional driver `admit()` hook on the
serialized driver worker. The Linux producer driver passes it to an
admission backend seam:

- `none` (default for existing configurations): no enforcement; the data
  plane remains open to the LAN, as before.
- `nftables`: the unprivileged agent atomically writes a bounded request
  (`schema`, `generation`, `port`, IPv4 `allow` list, `peers` labels) to
  its runtime directory. A root oneshot helper (`yoke usbip-admission
  apply`), started by a systemd path unit, validates the request against
  the port fixed in its own unit, renders one fixed `inet yoke_usbip`
  table and loads it atomically with `nft -f`. Only new TCP connections to
  that port are filtered: established/related traffic, loopback and
  allowlisted IPv4 sources are accepted; other new IPv4 and IPv6
  connections to the port are dropped. The helper records the applied
  generation in a root-owned status file; the producer reports `pending`
  until the generation it requested is applied and `error` if the helper
  reports failure. The helper also runs at boot before `usbipd` with an
  absent request, so the port starts closed. Orderly agent shutdown
  requests the empty set.

A consumer that is paired but whose announces cannot reach the producer
cannot open a new data session; an established session is not torn down
by admission changes (the consumer's own detach ends it). Because a
producer `active` edge now prompts paired consumers to retry at once
(ADR 0023), the first attach after admission is applied does not wait for
backoff.

The computer-side box's userspace USB/IP client (separate work) connects
from the same box that runs its consumer agent and is therefore admitted
by the same address, provided both use the box's single control-VLAN IPv4
address (⚠ multi-homed boxes are unverified). Producers that enable
admission must advertise an IPv4 `host`.

Limits, accepted: IP source addresses are not authenticated, so an
on-path attacker or one that takes over an admitted consumer's address
can still connect; open mode makes the claims themselves unauthenticated;
every site-key holder may claim (ADR 0005); USB/IP payload remains
unencrypted. A producer whose clock falls more than an hour behind its
consumer rejects the consumer's frames as future (ADR 0023) and therefore
stops admitting new sessions from it until its clock is corrected;
established sessions continue. This is admission control for the
reference backend, not a USB/IP security protocol.

### Device-box privileges

The agent stays unprivileged on both boxes: `User=yoke`,
`NoNewPrivileges`, no capabilities. Privileged work moves to root units
with one job each:

- `yoke-usbip-bind@BUSID.service` binds a configured device to
  `usbip-host`, started by a generated udev rule when that bus position
  appears (and at coldplug). The producer driver's `bindings = "external"`
  mode never runs `usbip bind`/`unbind`; it reports devices that are not
  bound as an error naming the provisioning unit.
- `yoke-usbipd.service` runs the daemon on the configured port, ordered
  after the admission helper so the port is never open before the table
  exists.
- `yoke-usbip-admission.{path,service}` applies admission as above.
- `modules-load.d` loads `usbip-host` on the device side.

For a lab Linux consumer that imports through the kernel (`vhci-hcd`), a
udev rule grants group `yoke` write access to the `attach`/`detach`
attributes of each `vhci_hcd` platform device, tmpfiles creates
`/run/vhci_hcd` owned by `yoke`, and a unit drop-in adds that directory to
`ReadWritePaths`. Import and connection record therefore succeed or fail
together under one unprivileged identity. The product's computer-side box
does not use this path; its USB gadget worker has its own unit.

### Recovery attribution

The ownership journal (schema 3) records, per owned import, the
record-less ports already present when the import was last observed
with its connection record — initially the record-less ports present
before the import was attempted. When an owned import is missing, only
record-less ports *absent* from that baseline may be its candidates. If
none exists the import is gone: its ownership is released and re-attach
proceeds. Pre-existing unrelated ports are never attributed and never
block. Journals written by schema 1 or 2 carry no baseline and keep the
earlier conservative attribution until their imports are observed again.
The baseline is persisted only when it changes, not on every poll.

### Readiness polling

Producer polls read sysfs every time. The `usbipd` device-list probe runs
on the first poll, whenever the sysfs export state changes, five seconds
after a failed probe, and otherwise at most every 30 seconds. Between
probes the last successful export list stands. A daemon that dies while
exports are unchanged is detected within the probe interval; existing
kernel sessions do not depend on it.

## Consequences

`examples/yoked.service` stays unprivileged and runs every driver except
an agent-managed USB/IP binding; `packaging/zero3e/` ships the device-side
units, udev rule template, module list and an installer, plus the vhci lab
recipe. Existing producer configurations keep `bindings = "agent"` and
`admission = "none"` behavior unless they opt in; the ZERO 3E installer
writes both opt-ins. Simulated tests cover admission derivation, replay
and spoofing attempts, the rendered ruleset, helper validation, the
recovery baseline and probe scheduling. The physical effect of the rules
and units requires bench verification on the boxes.
