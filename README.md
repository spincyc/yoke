# <img src="site/assets/yoke-lockup.svg" width="264" height="64" alt="Yoke">

**Controllerless pairing for HDMI and USB endpoints over switched Ethernet.**

Yoke puts pairing state at the endpoints. A producer can be paired with a
consumer, then re-paired at runtime. The CLI sends a signed multicast intent,
waits for the consumer's acknowledgment, and exits. Each endpoint persists
its own state; peers exchange observations and reconcile versions without a
resident central controller.

[Documentation](https://spincyc.github.io/yoke/) ·
[Start with the software](site/pages/start.md) ·
[Contributing](CONTRIBUTING.md) · [Security](SECURITY.md)

```sh
yoke pair cam-garage tv-den
yoke unpair tv-den
yoke status
yoke snoop
```

## Project status

Yoke is an early software reference and endpoint engineering project. The
repository implements the control plane, driver adapters, deterministic
simulators, and a synthetic media reference pipeline. **No real endpoint
hardware has been bench-tested.** The 4K120-over-10GbE designs and physical
latency goals remain proposed engineering paths, not demonstrated results.

| Available in this repository | Requires integration or bench validation |
|---|---|
| Sans-I/O pairing state machines and authenticated YWP/1 frames | Actual endpoint device APIs and hardware behavior |
| Durable state transitions, driver orchestration, and passive observers | HDMI capture/display, real USB peripherals, and fabric behavior under load |
| Seeded fault simulations and real-socket loopback tests | Production JPEG XS, VC-2, DSC, or other real codecs |
| Synthetic packetization, FEC, jitter, and integrity tools | Physical latency, protected-content relay timing, and hardware throughput |

The dated [feasibility study](doc/research/FEASIBILITY.md),
[hardware survey](doc/research/HARDWARE.md), and
[endpoint roadmap](doc/design/EDGE-DEVICES.md) preserve assumptions and open
questions. Product availability and performance estimates in that research
have not been revalidated for publication.

## Design principles

- **State at the edge.** Endpoint agents own durable pairings. Conflicting
  intents have a total version order, and peers reissue newer state after a
  partition or missed intent.
- **Separate control and payload.** The agent never receives or inspects
  media. Drivers configure endpoint hardware or external processes. Media
  reference tools run separately and use unprotected synthetic content.
- **Observe independently.** `yoke snoop` passively verifies control traffic.
  The media observer independently joins the reference multicast stream and
  sends no application datagrams or feedback.
- **Reproduce failures offline.** Pure state machines receive explicit time
  inputs. Tests exercise loss, duplication, reordering, partitions, replay,
  persistence errors, and driver failure.

YWP/1 uses a site-wide PSK. Any key holder can author control traffic; this
is a shared trust domain, not per-device authorization. Control metadata is
visible. See [SECURITY.md](SECURITY.md) for that boundary. Generic driver
`active` status means backend-confirmed readiness, not independently
measured media flow or a working physical display.

## Try the software

Python 3.12 or later is required. The runtime has zero third-party
dependencies.

```sh
python3 -m venv .scratch/run
.scratch/run/bin/python -m pip install .
.scratch/run/bin/yoke-media probe --frames 120
.scratch/run/bin/yoke-media simulate --frames 120 --drop 0.05 --fec xor --seed 7
```

`raw` verifies synthetic model bytes exactly. `jpegxs` and `vc2` are
**ratio models**: they preserve the test payload and model compression in
metadata. They do not implement those codecs or establish visually lossless
quality. Simulation timing is model time; Python loopback and benchmark
timing measure software costs on the host, not hardware glass-to-glass
latency. The [performance report](doc/design/PERFORMANCE.md) records measured
software workloads and reproduction commands.

Run the installed control-plane fault campaign with `yoke simulate --json`.
It needs no network, hardware, or management service. See the
[simulation guide](doc/design/SIMULATION.md) for its scenarios and limits.

A live receiver and independent observer can join the same multicast group:

```sh
# Separate terminals; prove the local gate below before a two-host run.
make media-recv INTERFACE=127.0.0.1
make media-send INTERFACE=127.0.0.1 FRAMES=600
.scratch/venv/bin/yoke-media snoop --live --interface 127.0.0.1
```

Multicast sends to `GROUP`, not `PEER`. For unicast, set `UNICAST=1` on
both Make targets and `PEER=<receiver-ip>` on the sender. See the
[media transport contract](doc/design/MEDIA.md) and
[testing guide](doc/design/MEDIA-TESTING.md).

## Develop and verify

```sh
make verify                # format, lint, strict types, tests, strict docs build
make simulate              # seeded control-plane fault campaign
make benchmark-control     # repeated control-plane software measurements
make benchmark-media       # repeated synthetic media measurements
make docs-serve            # docs at http://127.0.0.1:8000
```

Tools, caches, test temporaries, and generated docs live under `.scratch`.
Use `PYTHON=python3.12` to select an interpreter or `VENV=.scratch/dev` to
reuse a developer environment. The pinned `dev` extra includes the Markdown
renderer; it adds no runtime dependency. CI verifies Python 3.12 and 3.14,
smoke-tests an isolated wheel install, and publishes the verified static
site from `feature/ir` after both Python jobs and the separate ESP32
firmware build/test gate pass.

## Endpoint prototypes

The [USB housing guide](doc/design/USB-HOUSING.md) contains the screw-closed
Radxa ZERO 3E enclosure, assembly instructions, and a separate branded R2
CAD revision. Physical fit and thermal testing remain outstanding.

[Linux endpoint updates](doc/design/UPDATES.md) use operator-controlled
SSH/SFTP delivery, immutable releases, readiness checks, and rollback.
The [ESP32 Ethernet updater](doc/design/ESP32-UPDATES.md) provides a
separate ESP32-S3/W5500 reference after initial USB provisioning; IR routing
is not implemented. Both update paths require device bench validation.
The [identity guide](doc/design/BRANDING.md) defines the shared screen and
enclosure mark, colors, and typography.

## Repository map

| Path | Contents |
|---|---|
| [doc/design/ARCHITECTURE.md](doc/design/ARCHITECTURE.md) | State ownership, control plane, and driver boundary |
| [doc/design/PROTOCOL.md](doc/design/PROTOCOL.md) | Normative Yoke Wire Protocol v1 |
| [doc/design/MEDIA.md](doc/design/MEDIA.md) | Synthetic media reference and transport |
| [doc/decisions/](doc/decisions/) | Architecture decision records |
| [doc/research/](doc/research/) | Dated hardware, compression, and feasibility research |
| `src/yoke/core/` | Sans-I/O control state machines |
| `src/yoke/net/`, `src/yoke/drivers/` | Network shell and endpoint adapters |
| `src/yoke/media/` | Reference media stages, observers, runners, and CLI |
| `tests/`, `benchmarks/` | Reproductions, seeded faults, socket checks, and measurements |
| `site/`, `scripts/build_docs.py` | Static documentation source, shell, and strict builder |

MIT licensed. The agent never decrypts protected content, holds HDCP
secrets, alters HDCP messages or capabilities, or instructs endpoints to
weaken protection. The full media boundary is recorded in
[ADR 0011](doc/decisions/0011-media-and-hdcp-scope-for-yoke-endpoints.md).
