# Endpoint backends

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

A physical USB output that presents the imported device to a separate
computer requires a gadget/proxy backend; that backend is unimplemented.
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

The [endpoint roadmap](../../../doc/design/EDGE-DEVICES.md) describes
proposed dedicated video and USB hardware. Candidate backends named in
that roadmap are not implemented simply because they appear in a design
table. The [project status](../docs/project-status.md) is the concise
capability map.

## Add a backend

Read the [architecture](../../../doc/design/ARCHITECTURE.md#drivers)
and the [contributor guide](../../../CONTRIBUTING.md). Keep device I/O in
the driver, report explicit attachment evidence, and cover failure and
teardown with deterministic scripted transcripts.
