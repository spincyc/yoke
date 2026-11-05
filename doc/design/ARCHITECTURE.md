# Yoke architecture

Status: current software architecture, 2026-11-02. YWP/1 is the multicast
wire protocol. The [decision index](../../site/pages/engineering/decisions.md)
records the protocol and operational decisions.

## Problem

Route HDMI and USB point-to-point links over a switched Ethernet network.
The selected USB prototype is two Radxa ZERO 3E hardware boxes, one
beside the USB device and one connected by USB to the user's computer.
USB 2 is the first deliverable; power and any PoE adapter need separate
validation. See [USB hardware endpoint](USB-ENDPOINT.md).
Any single producer must be re-pairable to any single
consumer at runtime. The payload must be transported transparently — the
control plane never interprets media payload. Content protection (HDCP)
remains between the endpoint hardware and the sink/source. There
is no central controller process; reconfiguration is signaled on the
network itself. A passive snooper must be able to verify and debug the
system out of band. Failure and recovery behavior must have repeatable tests.

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
  USB devices. The product has a second hardware box that connects by USB
  to a separate user computer; it is not a Linux application host. The
  existing USB/IP adapter imports into the box's Linux USB stack, but the
  physical USB device/gadget bridge to the user computer is unfinished.
  The [USB design](USB-ENDPOINT.md) owns this product topology and the
  USB 2 first milestone. Sessions are consumer-initiated.

The selected USB hardware is described in [USB-ENDPOINT.md](USB-ENDPOINT.md).
Earlier HDMI/USB3 proposals are preserved in `EDGE-DEVICES.md`. They keep the same split: the agent drives the
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

The installed package separates decisions, adapters, and operator commands.
Start with the module that owns the behavior you are changing:

```
src/yoke/
  core/          sans-I/O control protocol; no sockets, clock reads, or filesystem
    frames.py    wire format: canonical encode/decode, HMAC, validation
    node.py      agent state machine (producer & consumer behaviors)
    types.py     control dataclasses, effects, enums, and constants
  net/
    mcast.py     UDP multicast socket helper (interface-selectable)
    runner.py    executes core effects with sockets, a driver, and durable state
    executor.py  serialized driver work with actual cancellation/drain barriers
    notify.py    optional systemd startup readiness
  drivers/
    base.py      Driver protocol: attach(producer, media), detach(), poll(), describe()
    factory.py   validates backend configuration and constructs a Driver
    sim.py       in-memory driver with scriptable fault injection
    igmp.py      decoder-side multicast join/leave (reference software path)
    usbip.py     usbip attach/detach + producer bind/unbind
    gadget.py    metadata-only usb-gadget consumer: supervises the gadget worker
    jap.py       Just Add Power decoder API driver (channel switching)
    stream.py    starts/stops an out-of-process media pipeline (RTP)
    shellcmd.py  command-runner abstraction (mockable; never shell=True)
  media/         separate synthetic reference media plane (MEDIA.md)
    types.py     media units, stage events, feedback, and resource budgets
    pipeline.py  sans-I/O stage composition and finalization
    stages/      source, codec models, packetization, FEC, jitter, rate control, sink
    assembly.py  validated configuration and canonical sender/receiver construction
    simulation.py seeded virtual link, faults, and delayed feedback
    experiment.py deterministic synthetic media experiments and source-known results
    wire.py      reference packet framing and validation
    metrics.py   bounded aggregation of stage observations
    snoop.py     passive wire and stage-event observation
    sockets.py   UDP socket setup and transport validation
    runner.py    live sender, receiver, and passive observer shells
    cli.py       yoke-media argument parsing, offline reports, and command dispatch
  telemetry/     optional passive control observations and collector-host samples
    model.py     versioned, bounded snapshot data and validation
    collector.py sans-I/O collection of control observations
    host.py      collector-host sampling, separate from endpoint observations
    prometheus.py snapshot-to-metrics rendering
    http.py      immutable representation cache and bounded HTTP server
    client.py    bounded remote snapshot reads
    runtime.py   optional receiver/sampler worker
    tui.py       terminal view state, rendering, and interactive shell
    demo.py      synthetic offline samples
    cli.py       metrics/top command adapters
  config.py      per-agent TOML validation and shared PSK loading/resolution
  statefile.py   exclusive local lease and atomic durable state replacement
  simulation.py installed deterministic software fault campaign
  cli.py         yoke operator commands and injected intent/status/snoop helpers
  snoop.py       passive decoder/renderer for the control group
  terminal.py    shared escaping of untrusted terminal text
  update.py      standalone offline Linux release updater and recovery artifact
  usb/           computer-side USB data path, run only in the separate gadget worker:
    usbip.py     USB/IP 1.1.1 wire codec; client.py userspace import client
    rawgadget.py Raw Gadget ioctl backend behind gadget.py's GadgetBackend
    proxy.py     descriptor cache, control relay, endpoint pumps; session.py, control.py
    worker.py    yoke.usb.worker process and its bounded control socket
firmware/
  components/    shared ESP-IDF components: yoke_ota updater, yoke_eth, yoke_node lifecycle
  esp32/         IR endpoint: RMT IR, iTach-subset API, mutual-TLS OTA (ADRs 0025/0026)
  esp32-usb/     USB box (ESP32-S3/P4): USB host or TinyUSB device role (ADRs 0027/0030)
packaging/zero3e/ ZERO 3E device/computer box units, udev rules and install recipe
tests/           offline behavior, fault, driver transcript, and loopback tests
benchmarks/      reproducible control, media, and telemetry software measurements
scripts/
  build_docs.py  documentation builder CLI and compatibility facade
  docsite/       publication config, link validation, navigation, and rendering
site/            documentation template, navigation, and static assets
```

| Entry point | Implementation | Boundary |
|---|---|---|
| `yoke pair`, `unpair`, `status`, `snoop` | `yoke.cli` | Short-lived control client or passive observer |
| `yoke agent` | `yoke.net.runner` + `yoke.drivers.factory` | One endpoint's control runtime and backend |
| `yoke simulate` | `yoke.simulation` | Installed deterministic control fault campaign |
| `yoke metrics`, `top` | `yoke.telemetry.cli` | Optional read-only observation and host sampling |
| `yoke-media` | `yoke.media.cli` | Separate synthetic reference media tools |
| `python -m yoke.update` | `yoke.update` | Explicit offline Linux release operations |
| `python firmware/verify.py` | `firmware/verify.py` | Software verification of the separate firmware projects |
| `python scripts/build_docs.py` | `scripts.docsite` | Developer-only static documentation build |

The control core imports only its own types and wire codec. Drivers consume
control metadata and report backend evidence; they do not import the reference
media pipeline or inspect media payload. `drivers.factory.build_driver` owns
backend selection and configuration errors; the runner owns effect ordering,
resource lifetimes, and worker drain. `net.runner.build_driver` remains an
import alias for existing integrations.

Shared PSK resolution belongs to `config`, so telemetry commands do not depend
on the main command shell. Terminal escaping belongs to `terminal`, so media
observation does not need to import the control snooper for text rendering.
The original CLI and snooper helper names remain import aliases.

Tests mirror the package with `test_media_*` and `test_telemetry_*` groups and
dedicated driver suites. `tests/simnet.py` and `tests/media_simnet.py` are
independent test harnesses; the installed simulators never import the test tree.
Keep benchmark timings separate from their deterministic workload results.
The Linux updater deliberately remains one stdlib-only file: recovery uses a
fixed copy outside the candidate release, so splitting it across package
modules would make recovery depend on the release it must recover.

### Sans-I/O core

`core.node.Node` is a pure state machine. Inputs: decoded frames plus a
monotonic timestamp, the wall-clock reading the shell took at receipt and
the datagram's source address; `poll(now)` for timers, and explicit
persistence and driver completion inputs. Wall time only bounds how far a
peer's sequence may run ahead and seeds a capped restart floor; every timer
is monotonic, so clock steps by time synchronization neither expire peers
nor move retries (ADR 0023). Outputs: a list of effects — frames to send,
driver commands, producer data-plane admission sets, leveled local log
lines, state to persist. The shell (`net.runner`) performs effects.
Consequences:

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
| Box boots before time sync (no RTC) | Its own frames stay admissible; it admits peers once the clock steps, without restart |
| Boot with a far-future clock | Restart floor advances at most 60 s past durable state; explicit rebase recovers older future reservations |
| USB producer device replugged | Producer's `active` announce makes the paired consumer reattach immediately |
| Owned USB/IP import vanishes beside unrelated record-less ports | Only ports newer than the import's last sighting can block; otherwise re-attach proceeds |
| Old driver call completes after reassignment | Work is serialized and stale-generation results are discarded |
| Forged/altered frames | HMAC-PSK verification; failures counted and evented, frame dropped |
| Partition | Each side keeps last state; versions reconcile on heal |

## Security

Control frames are authenticated with HMAC-SHA256 over the transmitted
frame bytes (with the `sig` member excised; the legacy canonical
re-serialization is also accepted) using a pre-shared key of at least 32
bytes provisioned per site (ADRs 0005, 0023). Published vectors let other
implementations verify interoperability. Replay is
bounded by per-origin monotonic sequence numbers. The threat model is
"a device without the site key must not re-route streams or spoof state".
Every site-key holder is trusted to author messages, including another
node's origin. Open mode is explicitly unauthenticated, must be selected
explicitly and is intended for isolated labs. Control metadata is cleartext so the passive snooper can
observe it. Media-plane security belongs to the endpoint backend. The
reference USB/IP backend adds no encryption, but a producer can restrict
new data sessions to the consumers whose authenticated announces claim it,
at their announces' source addresses, through a root nftables helper
(ADR 0024). Source addresses are not authenticated, so this stops off-path
LAN hosts, not on-path attackers or key holders.
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
- **Clock prerequisite.** A box whose clock stays more than an hour behind
  its peers cannot admit their frames until time synchronization corrects
  it (ADRs 0007, 0010, 0023); its own frames remain admissible. Nothing
  rebases a sequence automatically: a box cannot tell a behind clock from a
  future reservation.
- **USB/IP data plane.** Admission filters by unauthenticated IPv4 source
  address and the payload is unencrypted (ADR 0024); the device-box units
  and rules are bench-unverified.
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
