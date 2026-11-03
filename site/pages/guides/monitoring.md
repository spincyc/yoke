# Monitor and integrate

Choose the view you need. Every Yoke monitor is optional and read-only:
pairing and reconciliation run without it, Thermite, Prometheus, or a
cloud service.

## Inspect a connection

```sh
yoke status
yoke snoop
```

`status` collects recent endpoint announcements. `snoop` renders the
control conversation as it arrives. Add global
`--interface <local-ip>` and `--psk-file <path>` options before the command
when your test network requires them.

## Use the terminal dashboard

```sh
yoke top --demo --once
yoke top --demo
```

The first command prints a plain-text preview. The second opens an
interactive synthetic dashboard. Use arrows or `j`/`k` to select a
device, `s` to sort, `?` for help, and `q` to exit.

For live devices, omit `--demo` and select the control interface and key:

```sh
yoke --interface 192.0.2.10 --psk-file /etc/yoke/psk top
```

The dashboard labels the machine it samples **COLLECTOR HOST**. Those CPU,
memory, network, and temperature values describe the monitor’s host.
A remote appliance’s announcement is a separate source of information.

## Add Prometheus or a manager

Start the optional exporter:

```sh
yoke --interface 192.0.2.10 --psk-file /etc/yoke/psk metrics
```

It binds to loopback by default and serves cached snapshots:

| Route | Use |
|---|---|
| `http://127.0.0.1:9754/metrics` | Prometheus text metrics |
| `http://127.0.0.1:9754/v1/snapshot` | Versioned JSON for a manager or custom display |

Thermite or another manager can consume these views. The exporter exposes
no command or upload API and grants no authority over pairings. Closing
it leaves endpoint operation in place.

Reuse the exporter from another terminal to avoid a second collector:

```sh
yoke top --url http://127.0.0.1:9754
```

## Interpret the evidence

An endpoint’s **liveness** says whether its announcement was recently
observed. Its **state** is what its backend last reported. Neither is an
independent measurement of physical video output or USB application
success. Missing samples appear as unknown rather than fabricated zeros.

The [telemetry reference](../../../doc/design/TELEMETRY.md) defines fields,
metric names, retention, trust, resource limits, and exporter settings.
