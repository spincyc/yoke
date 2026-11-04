# Deploying yoke

This page covers the current software agents and backend labs. The USB
product is the [two-box ZERO 3E hardware design](USB-ENDPOINT.md); a Linux
USB/IP import on a separate computer is not that product.

## Topology

One agent per endpoint. Where the endpoint is embeddable Linux, the agent
runs onboard; for closed appliances with a device API (Just Add Power,
NMOS devices) the agent runs as that device's *warden* on any always-on
box. SDVoE is reachable only through its ZMP site server, a central
controller (`doc/research/HARDWARE.md` §1). A warden is one process per
device, with no shared state and no global knowledge (ARCHITECTURE.md,
"Where the agent runs"). The CLI and snooper run anywhere on the LAN.

## Network (summary of doc/research/HARDWARE.md prerequisites)

The following AV network checklist belongs to the earlier high-bandwidth
video research. Jumbo frames, a dedicated 10G port and PoE are not blanket
requirements for the selected USB 2 prototype. Use the requirements of the
backend actually being tested.

Dedicated AV VLAN; IGMP snooping on; exactly one pinned querier;
fast-leave on; jumbo frames on all AV ports and trunks; one endpoint per
access port, sized above its stream (a 4K120 DSC stream is ~8 Gb/s and
needs a dedicated 10G port); unicast for high-rate flows until the
flood-containment drill passes; PoE budget per the endpoint's datasheet.
The control group (`239.121.111.107:9753`) is tiny traffic and rides the
same VLAN so the control snooper can observe its datagrams.

Set `interface = "10.20.30.2"` to the agent host's local IPv4 address on
that VLAN. The CLI uses the equivalent global `--interface 10.20.30.2`
option. `0.0.0.0` delegates selection to the kernel and is suitable only
when its route selects the intended interface. Group values must be IPv4
multicast literals, ports 1–65535, and control TTL is fixed at 1. Linux
sockets subscribe only to their selected group even when other groups use
the same UDP port.

Run `yoke snoop` on a wired host in that VLAN during bring-up to observe
received announcements, intent/ack exchanges, and `rx-drop` events that
reveal auth or replay problems. This observer reads the control group; it
does not measure media bitrate or detect media flooding.

For yoke's synthetic reference media framing, use the separate media
observer (see [MEDIA.md](MEDIA.md)):

```
yoke-media snoop --live --group 239.10.0.1 --port 5004 \
    --interface 10.20.30.2 --duration 10
```

For vendor hardware streams, use an external traffic capture tool such as
`tcpdump` or Wireshark and the switch's per-port traffic counters. To
investigate flooding, capture at a switch mirror or an access port with
no multicast member. Check whether stream traffic reaches that port;
joining the media group there would change the condition being measured.
The reference media observer does not validate vendor-specific framing.

## Files on an endpoint

| Path | Purpose |
|---|---|
| `/etc/yoke/agent.toml` | identity, role, kind, driver (see `examples/`) |
| `/etc/yoke/psk` | site pre-shared key, mode 0600 |
| `/var/lib/yoke/state.json` | pairing + sequence state (agent-managed) |

Provision the PSK once per site as hex (a single trailing newline is
stripped on load; every other byte is significant, so hex avoids any raw
`\n`/whitespace ambiguity):

```
( umask 077; head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' \
    > /etc/yoke/psk )
```

Yoke refuses a PSK file with any group/other access, a final symbolic link,
a nonregular file, an empty key, a key shorter than 32 bytes, or more than
4096 bytes. These checks apply to both CLI and agent. Open mode is for
offline labs only and is never a default (ADR 0023): an agent config
without `psk_file` must say `open_mode = true`, and `yoke pair`/`unpair`
without a key need `--open`. Startup warns `UNAUTHENTICATED`, and
status/intent JSON labels it `open-unauthenticated`. Open mode accepts
unsigned control frames from anyone on the segment. A site PSK
authenticates membership in the trusted site; any holder can forge another
node's messages (the accepted ADR 0005 limitation). Control sockets bind
the group address, so unicast datagrams to a host's control port are never
read.

Create a dedicated `yoke` system account and make `/etc/yoke/psk` readable
only by that account (owner `yoke`, mode 0600). Give that account ownership
of `/var/lib/yoke`, mode 0700. Keep the config root-owned and readable by
the service account. Provision each endpoint with a unique `node_id` and a
separate state file; processes must never share either identity or state.

## Install the service runtime

The service uses its own Python environment outside home directories.
From a reviewed checkout, on a fresh Linux installation with Python 3.12
or newer, install a noneditable package copy:

```
sudo install -d -m 0755 /opt/yoke/releases/initial
sudo /usr/bin/python3 -m venv /opt/yoke/releases/initial/venv
sudo /opt/yoke/releases/initial/venv/bin/python -m pip install --no-deps .
sudo ln -sT releases/initial /opt/yoke/current
/opt/yoke/current/venv/bin/python -m yoke.cli --help
sudo install -m 0644 src/yoke/update.py /opt/yoke/updater.py
sudo install -m 0644 examples/yoke-update-recover.service /etc/systemd/system/
sudo install -m 0644 examples/yoked.service /etc/systemd/system/
sudo systemctl daemon-reload
```

Package building may download build dependencies; the installed runtime
has no external Python dependencies. `/opt/yoke/releases/initial` and the
`current` symlink must be owned by root and readable/executable by `yoke`.
The symlink command intentionally refuses an existing destination; do not
overwrite a running installation with this fresh-install recipe. Both the
service and commissioning command below use the installed interpreter.
An editable checkout, `.scratch` environment, or `PYTHONPATH` under a home
directory is incompatible with the unit's `ProtectHome=true` restriction.
Use a system Python interpreter outside home directories when creating the
environment: virtual environments retain their base-interpreter path.
The fixed maintenance script and recovery unit are required by the agent
unit. Release staging, activation, rollback, and interrupted-update recovery
are documented in [UPDATES.md](UPDATES.md); they preserve configuration and
pairing state.

## First commissioning and state recovery

After provisioning the config/key and state directory, run once in the
foreground as the service account:

```
sudo -u yoke /opt/yoke/current/venv/bin/python -m yoke.cli agent --commission \
    --config /etc/yoke/agent.toml
```

Confirm the startup announce, then stop it and start the service. Commission
only with a synchronized clock: a fresh identity's first sequence floor
comes from the wall clock, while later restarts are anchored to durable
state and can advance at most 60 s from the clock (ADR 0023). The
commissioning option permits an absent state file to be created through
the normal durable startup barrier. Existing valid state is restored;
corrupt, unreadable, symbolic-link, and nonregular state are errors even
with that option. Ordinary starts require an existing valid file. Never
add `--commission` to the unit: missing commissioned state must stop
startup, rather than silently resetting a pairing or sequence history.

For lost state, stop the endpoint and restore a known-good backup where
possible. Otherwise investigate its peer sequence history and desired
pairing before explicitly commissioning a new identity. Changing identity
requires updating intents that name it. Renaming or deleting state alone
is not an automatic recovery procedure (ADRs 0012 and 0016).

## Service

`examples/yoked.service` runs under the dedicated unprivileged account,
with a private state directory, restart, no capabilities and
`NoNewPrivileges`. It has no network-online or management-process
dependency. An absent interface can fail startup; systemd retries as the
network becomes available. It is ordered `After=time-sync.target` but does
not want or require it (ADR 0023): if a site enables a time-wait unit the
agent starts once the clock is synchronized; otherwise it starts anyway
and frame admission follows the clock when it is stepped later. The agent
itself never gains privileges: USB/IP privileged work runs in separate
root units from [`packaging/zero3e/`](../../packaging/zero3e/install.sh)
(see ZERO 3E boxes below and ADR 0024).

The agent logs to stderr, which systemd sends to the journal with
priorities (`journalctl -u yoked`). Logs carry the full driver error text,
committed pairings, peer loss and return, admission changes and clock
diagnostics; multicast events stay bounded to 128-byte details. Set
`Environment=YOKE_LOG_LEVEL=debug` in a drop-in to troubleshoot.

The agent durably saves before acking and re-announces on restart. A CLI
or snooper may exit at any time without ending the endpoint's route;
neither owns a management lease. Orderly agent shutdown quiesces its local
driver and retains the pairing for restart. Hard-crash media behavior is
device-specific; recovery converges through the control protocol.

The unit uses systemd notification readiness with a 90-second startup
timeout. Ready means the local control socket, configuration, driver
description, and durable startup reservation are initialized. It does not
require a peer or establish working media. Slow producer startup and
hardware failures surface as driver health after control readiness.
The separate local update-recovery unit runs before the agent and does
not require network access.

For the reference software HDMI receiver, use
`examples/agent-consumer-stream.toml`. The `stream` driver supports HDMI
consumers, supervises the out-of-process GStreamer receiver, and accepts a
finite positive `stop_timeout` (default 5 seconds). Its `active` state means
the worker process is running; it does not independently verify decoded
pictures or physical output. Install the external media tools from
`MEDIA.md` and provide the service account the required display/device
access. No external media tools are required for the deterministic `sim`
driver or `yoke simulate`.

## ZERO 3E boxes

⚠ Bench-unverified: this recipe is checked offline (unit syntax, privilege
boundaries, staged install, simulated admission), not on the boxes. The
peripheral port's bus id, Debian's `usbip` paths and the kernel's handling
of the rules need a first bench run.

Install the package as above on both boxes, plus `usbip` and `nftables`
from the distribution on the device-side box. Then, from the checkout:

```
sudo packaging/zero3e/install.sh device --node-id usb-dev \
    --host 10.20.30.2 --busid 1-1          # this box's control-VLAN IPv4
sudo packaging/zero3e/install.sh computer  # on the computer-side box
```

The installer creates the `yoke` account and directories, installs the
agent units and role drop-ins, never overwrites `/etc/yoke/agent.toml`,
and prints the remaining enable/commission commands; it starts nothing.
`--root DIR` stages the files without touching the system.

On the **device side** the agent (producer) stays unprivileged. Root-owned
units do one job each:

| Unit / file | Job |
|---|---|
| `modules-load.d/yoke-usbip-host.conf` | load `usbip-host` at boot |
| `70-yoke-usbip-bind.rules` → `yoke-usbip-bind@BUSID.service` | bind the configured bus position(s) to `usbip-host` at coldplug and on every replug |
| `yoke-usbip-admission.service` + `.path` | load the `inet yoke_usbip` nftables table: new TCP sessions to the USB/IP port only from admitted consumers and loopback (`CAP_NET_ADMIN` only) |
| `yoke-usbipd.service` | run `usbipd`, only after the admission table exists |

The generated `agent.toml` sets `bindings = "external"` (the agent never
runs `usbip bind`) and `admission = "nftables"`. The agent admits each
consumer whose authenticated announces claim it, at the IPv4 source of
those announces (ADR 0024); the computer-side box's USB/IP client connects
from that same address. Check with
`cat /run/yoke-admission/status.json` and `nft list table inet yoke_usbip`.
The producer reports `pending` until the helper applied its latest
allowlist and `error` if the helper failed.

On the **computer side** the installer only installs the unprivileged
agent. Its USB gadget worker and that worker's unit are installed
separately and are not part of this recipe.

A lab Linux consumer that imports through the kernel (`vhci-hcd`) uses
`install.sh vhci-lab`: a udev rule makes the `vhci_hcd` attach/detach
attributes group-writable for `yoke`, tmpfiles creates `/run/vhci_hcd`
owned by `yoke`, and a drop-in allows writing it, so `usbip attach` writes
the import and its connection record under one identity. Granting only
the sysfs half makes every import a record-less RECOVERY_REQUIRED port.

## Day-2 operations

- **Re-route**: `yoke pair cam-garage tv-den` from anywhere on the VLAN.
  Exit code 0 means the consumer durably committed the pairing. It does
  not promise active media: the CLI reports activation as unconfirmed
  unless a matching announce already established its state. Inspect
  `yoke status` for `pending`, `active`, or `error`. Exit 1 means no ack
  before the deadline; 2 is invalid input/rejection; 3 is network I/O failure.
  A fresh operation uses a random version author even with a stable
  `--origin`; retries retain that operation's version. This prevents a
  delayed authentic acknowledgment for an earlier target from confirming
  a new request derived from an old announcement.
- **Audit**: `yoke status` renders the announce cache (who is up, paired
  to what, in which state); `yoke status --json` and `yoke pair P C --json`
  emit sorted compact JSON with `schema_version: 1` for scripting.
  `yoke snoop --json` streams every frame for
  scripting or archival.
- **Stop a snoop**: Ctrl-C prints its final topology to stderr. Liveness
  uses the stop time, so an endpoint silent beyond `PEER_TIMEOUT` is
  marked `live: false` even if the whole group went silent. Stopping the
  snooper sends no control frames and does not change pairings.
- **Verify a mystery**: reconstruct recently observed control state with
  `yoke snoop --json > incident.jsonl`. Its bounded, potentially lossy
  history cannot establish events it missed or a complete global state.
  Use the recorded frames and verdicts to investigate offline.
- **Key rotation**: distribute the new PSK, restart agents (order
  irrelevant; agents with the old key are simply deaf/dropped until
  restarted, visible as `auth` counters in `rx-drop` events).
- **Clocks**: an RTC-less box booted behind its peers drops their frames
  as `stale` and logs `this clock is behind`; it recovers on the next frame
  after time synchronization steps the clock, without a restart. If the
  agent logs `durable sequence bound is …s ahead` or `own sequence is …s
  beyond`, and `timedatectl` confirms the clock is correct, an earlier boot
  persisted a future reservation: stop `yoked`, run
  `sudo -u yoke /opt/yoke/current/venv/bin/python -m yoke.cli agent
  --config /etc/yoke/agent.toml --rebase-sequence` once, then start it.
  The command refuses while the agent runs or when the bound is already
  admissible, and keeps the pairing (ADR 0023). Never edit `state.json`.

## Failure playbook

| Symptom | Meaning | First move |
|---|---|---|
| Consumer announces `pending` | producer absent or media not confirmed | is the producer announcing? check its driver events |
| `rx-drop {auth: n}` rising | key mismatch or an intruder | compare PSK provisioning; snoop for the origin id |
| `rx-drop {stale: n}` rising on one origin | clock far behind + state loss (ADR 0007), or replay attack | check that node's NTP; snoop its seq values |
| One node drops everyone as `stale`; its log says `this clock is behind` | that box booted before time sync | fix its time source; admission recovers on the next frame |
| Log says `durable sequence bound is …s ahead` | clock behind, or a past future-clock boot | confirm the clock; if correct, `--rebase-sequence` (Day-2) |
| USB producer `pending`: `admission allowlist … not yet applied` | admission helper has not run | `systemctl status yoke-usbip-admission.path yoke-usbip-admission.service` |
| USB consumer `error`, producer `active`, no import | consumer's address not admitted | its announces must reach the producer; compare `/run/yoke-admission/status.json` |
| USB consumer reports `RECOVERY_REQUIRED` | an owned import lost its connection record | follow the message: restore the record or `usbip detach -p PORT` |
| Video dies ~4 min after working | querier lost, snooping state aged out | pin the querier (HARDWARE.md §network) |
| Re-pair glitches for seconds | fast-leave off (old+new stream > port speed) | enable fast-leave |
| Everything works except one trunk | jumbo-frame mismatch on that trunk | verify MTU end-to-end |
