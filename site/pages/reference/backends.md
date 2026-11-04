# USB/IP and video adapters

This page describes adapters already in the code. The USB product is the
[two-box ZERO 3E hardware design](../../../doc/design/USB-ENDPOINT.md).
The Linux USB/IP adapter below is one part of its development, not the
product wiring diagram.

A driver translates a saved pairing into a specific endpoint action.
The agent configures the backend; it never carries or inspects its media
payload. Select a backend that understands the producer’s media
description and the consumer’s role.

## Available adapters

| Driver | Role | What it does | What `active` establishes |
|---|---|---|---|
| `sim` | Producer and consumer | Scriptable in-memory behavior for offline development | The simulated attachment completed |
| `jap` | HDMI producer and consumer | Advertises a producer channel or selects a Just Add Power decoder channel | The configured channel was confirmed |
| `igmp` | HDMI consumer | Holds multicast group membership for an external renderer | Membership was joined |
| `stream` | HDMI consumer | Supervises an external GStreamer receiver process | The worker is running |
| `usbip` | USB producer and consumer | Exports local devices or imports remote devices into Linux | Backend export/import checks passed |
| `usb-gadget` | USB consumer (computer-side box) | Supervises the separate gadget worker that presents the producer's device through the OTG port | The worker and the UDC both report `configured` |

`active` is backend-specific evidence. None of these adapters independently
proves a working physical display or a successful USB application.
Hardware API assumptions and device compatibility still need bench tests.

## HDMI

Use the repository’s
[producer](../../../examples/agent-producer-jap.toml) and
[consumer](../../../examples/agent-consumer-jap.toml) configs as starting
points for the JAP adapter. The
[stream consumer example](../../../examples/agent-consumer-stream.toml)
supervises an external receiver. It requires the GStreamer tools and
device/display permissions described in the
[media reference](../../../doc/design/MEDIA.md).

The IGMP reference manages membership only. Provide a separate renderer;
joining a multicast group does not decode video.

## USB/IP

The [USB/IP consumer example](../../../examples/agent-consumer-usbip.toml)
imports exported devices into the **consumer host’s Linux USB stack**.
It requires Linux USB/IP tooling, kernel support, and narrowly scoped
device privileges. The Linux kernel carries the USB payload.

This Linux import path is a reference and bench mode. The product's
computer-side box uses the `usb-gadget` driver instead: a separate worker
imports the device with a userspace USB/IP client and presents it to the
user's computer through the OTG port with Raw Gadget. See the
[computer-side example](../../../examples/agent-consumer-usb-gadget.toml),
the [USB data path](../../../doc/design/USB-DATAPATH.md) and ADRs
[0021](../../../doc/decisions/0021-userspace-usbip-client-and-raw-gadget-presentation.md)
and [0022](../../../doc/decisions/0022-gadget-worker-process-and-metadata-driver.md).
It passes a pure-software end-to-end simulation; it has not run on
hardware. Device-box privileges and data-plane admission are in
`packaging/zero3e/` and
[ADR 0024](../../../doc/decisions/0024-usbip-data-plane-admission-and-device-box-privileges.md).
YWP/1 stores a route per consumer and has no distributed source-owner
transaction. USB/IP refuses simultaneous device attachment, and the
reference driver reconciles its locally owned imports. Plan a single
consumer for each exported device.

[ADR 0014](../../../doc/decisions/0014-usbip-owned-reconciliation.md)
records import ownership and recovery limits.

## IR and proposed hardware

There is no IR routing driver. The
[ESP32 firmware reference](../../../doc/design/ESP32-UPDATES.md) implements
an updater, not IR learning, routing, or emission.

The [endpoint roadmap](../../../doc/design/EDGE-DEVICES.md) preserves earlier video and USB3 research. The
[USB hardware design](../../../doc/design/USB-ENDPOINT.md) records the
selected ZERO 3E platform and USB 2 first scope. Candidate backends named in
that roadmap are not implemented simply because they appear in a design
table. The [project status](../docs/project-status.md) is the concise
capability map.

## Add a backend

Read the [architecture](../../../doc/design/ARCHITECTURE.md#drivers)
and the [contributor guide](../../../CONTRIBUTING.md). Keep device I/O in
the driver, report explicit attachment evidence, and cover failure and
teardown with deterministic scripted transcripts.
