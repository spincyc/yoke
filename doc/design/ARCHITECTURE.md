# Yoke architecture

Status: v1 design, 2026-08-08.

## Problem

Route HDMI and USB point-to-point links over a switched Ethernet network.
Endpoints are small boxes whose only ports are HDMI (or USB) plus
Ethernet/PoE. Any single producer must be re-pairable to any single
consumer at runtime. The payload must be transported transparently — the
system never interprets HDMI or USB signals, so content protection (HDCP)
remains entirely between the endpoint hardware and the sink/source. There
is no central controller process; reconfiguration is signaled on the
network itself. A passive snooper must be able to verify and debug the
system out of band. This must hold up in production, not as a lab toy.

## Shape of the solution

Yoke is a **control plane**, deliberately separated from the media plane.

### Media plane (endpoint hardware)

The media plane is whatever the endpoint hardware does natively:

- **HDMI**: AV-over-IP encoder/decoder appliances. Each encoder
  (producer) transmits its stream to its own fixed multicast group,
  always. Each decoder (consumer) subscribes to exactly one encoder's
  group. "Switching" is therefore purely decoder-side: an IGMP leave of
  one group and a join of another, or the equivalent call on the
  appliance's local API. This is the standard commercial AVoIP pattern;
  the switch fabric prunes traffic via IGMP snooping.
- **USB**: USB-over-IP endpoint pairs (appliance, or usbip on an
  embeddable Linux box). The producer side exports the physically attached
  USB devices; the consumer side attaches to one producer and presents its
  devices to the downstream computer. Sessions are consumer-initiated,
  which matches the pairing model below.

Where no commercial endpoint meets the requirement (4K120 over 10G, a
supervised USB3 mesh), yoke's own endpoint designs fill the media-plane
role (`EDGE-DEVICES.md`). They keep the same split: the agent drives the
endpoint through a driver, and gateware or an external process carries
the media.

Yoke never touches the media payload. HDCP is negotiated hop-by-hop by
licensed endpoint silicon (encoder acts as HDCP sink, decoder as HDCP
source/repeater, per the vendor's certification). Yoke only decides — and
records — *who feeds whom*. See ADR 0004.

### Control plane (yoke)

Every endpoint runs (or is fronted by) a **yoke agent**. Agents speak the
Yoke Wire Protocol (YWP, `PROTOCOL.md`) on one well-known UDP multicast
group. The protocol has five frame types: `ann` (announce), `int`
(pairing intent), `ack`, `evt` (event), `bye`.

- **Discovery & state replication**: every agent multicasts a periodic
  `ann` carrying its identity, role, kind, health, and — for consumers —
  the pairing it currently applies, with that pairing's version.
- **Reconfiguration**: anything on the LAN (normally the ephemeral `yoke`
  CLI, but equally a button box, a scheduled job, a home-automation hook)
  multicasts a signed `int` naming a consumer and the producer it should
  attach to (or none). The named consumer applies it, persists it, `ack`s
  it, and re-announces. Everything else ignores it — except that peers
  cache the highest-version intent per consumer for anti-entropy.
- **Convergence**: intents carry a total-ordered version (Lamport counter,
  origin id as tie-break). A consumer applies the highest version it has
  seen; equal or lower versions are idempotently ignored. If an agent
  hears a consumer announce a pairing version older than a cached intent,
  it re-multicasts the cached intent after a suppression delay. The system
  therefore heals partitions and reboots without any coordinator.
- **Observability**: agents multicast `evt` frames on every state
  transition (pairing applied, media up, media down, driver error, peer
  lost, auth failure counters). `yoke snoop` passively joins the group and
  renders the full control-plane conversation; it transmits nothing.

There is no controller. There is no store of record beyond each agent's
own persisted state file. The CLI is a client that happens to run for a
few seconds.

### Where the agent runs

Commercial appliances will not run our code onboard. The agent supports
two placements, chosen per device in its config:

1. **Onboard** — for endpoints that are embeddable Linux (SBC-class or
   vendor SDK): the agent runs on the box and drives the local media
   pipeline or localhost API. This is the preferred, fully self-contained
   shape.
2. **Warden** — for closed appliances with a device API (telnet/REST):
   the agent runs on any box on the LAN as that appliance's dedicated
   warden, holding only that one device's pairing state and driving its
   API. Wardens are per-device, stateless beyond the device's own state,
   and many can share one host. This preserves "no central controller":
   there is no process that knows global state or that other devices
   depend on; losing a warden affects exactly its device, and pairing
   state for everything else keeps converging.

## Software structure

```
src/yoke/
  core/          sans-I/O protocol core — stdlib only, no sockets, no clock
    frames.py    wire format: canonical encode/decode, HMAC, validation
    node.py      agent state machine (producer & consumer behaviors)
    types.py     shared dataclasses, enums, and constants
  net/
    mcast.py     UDP multicast socket helper (interface-selectable)
    runner.py    wires core.node to the socket + a driver + persisted state
  drivers/
    base.py      Driver protocol: attach(producer, media), detach(), poll(), describe()
    sim.py       in-memory driver with scriptable fault injection (tests)
    igmp.py      decoder-side multicast join/leave (reference software path)
    usbip.py     usbip attach/detach + producer bind/unbind
    jap.py       Just Add Power decoder API driver (channel switching)
    stream.py    starts/stops an out-of-process media pipeline (RTP)
    shellcmd.py  command-runner abstraction (mockable; never shell=True)
  media/         software media pipeline + yoke-media CLI (MEDIA.md)
  config.py      per-agent TOML config with fail-fast validation
  statefile.py   atomic (write-fsync-rename) persisted-state load/save
  cli.py         yoke pair|unpair|status|snoop|agent
  snoop.py       passive decoder/renderer for the control group
```

### Sans-I/O core

`core.node.Node` is a pure state machine. Inputs: decoded frames plus a
monotonic timestamp; a `tick(now)` for timers. Outputs: a list of effects
— frames to send, driver commands, events, state to persist. The shell
(`net.runner`) performs effects. Consequences:

- Every protocol behavior, timer, and failure mode is unit-testable with
  a fake clock and no sockets.
- A simulated network (`tests/simnet.py`) connects many `Node` instances
  through a scheduler that can drop, duplicate, reorder, delay, and
  partition — deterministically, from a seed.
- The asyncio shell stays so thin it can be reviewed by eye and smoke
  tested over loopback.

### Drivers

A driver translates the abstract pairing ("consume producer P" /
"produce on group G") into the device's reality. Drivers are handed a
command runner and report `DriverStatus` (active/pending/error + detail).
Driver failures never crash the agent: they surface as `evt` frames and
retries with capped backoff. The `sim` driver can be scripted to fail in
every way we have seen or can imagine, which is how error conditions stay
covered offline (see ADR 0006).

## Failure model

| Failure | Behavior |
|---|---|
| Intent lost in transit | CLI retries until `ack` or deadline; exits nonzero on deadline |
| Consumer offline during intent | Peers cache intent; anti-entropy re-sends when it reappears |
| Conflicting concurrent intents | Total version order; all nodes converge on the winner |
| Duplicate/reordered/replayed frames | Per-origin monotonic seq; stale frames dropped and counted |
| Producer disappears | Consumer keeps pairing, media goes `pending`, event emitted; media resumes on return |
| Driver failure | `error` state + event + capped-backoff retry; never crashes agent |
| Agent reboot | Pairing + seq persisted; rejoins and re-applies before first announce |
| Forged/altered frames | HMAC-PSK verification; failures counted and evented, frame dropped |
| Partition | Each side keeps last state; versions reconcile on heal |

## Security

Control frames are authenticated with HMAC-SHA256 over canonical frame
bytes using a pre-shared key provisioned per site (ADR 0005). Replay is
bounded by per-origin monotonic sequence numbers. The threat model is
"malicious or buggy device on the LAN must not re-route streams or spoof
state"; confidentiality of control metadata is explicitly not a goal —
the snooper depends on its absence. Media-plane security is the endpoint
hardware's (VLAN isolation is the recommended deployment posture).

## Testing strategy

- **Unit**: frames (round-trip, tamper, oversize, garbage, wrong key,
  wrong version), node state machine transitions, driver adapters with
  mocked runners.
- **Property** (seeded, stdlib `random`): convergence — N nodes, arbitrary
  interleaving of intents and faults, network eventually quiescent ⇒ all
  nodes agree on the highest-version pairing per consumer; idempotence —
  re-delivery never changes state.
- **Simulation**: scripted scenarios for every row of the failure model,
  through the same code paths production uses (only the shell is swapped).
- **Loopback smoke**: the asyncio shell exercised over 127.0.0.1
  multicast, still no external network.

## Known limitations

- **Pre-shared-key security.** One site-wide key authenticates all
  control frames (ADR 0005). A compromised endpoint holding the key can
  forge intents; the DoS hardening (ADR 0010) caps the blast radius of
  unauthenticated and buggy senders but does not make a key-holder
  harmless. Per-device keys are the v2 answer and need an ADR (enrollment
  touches the no-controller principle).
- **Two minor async races in `net/runner.py`.** The driver-poll loop can
  publish a status captured between an intent's application and its
  attach task running (a brief spurious `media-down`/`media-up` blip that
  self-corrects on the next poll), and a driver call's worker thread can
  outlive task cancellation. Fix: a single-thread driver executor with
  generation-tagged statuses.
- **HDCP position is a well-supported reading, not a legal opinion**
  (ADR 0004); a commercial launch should get it reviewed.
- **No real endpoint hardware has been touched.** Driver assumptions are
  isolated and marked ⚠ in `doc/research/HARDWARE.md`.

## What yoke is not

- Not a video codec, USB stack, or HDCP participant.
- Not a many-to-many broadcaster in v1 (one consumer attaches to one
  producer; multiple consumers may attach to the same producer since
  decoder-side joins are independent — but yoke models each consumer's
  pairing independently, so this falls out for free on HDMI; USB remains
  strictly 1:1 per producer, enforced by the producer side, since USB
  cannot be meaningfully shared at this layer).
- Not a discovery service for anything but its own endpoints.
