# Reference

Use the installed help for the exact options supported by your checkout.
Global network and key options come before the subcommand.

```sh
yoke --help
yoke pair --help
yoke-media --help
```

## Commands

| Command | Purpose |
|---|---|
| `yoke pair PRODUCER CONSUMER` | Save a consumer’s selected source |
| `yoke unpair CONSUMER` | Clear its saved pairing |
| `yoke status` | Collect recent endpoint announcements |
| `yoke snoop` | Observe the control conversation continuously |
| `yoke agent --config PATH` | Run one endpoint agent |
| `yoke simulate` | Run deterministic offline pairing scenarios |
| `yoke top` | Show the optional terminal monitor |
| `yoke metrics` | Start the optional read-only exporter |
| `yoke-media probe` | Check synthetic frame integrity |
| `yoke-media simulate` | Exercise the synthetic media fault model |
| `yoke-media run` | Run a reference sender or receiver over UDP |

## Contracts and configuration

| Reference | Covers |
|---|---|
| [Endpoint backends](backends.md) | Driver roles, requirements, and the meaning of activation |
| [Deployment](../../../doc/design/DEPLOYMENT.md) | Agent TOML, identities, site keys, state files, network, and services |
| [Example configs](../../../examples) | Starting points for backend-specific agent configuration |
| [Wire protocol](../../../doc/design/PROTOCOL.md) | Normative YWP/1 frames, authentication, persistence, and convergence |
| [Media reference](../../../doc/design/MEDIA.md) | Synthetic pipeline, framing, transport, and observation |
| [Simulation](../../../doc/design/SIMULATION.md) | Installed fault suite, Python API, and repeatable benchmarks |
| [Telemetry](../../../doc/design/TELEMETRY.md) | Terminal monitor, Prometheus metrics, and JSON schema |
| [Security](../../../SECURITY.md) | Shared-key trust and reporting |

For an operational walkthrough, use the [guides](../guides/index.md).
For design rationale, use the [engineering map](../engineering/index.md).
