# Deploying yoke

## Topology

One agent per endpoint. Where the endpoint is embeddable Linux, the agent
runs onboard; for closed appliances (JAP, Visionary, IPMX boxes) the
agent runs as that device's *warden* on any always-on box — one process
per device, no shared state, no global knowledge (ARCHITECTURE.md,
"Where the agent runs"). The CLI and snooper run anywhere on the LAN.

## Network (summary of doc/research/HARDWARE.md prerequisites)

Dedicated AV VLAN; IGMP snooping on; exactly one pinned querier;
fast-leave on; jumbo frames on all AV ports and trunks; one endpoint per
1G access port; 10G uplinks between switches carrying multiple streams;
PoE budget ≈13 W per endpoint. The control group (`239.121.111.107:9753`)
is tiny traffic and rides the same VLAN so the snooper sees everything.

Run `yoke snoop` on a wired host in that VLAN during bring-up: it will
show announces from every healthy agent within ~2 s, every intent/ack
exchange, and `rx-drop` events that reveal auth or replay problems.
Streams appearing on ports with no member (querier loss / snooping
defect) show up as bitrate where the snooper expects silence.

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

Yoke refuses a `psk` file that is group- or other-readable. Open mode
(no `psk_file`) is for offline labs only.

## Service

`examples/yoked.service` runs the agent under systemd with restart. The
agent is crash-only software: it persists before acking, announces on
start, and converges from the network (anti-entropy) — killing it at any
moment is safe by design and is exercised in the simulation tests.

## Day-2 operations

- **Re-route**: `yoke pair cam-garage tv-den` from anywhere on the VLAN.
  Exit code 0 means the consumer acked; nonzero prints why (deadline,
  rejection reason from the ack).
- **Audit**: `yoke status` renders the announce cache (who is up, paired
  to what, in which state); `yoke snoop --json` streams every frame for
  scripting or archival.
- **Verify a mystery**: the snooper is the conformance oracle — global
  state is reconstructible from the multicast stream alone. Record it
  (`yoke snoop --json > incident.jsonl`) and replay reasoning offline.
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
| Re-pair glitches for seconds | fast-leave off (old+new stream > 1G port) | enable fast-leave |
| Everything works except one trunk | jumbo-frame mismatch on that trunk | verify MTU end-to-end |
