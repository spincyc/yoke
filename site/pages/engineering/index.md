# Engineering

The [USB hardware design](../../../doc/design/USB-ENDPOINT.md) records
the selected two-box ZERO 3E prototype and USB 2 first scope. These pages
also cover software, tests, and earlier hardware research.

## Understand the software

| Read | For |
|---|---|
| [Architecture](../../../doc/design/ARCHITECTURE.md) | Endpoint state, agent placement, module boundaries, and failure behavior |
| [Wire protocol](../../../doc/design/PROTOCOL.md) | The normative control contract |
| [Simulation](../../../doc/design/SIMULATION.md) | Repeatable control faults and the installed harness |
| [Media reference](../../../doc/design/MEDIA.md) | Synthetic frames and the separate media transport |
| [Telemetry](../../../doc/design/TELEMETRY.md) | Optional observers, resource sampling, and export formats |

## Reproduce the evidence

| Read | For |
|---|---|
| [Verification](../verification.md) | Local gate, test layers, fault campaigns, and site checks |
| [Media testing](../../../doc/design/MEDIA-TESTING.md) | Loopback before two-host tests |
| [Software performance](../../../doc/design/PERFORMANCE.md) | Workloads, reproduction commands, and measured software costs |
| [Contributing](../../../CONTRIBUTING.md) | Setup, change boundaries, and useful failure reports |

## Develop the endpoints

| Read | For |
|---|---|
| [USB hardware design](../../../doc/design/USB-ENDPOINT.md) | Two ZERO 3E boxes, physical USB at each end, and remaining firmware work |
| [Earlier HDMI/USB3 study](../../../doc/design/EDGE-DEVICES.md) | Earlier alternatives and video research; superseded for the USB prototype |
| [USB housing design](../../../doc/design/USB-HOUSING.md) | Enclosure requirements and revision history |
| [R4 case candidate](../../../hardware/usb-housing/r4/README.md) | Sliding joint, recessed M3 screws and heat-set inserts; physical fit untested |
| [Retired R3 assembly](../../../hardware/usb-housing/r3/README.md) | Historical failed revision; do not print |
| [Linux updates](../../../doc/design/UPDATES.md) | Operator-controlled releases, readiness, rollback, and recovery |
| [ESP32 updater](../../../doc/design/ESP32-UPDATES.md) | Ethernet firmware-update reference for future IR endpoints |
| [Brand identity](../../../doc/design/BRANDING.md) | Shared screen and enclosure mark |

## Follow the reasoning

[Architecture decisions](decisions.md) preserve the rationale for protocol,
security, persistence, driver, and operational choices.
The [research archive](research.md) preserves dated feasibility,
compression, and hardware studies. See [project status](../docs/project-status.md)
for current capabilities before treating a historical proposal as an
available endpoint.
