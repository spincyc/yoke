# <img src="site/assets/yoke-lockup.svg" width="264" height="64" alt="Yoke">

**USB devices over Ethernet, with a hardware box at each end.**

Plug a USB device into one Yoke box and your computer into a second box.
Connect both boxes to your Ethernet switch and pair them. The boxes save
that pairing and keep it until a pair or unpair command changes it.

The USB prototype uses **two Radxa ZERO 3E boards**. **USB 2 is the first
deliverable.** HDMI hardware is planned; IR uses a separate ESP32 PoE endpoint. Thermite or another
manager can control the boxes, but is optional.

[USB hardware](site/pages/usb.md) ·
[USB design](doc/design/USB-ENDPOINT.md) ·
[Website](https://spincyc.github.io/yoke/) ·
[Documentation](site/pages/docs/index.md)

## Development status

The pairing software, stored settings, monitors and simulations exist.
The computer-side box's USB bridge, which presents the remote device
through its own USB port, is implemented. It passes a pure-software
end-to-end simulation and a real-kernel loopback test in a virtual
machine. Nothing has run on the boards yet; hardware tests are the next
step. IR firmware for an ESP32 PoE endpoint exists in
software and awaits bench validation.

The user's computer is separate from both boxes. Linux inside the boxes
is a firmware choice, not a requirement for that computer.

See [project status](site/pages/docs/project-status.md) for current work and
[the USB design](doc/design/USB-ENDPOINT.md) for the agreed hardware.

## Try the software

Use Python 3.12 or later. From a checkout:

```sh
python3 -m venv .scratch/run
.scratch/run/bin/python -m pip install .
.scratch/run/bin/yoke simulate --json
.scratch/run/bin/yoke top --demo --once
.scratch/run/bin/yoke-media probe --frames 120
```

These run without hardware or a network. They test the software and use
simulated devices; they do not establish that the physical USB bridge works.
The Python runtime uses the standard library only.

## Develop and verify

```sh
make verify                         # format, lint, types, tests, docs
make browser-install verify-browser # install the browser, then check the site
make simulate                       # repeatable pairing tests
make docs-serve                      # local documentation
```

See [Contributing](CONTRIBUTING.md) for setup and checks.

## Repository map

| Path | Contents |
|---|---|
| [doc/design/USB-ENDPOINT.md](doc/design/USB-ENDPOINT.md) | Agreed USB hardware and remaining firmware work |
| `hardware/usb-housing/` | ZERO 3E case designs and assembly instructions |
| `src/yoke/core/` | Pairing state machines |
| `src/yoke/net/`, `src/yoke/drivers/` | Networking and device adapters |
| `src/yoke/media/` | Separate media experiments and tools |
| `src/yoke/telemetry/` | Monitoring, terminal display, and metrics |
| `doc/design/` | Architecture, protocol, and subsystem documentation |
| `doc/decisions/`, `doc/research/` | Decisions and dated research |
| `site/pages/`, `site/assets/` | Website pages, styles, and drawings |
| `scripts/docsite/` | Python site builder |
| `tests/`, `benchmarks/` | Tests and measurements |
| `firmware/` | Endpoint firmware references |

MIT licensed. See [Security](SECURITY.md) and
[the protected-content rules](doc/decisions/0011-media-and-hdcp-scope-for-yoke-endpoints.md).
