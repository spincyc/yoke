# Monitor and integrate

The monitors show status without changing pairings. They are optional;
boxes do not need a monitor, Thermite, or Prometheus to keep working.

## Inspect a connection

```sh
yoke status
yoke snoop
```

`status` shows the latest messages from the boxes. `snoop` shows new
messages as they arrive. Add global
`--interface <local-ip>` and `--psk-file <path>` options before the command
when your test network requires them.

## Use the terminal dashboard

```sh
yoke top --demo --once
yoke top --demo
```

The first command prints a plain-text preview. The second opens an
interactive monitor with sample data. Use arrows or `j`/`k` to select a
device, `s` to sort, `?` for help, and `q` to exit.

For live devices, omit `--demo` and select the control interface and key:

```sh
yoke --interface 192.0.2.10 --psk-file /etc/yoke/psk top
```

The dashboard labels the machine it samples **COLLECTOR HOST**. Those CPU,
memory, network, and temperature values describe the monitor’s host.
These are not measurements of a remote Yoke box.

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
no commands for changing pairings or uploading firmware. Closing it
does not stop the boxes.

Reuse the exporter from another terminal to avoid a second collector:

```sh
yoke top --url http://127.0.0.1:9754
```

<span id="interpret-the-evidence" class="legacy-anchor" aria-hidden="true"></span>
## Read the status

A box is shown as online when the monitor has heard from it recently.
Its connection state is the last state reported by its software. Check
the actual USB device or screen to confirm it works. Missing measurements
are shown as unknown, not zero.

The [telemetry reference](../../../doc/design/TELEMETRY.md) defines fields,
metric names, retention, trust, resource limits, and exporter settings.
