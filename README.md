# <img src="site/assets/yoke-lockup.svg" width="264" height="64" alt="Yoke">

**Put your devices where you need them.**

Yoke is an open hardware and software project for connecting HDMI sources,
USB peripherals, and future IR endpoints over Ethernet. Choose a source,
pair a destination, and let the endpoints remember the connection.
No central controller or management service needs to keep running.

[Explore Yoke](https://spincyc.github.io/yoke/) ·
[Get started](site/pages/start.md) ·
[Documentation](site/pages/docs/index.md) ·
[Contributing](CONTRIBUTING.md)

## The connection belongs to the endpoints

Each endpoint has its own agent and saved state. A command selects a source
for a destination:

```sh
yoke pair cam-garage tv-den
yoke status
yoke unpair tv-den
```

The consumer saves its pairing before acknowledging it, then asks its
driver to connect. Restarting an agent restores the saved route. A missing
source leaves the pairing in place while the consumer waits for its return.
The agent configures the connection and never receives or inspects its
media payload.

Use the CLI on its own, or add the optional terminal dashboard,
Prometheus exporter, Thermite, or another manager. Closing a monitor does
not change the pairing.

## Run the software today

Yoke is in development. The repository includes working control software,
endpoint driver adapters, deterministic fault simulation, passive monitors,
and a synthetic media pipeline. Complete endpoint hardware has not been
bench-tested.

The [project status](site/pages/docs/project-status.md) maps current
capabilities and remaining work. In particular, USB/IP currently imports
devices into a Linux consumer's USB stack; a physical USB gadget/proxy
output and IR routing remain unimplemented.

Use Python 3.12 or later. From a checkout:

```sh
python3 -m venv .scratch/run
.scratch/run/bin/python -m pip install .
.scratch/run/bin/yoke simulate --json
.scratch/run/bin/yoke top --demo --once
.scratch/run/bin/yoke-media probe --frames 120
```

These examples need no hardware, sockets, or management service. The
runtime uses the Python standard library only. The simulator uses the
real pairing state machines with virtual time and simulated drivers.
Media checks use synthetic frames; `jpegxs` and `vc2` are ratio models,
not codec implementations.

For real devices, choose a [backend](site/pages/reference/backends.md),
follow [deployment](doc/design/DEPLOYMENT.md), then use the
[pairing guide](site/pages/guides/pairing.md). A saved pairing and
backend-reported `active` state are distinct from measured physical output.

## Find your path

| Read | For |
|---|---|
| [Connections](site/pages/product.md) | HDMI, Linux USB/IP, and the planned IR direction |
| [How it works](site/pages/how-it-works.md) | Source, destination, durable pairing, and independent management |
| [Get started](site/pages/start.md) | Install and try the software on one computer |
| [Guides](site/pages/guides/index.md) | Pairing, monitoring, deployment, and maintenance |
| [Reference](site/pages/reference/index.md) | Commands, backends, configuration, and protocol contracts |
| [Engineering](site/pages/engineering/index.md) | Architecture, evidence, endpoint designs, decisions, and research |
| [Security](SECURITY.md) | Site-key trust, metadata visibility, and reporting |

## Develop and verify

```sh
make verify                       # formatting, lint, strict types, tests, docs
make browser-install verify-browser  # explicit browser install, then UI checks
make simulate                     # repeatable control-plane fault campaign
make docs-serve                   # documentation at http://127.0.0.1:8000
```

Development tools and generated files belong under `.scratch`.
See [Contributing](CONTRIBUTING.md) for setup, browser provisioning,
software measurements, and the CI gates.

## Repository map

| Path | Responsibility |
|---|---|
| `src/yoke/core/` | Pure control state machines; no sockets, clock reads, or filesystem |
| `src/yoke/net/`, `src/yoke/drivers/` | Agent I/O, serialized driver work, and endpoint adapters |
| `src/yoke/media/` | Separate synthetic media stages, runners, and CLI |
| `src/yoke/telemetry/` | Optional passive monitoring, terminal UI, and metrics |
| `doc/design/` | Canonical architecture, protocol, and subsystem documents |
| `doc/decisions/`, `doc/research/` | Decision history and dated engineering evidence |
| `site/pages/` | Product explanation, task guides, and documentation hubs |
| `scripts/docsite/`, `site/assets/` | Static-site builder and presentation |
| `tests/`, `benchmarks/` | Reproductions, simulations, socket checks, and measurements |
| `firmware/`, `hardware/` | Endpoint firmware references and mechanical designs |

MIT licensed. Protected-content boundaries are recorded in
[ADR 0011](doc/decisions/0011-media-and-hdcp-scope-for-yoke-endpoints.md).
