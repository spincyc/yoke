# Deploying yoke

## Topology

One agent per endpoint. Where the endpoint is embeddable Linux, the agent
runs onboard; for closed appliances with a device API (Just Add Power,
NMOS devices) the agent runs as that device's *warden* on any always-on
box. SDVoE is reachable only through its ZMP site server, a central
controller (`doc/research/HARDWARE.md` §1). A warden is one process per
device, with no shared state and no global knowledge (ARCHITECTURE.md,
"Where the agent runs"). The CLI and snooper run anywhere on the LAN.

## Network (summary of doc/research/HARDWARE.md prerequisites)

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
a nonregular file, an empty key, or more than 4096 bytes. These checks apply
to both CLI and agent. Open mode (no `psk_file`) is for offline labs only;
startup warns `UNAUTHENTICATED`, and status/intent JSON labels it
`open-unauthenticated`. It accepts unsigned control frames from anyone on
the segment. A site PSK authenticates membership in the trusted site; any
holder can forge another node's messages (the accepted ADR 0005 limitation).

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

Confirm the startup announce, then stop it and start the service. The
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
with a private state directory and restart. It has no network-online or
management-process dependency. An absent interface can fail startup;
systemd retries as the network becomes available. Drivers that require
device access or usbip privileges need a narrowly scoped installation
specific to that driver. The sample grants no blanket root access.

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

## Failure playbook

| Symptom | Meaning | First move |
|---|---|---|
| Consumer announces `pending` | producer absent or media not confirmed | is the producer announcing? check its driver events |
| `rx-drop {auth: n}` rising | key mismatch or an intruder | compare PSK provisioning; snoop for the origin id |
| `rx-drop {stale: n}` rising on one origin | clock far behind + state loss (ADR 0007), or replay attack | check that node's NTP; snoop its seq values |
| Video dies ~4 min after working | querier lost, snooping state aged out | pin the querier (HARDWARE.md §network) |
| Re-pair glitches for seconds | fast-leave off (old+new stream > port speed) | enable fast-leave |
| Everything works except one trunk | jumbo-frame mismatch on that trunk | verify MTU end-to-end |
