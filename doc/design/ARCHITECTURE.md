# Yoke architecture

Status: current software architecture, 2026-11-02. YWP/1 remains the
multicast wire protocol; ADRs 0012–0017 record the correctness changes.

## Problem

Route HDMI and USB point-to-point links over a switched Ethernet network.
Endpoints are small boxes whose only ports are HDMI (or USB) plus
Ethernet/PoE. Any single producer must be re-pairable to any single
consumer at runtime. The payload must be transported transparently — the
control plane never interprets media payload. Content protection (HDCP)
remains between the endpoint hardware and the sink/source. There
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
  USB devices; the reference consumer imports them into its local Linux
  USB stack. A physical USB output to another computer needs a separate
  gadget/proxy backend, which is not implemented. Sessions are
  consumer-initiated, matching the pairing model below.

Where no commercial endpoint meets the requirement (4K120 over 10G, a
supervised USB3 mesh), Yoke's endpoint proposals describe the media-plane
role (`EDGE-DEVICES.md`). They keep the same split: the agent drives the
endpoint through a driver, and gateware or an external process carries
the media (ADR 0011).

The agent never touches media payload. HDCP is negotiated hop-by-hop by
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
  attach to (or none). The named consumer stages it, waits for durable
  persistence, then commits and acknowledges. Driver activation is a
  separate outcome. Other nodes ignore it except that peers
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

Pairing is durable desired configuration, not a heartbeat lease. Producer
loss changes operational state to `pending`; only an explicit intent
changes the configured peer. Acknowledgment means the pairing was saved,
not that a player, display or USB application performed an action.

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
    executor.py  serialized driver work with actual cancellation/drain barriers
  drivers/
    base.py      Driver protocol: attach(producer, media), detach(), poll(), describe()
    sim.py       in-memory driver with scriptable fault injection (tests)
    igmp.py      decoder-side multicast join/leave (reference software path)
    usbip.py     usbip attach/detach + producer bind/unbind
    jap.py       Just Add Power decoder API driver (channel switching)
    stream.py    starts/stops an out-of-process media pipeline (RTP)
    shellcmd.py  command-runner abstraction (mockable; never shell=True)
  media/         software media pipeline + yoke-media CLI (MEDIA.md)
  telemetry/     optional passive metrics, collector-host sampling and terminal UI
  config.py      per-agent TOML config with fail-fast validation
  statefile.py   exclusive local lease and atomic durable state replacement
  simulation.py installed deterministic software fault campaign
  cli.py         yoke pair|unpair|status|snoop|agent|simulate
  snoop.py       passive decoder/renderer for the control group
benchmarks/      repeatable control and media software measurements
site/            local static documentation shell and navigation
```

### Sans-I/O core

`core.node.Node` is a pure state machine. Inputs: decoded frames plus a
monotonic timestamp; `poll(now)` for timers, and explicit persistence and
driver completion inputs. Outputs: a list of effects
— frames to send, driver commands, events, state to persist. The shell
(`net.runner`) performs effects. Consequences:

- Every protocol behavior, timer, and failure mode is unit-testable with
  a fake clock and no sockets.
- A simulated network (`tests/simnet.py`) connects many `Node` instances
  through a scheduler that can drop, duplicate, reorder, delay, and
  partition deterministically. The installed `yoke simulate` command
  exercises those contracts without importing the test suite.
- The asyncio shell owns sockets, storage and actual driver execution.
  Failed storage writes cannot release a success acknowledgment or leave
  an in-memory committed pairing that later acknowledges a duplicate.

Before sending frames, a node durably reserves sequence numbers. Restart
skips the unused part of a reservation rather than reusing emitted values.
This avoids a write for every announcement while preserving uniqueness
through a same-second restart with intact storage. State loss and storage
rollback need explicit recovery; the protocol does not claim a hardware
monotonic trust anchor.

### Drivers

A driver translates the abstract pairing ("consume producer P" /
"produce on group G") into the device's reality. Drivers are handed a
command runner and report `DriverStatus` (active/pending/error + detail).
`active` means the backend confirms its requested attachment: membership
for IGMP, a running supervised process for `stream`, a selected channel for
JAP, or imports for USB/IP. It is not universal proof of media flow or
physical output. Details expose the backend's evidence.

Driver work runs on one executor. Route changes fence older generations;
late status results cannot reactivate an old route. Replacement waits for
confirmed teardown. Cancellation of an asyncio task does not establish that
a device call or subprocess has stopped. Failures surface as `evt` frames
and capped-backoff retries. The `sim` driver can be scripted to fail in
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
| Storage write fails | Committed pairing retained; no success ACK or uncommitted driver change |
| Two local processes use one state file | Second startup fails on the held exclusive lease |
| Old driver call completes after reassignment | Work is serialized and stale-generation results are discarded |
| Forged/altered frames | HMAC-PSK verification; failures counted and evented, frame dropped |
| Partition | Each side keeps last state; versions reconcile on heal |

## Security

Control frames are authenticated with HMAC-SHA256 over canonical frame
bytes using a pre-shared key provisioned per site (ADR 0005). Replay is
bounded by per-origin monotonic sequence numbers. The threat model is
"a device without the site key must not re-route streams or spoof state".
Every site-key holder is trusted to author messages, including another
node's origin. Open mode is explicitly unauthenticated and intended for
isolated labs. Control metadata is cleartext so the passive snooper can
observe it. Media-plane security belongs to the endpoint backend; the
reference USB/IP transport does not add encryption or per-peer admission.
See [Security](../../SECURITY.md) for the exact boundary.

Multicast is a supported operating mode, not a debugging mirror managed by
Thermite. An observer joins alongside agents and transmits no application
control or media datagrams. Normal kernel IGMP membership traffic still
occurs. The separate synthetic media runner also supports multicast with
an independent passive observer; feedback is disabled by default and must
be directed explicitly to a designated unicast reporter path. Protected
media remains point-to-point under ADR 0011.

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
- **USB ownership scope.** YWP/1 owns pairings per consumer. A producer's
  USB/IP backend refuses simultaneous device attachment; there is no
  distributed, authenticated source-owner transaction. The driver now
  reconciles its owned imports and fences replacement locally. A future
  source-authorized protocol must have its own ADR and version rather
  than adding optional authority fields ignored by legacy nodes.
- **Observation scope.** Snooper caches and packet histories are bounded.
  Announcements reconstruct recently observed endpoint state; an observer
  cannot prove an unobserved endpoint's physical state, recover every
  historical event, or authenticate individual holders of a shared key.
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
