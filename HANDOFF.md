# Yoke — implementation handoff

Updated 2026-11-02. This file describes the current development direction;
run `make verify` for the current checkout's result rather than relying on a
historical test count. The repository's deployment branch is `feature/ir`.

## Goal and present boundary

The goal is a decentralized, point-to-point, reconfigurable mesh of HDMI
and USB endpoints over switched Ethernet, with a 10GbE baseline and small
endpoints. The software reference exists; **no actual endpoint hardware
has been bench-tested**. The proposed 4K120 DSC link, physical USB output,
HDCP ciphertext relay, and competitive-gaming latency targets remain
hardware or integration work.

The September research is dated analysis. Its vendor availability,
performance estimates, and legal interpretations have not been revalidated
for publication. Its feasibility arguments are not an end-to-end
implementation result. Keep its assumptions and ⚠ bench flags visible.

## Read in this order

| Document | Purpose |
|---|---|
| `README.md` | Public software status and commands |
| `doc/design/ARCHITECTURE.md` | Current state ownership, driver execution, and trust boundaries |
| `doc/design/PROTOCOL.md` | Normative YWP/1 contract |
| `doc/decisions/0012-*` through `0017-*` | Durable transitions, serialized effects, USB ownership, media bounds, explicit provisioning, and status evidence |
| `doc/design/SIMULATION.md` | Installed offline fault campaign and control benchmark |
| `doc/design/MEDIA.md`, `MEDIA-TESTING.md` | Synthetic media transport and verification layers |
| `doc/design/PERFORMANCE.md` | Reproducible software measurements and their limits |
| `doc/design/UPDATES.md`, `ESP32-UPDATES.md` | Operator-controlled Linux releases and the ESP32 Ethernet OTA reference |
| `doc/design/USB-HOUSING.md`, `BRANDING.md` | Enclosure revisions, assembly limits, and shared identity assets |
| `doc/design/TELEMETRY.md` | Optional passive metrics, JSON API, host sampling and terminal monitoring |
| `doc/design/EDGE-DEVICES.md`, `doc/research/` | Proposed endpoints and dated hardware research |
| `CONTRIBUTING.md`, `SECURITY.md` | Verification, contribution, and security reporting |

## Implemented software

- **Control core:** sans-I/O state machines, HMAC-authenticated multicast
  frames, bounded input validation, explicit durable-save feedback, reserved
  sequence blocks, deterministic reconciliation, and passive observation.
- **Agent shell:** serialized effects, route-generation fencing, driver
  teardown barriers, durable state files, a local single-writer state lease,
  and explicit first-install or recovery decisions.
- **Driver adapters:** simulation, IGMP membership, USB/IP host imports,
  Just Add Power channel control, and supervised external stream consumers.
  Vendor transcripts and mocks establish adapter contracts, not hardware
  compatibility. A generic `active` state is backend-confirmed readiness,
  not an independent measurement of media or physical output. USB/IP imports
  into the local host; a physical USB output to another computer requires a
  separate gadget/proxy backend.
- **Media reference:** synthetic source patterns, raw transport, ratio
  models, packetization, bounded FEC/jitter/reassembly, passive observers,
  deterministic faults, and real UDP multicast/unicast runners. `jpegxs` and
  `vc2` are model names; neither invokes a production codec. Live transport
  supports `none` and duplicate-path FEC; XOR is an offline model.
- **Reproduction tools:** an installed control fault campaign, repeated
  software benchmarks, and a local static documentation builder with link,
  anchor, asset, and deterministic-output checks.
- **Updates:** standalone Linux release staging, activation, readiness,
  rollback and crash recovery; a separate ESP32-S3/W5500 Ethernet OTA
  reference with locally provisioned mutual TLS and dual app slots. The
  ESP32 reference does not implement IR routing. Hardware acceptance for
  both update paths remains outstanding; see ADRs 0018 and 0019.
- **Housing:** a Radxa ZERO 3E R1 first article was submitted for printing.
  The separately branded R2 changes exterior identity only and has not
  been printed. No physical fit, strength or thermal result is established.

The agent never receives media payload. Separate reference media tools may
process unprotected synthetic content. The full HDCP boundary remains
ADR 0011; no component may decrypt, hold HDCP secrets, alter HDCP messages
or capabilities, or instruct endpoints to weaken protection.

## Verification and publication

```sh
make verify
make simulate
make benchmark-control
make benchmark-media
make docs-serve
```

The Makefile creates `.scratch/venv`; `VENV=.scratch/dev` can reuse an
existing developer environment. All generated output, caches, and test
temporaries belong under `.scratch`. Runtime code is stdlib-only, with
pinned developer tooling in the `dev` extra. Preserve any pre-existing
user environment outside `.scratch`.

CI runs the gate and an isolated wheel smoke test on Python 3.12 and 3.14,
plus a pinned ESP32 SDK target build and complete firmware host tests.
The `feature/ir` workflow publishes `.scratch/site` only after all gates
pass. GitHub Pages must use the GitHub Actions publishing source. There
are no runtime CDN assets or analytics. Canonical `doc/` Markdown feeds
the site directly; ADRs and new design pages appear automatically.

Socket tests may skip where sockets are unavailable; a passing run with
such skips does not establish the real-socket layer. Synthetic simulation
latency, Python benchmark timing, and historical real-codec timings are
separate forms of evidence. None establishes physical glass-to-glass
latency.

## Remaining engineering work

Hardware work begins with the unresolved endpoint choices: monitor/connector
compatibility, the GPU DSC policy, accessible bridge silicon/firmware,
clock-locked transport, and real USB device behavior. The roadmap's vendor
questions and bench milestones remain pending. No simulated outcome should
be used to mark those complete.

YWP/1 retains one site-wide key. Any key holder can impersonate origins and
author intents. Per-device identity, enrollment, and source authorization
need a separately designed protocol version and ADR; current parser and
DoS hardening do not provide that authority model.

For each new field failure, retain a deterministic regression with the fix.
Protocol changes begin in the normative document and an ADR. Hardware
assumptions stay isolated behind driver seams and labeled until a bench
result establishes them.
