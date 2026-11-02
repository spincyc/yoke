# ADR 0020: Optional read-only telemetry and terminal monitoring

Date: 2026-11-02. Status: accepted.

## Context

Endpoint metrics must be usable by Thermite and independent monitoring tools
without making monitoring part of pairing, routing or device availability.
Thermite already generates Prometheus scrape configuration. YWP/1 already
multicasts bounded endpoint state, events and backend readiness. Adding
resource samples to its 1,200-byte frames would consume the route-description
budget and create more control traffic.

## Decision

Add an optional Python standard-library telemetry process. It passively
observes existing control announcements using the same authenticated
`Snooper`; it has no send path and never instantiates a driver or reads media
payload. Its absence or failure cannot change pairing or media operation.
There are no new YWP/1 fields, messages, discovery broadcasts or heartbeats.

The process samples its own Linux host at a bounded interval, separately
from observed endpoint state. Host CPU, memory, network and temperature
describe the **collector host**, including when it fronts a remote appliance.
An optional explicit local node ID annotates this association; it does not
turn host measurements into appliance measurements. Missing/unsupported
measurements are unknown, never fabricated zeros. No subprocesses or shell
scripts collect samples.

Explicit `yoke metrics` invocation exposes cached read-only Prometheus text
0.0.4 at `/metrics` and a versioned JSON snapshot at `/v1/snapshot`. Bind
defaults to IPv4 loopback port 9754; a non-loopback bind requires an explicit
exposure option. This endpoint contains diagnostic metadata and has no
authentication or command API: use a trusted management network or an
operator-managed authenticated TLS proxy for remote access. It never uses
the control PSK as an HTTP credential.

Collection and serialization happen once per sample interval, not per scrape.
HTTP requests read an immutable cached representation, with bounded clients,
request deadlines and response sizes. Counters are cumulative during retained
observation, with resets on process restart or peer eviction. Label values
come from bounded enums and endpoint IDs; names, errors, pairing targets,
versions and command IDs never become metric labels. State gauges describe
the last accepted announcement; liveness is separate and expires during
silence. Lost observations cannot establish historical device event totals.

`yoke top` shares the same model and collection code. A pure renderer and
bounded history drive a curses display with CPU/memory/network panels and
an endpoint table. The interface supports resize, selection, sorting,
pause, help and clean terminal restoration. Demo and one-shot modes work
without devices; an explicit HTTP URL reuses an existing collector. All
new tooling and scripts are Python. No terminal framework, broker, database,
cloud account, OpenTelemetry collector or management service is required.

Thermite optionally scrapes the standard endpoint. It does not import Yoke,
own its lifecycle, change configured pairings, or require telemetry for
home-control readiness. Third-party Prometheus and JSON clients have the
same access.

## Alternatives

Prometheus pull preserves scrape health and avoids a Pushgateway's extra
lifecycle and stale-series management. OTLP can be added by an external
collector later; its SDK, exporter and collector costs are unnecessary for
this bounded stdlib-only reference. Existing node_exporter remains appropriate
for comprehensive host/process monitoring; Yoke exposes only a small useful
subset and labels its scope explicitly.

See [telemetry design and research](../design/TELEMETRY.md) for the schema,
commands, sources and measured collection costs.
