# 0014 — Reconcile only owned Linux USB/IP resources

Date: 2026-11-02. Status: accepted for the Linux reference backend.

## Problem

Repeated attach created duplicate imports, detach removed unrelated ports,
partial failures leaked imports, and producer health confused physical
presence with export readiness. The advertised TCP port was ignored.

## Decision

USB/IP remains an import into the consumer's Linux USB stack, not physical
OTG presentation. The driver reconciles exact host, TCP port and busid
tuples, using the Linux usbip global `--tcp-port` option. It never evicts an
unowned import or preempts a remote client's active export.

An optional private ownership journal is written and synchronized before
an import is attempted. It records only targets absent at that point or
already owned by this driver. Restart reconciliation may adopt an import
covered by that durable intent, including a command that completed just
before a crash. An existing import without an ownership record is a
conflict, not permission to adopt it. Missing/corrupt ownership records
never authorize detaching arbitrary ports. Explicit detach removes only
matching recorded imports and retains records when cleanup fails.

Each installation must allocate one ownership journal and serialize local
operations for that consumer. Kernel USB/IP has no owner token: a direct
external usbip operator racing the driver for the exact same export is
outside this reference backend's ownership guarantee. Port identities are
re-read before destructive operations; no generic USB/IP command can make
that check and detach atomic against an independent local administrator.

Attach is idempotent, replaces only previous owned targets, and rolls back
new imports after a partial failure when complete connection metadata
corroborates the target, including ambiguous command timeouts.
Previously owned requested imports are preserved. Failed cleanup stays
visible and retryable. An ownership journal is a cleanup/reconciliation
record, not the durable pairing authority or an ownership lease.

The stock CLI attaches in the kernel before writing its connection record.
A crash or record-write failure in that window can leave an imported port
with no trustworthy remote URL. Such a port is never detached by inference.
Unrelated pre-existing unknown ports do not block ordinary known imports.
If a pending owned target might occupy an unknown port, a durable recovery
barrier records the target and candidate local port numbers. Replacement,
new import attempts and successful teardown are blocked for that driver
until connection metadata is repaired or the operator identifies and
detaches the ambiguous port. Port disappearance/reuse clears the barrier
only after confirming the recorded ambiguous ports no longer exist in
that state. The diagnostic names the ports and manual recovery procedure.
An intent journal alone cannot promise automatic cleanup of every crash.

The formatted connection record (`host port busid\n`) must fit the CLI's
99-byte text capacity. Targets exceeding that bound are rejected before
any device or journal side effect; no accepted target relies on truncated
metadata for ownership.

Producer checks use injected Linux sysfs metadata to prove usbip-host
binding and kernel export status, plus a localhost daemon device-list
request on the configured TCP port. Newer usbipd omits in-use devices from
that list, so kernel status `used` is accepted with a responsive daemon;
an available device must appear in its export list. Binding is idempotent
and rolls back only new bindings after failure. The daemon and kernel
modules remain provisioned system dependencies, not processes implicitly
started by a media driver.

Only bindings newly established by the current driver are its teardown
resources. Pre-bound exports, including ones in use by another consumer,
are left untouched during rollback and shutdown. After a process crash,
pre-bound exports are conservatively unowned; the reference backend does
not pretend it can recover source-binding ownership without a source
journal. Explicit operator cleanup remains available through usbip.

Once producer startup has requested exports, status polls also reconcile
unbound configured devices. This repairs a failed initial bind or a device
that returns after unplugging without relying on consumer retry effects.
An explicit unbind disables that repair. A removed device is already
unbound for teardown purposes, while readiness still reports it unavailable.

All subprocess arguments and configured host/busid/port values are
validated; no shell or USB payload enters the control plane. Production
probes have injected alternatives so every behavior can be tested offline.

## Verification and limits

Scripted command transcripts and temporary sysfs/journal fixtures cover
retries, replacement, restart, partial failure, foreign imports, custom
ports, malformed output and false readiness. No endpoint hardware is
validated by these tests. Source-authorized re-pairing and physical USB
gadget presentation remain separate work.

Primary references: Linux [usbip command options](https://github.com/torvalds/linux/blob/master/tools/usb/usbip/src/usbip.c)
and [usbipd device-list behavior](https://github.com/torvalds/linux/blob/master/tools/usb/usbip/src/usbipd.c),
plus [kernel attach versus connection record ordering](https://github.com/torvalds/linux/blob/master/tools/usb/usbip/src/usbip_attach.c).
