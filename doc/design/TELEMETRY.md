# Endpoint telemetry and terminal monitoring

Yoke monitoring is optional and read-only. Pairing, reconciliation and media
keep operating when the monitor, Prometheus or Thermite is absent. The same
Python model feeds a terminal dashboard, a JSON API and a Prometheus exporter.
No broker, central registry, cloud account or third-party runtime package is
required. [ADR 0020](../decisions/0020-optional-read-only-telemetry.md) records
the boundary.

## Research and choice

Research checked against primary sources on 2026-11-02:

| Approach | Decision for Yoke |
| --- | --- |
| Prometheus HTTP pull | Implement. Thermite already supports it; an ordinary scrape also reports whether the exporter itself is reachable. Cache the rendered response between samples. |
| Versioned JSON snapshot | Implement alongside Prometheus. A UI or third-party manager can read structured endpoint details without parsing time-series text or importing Yoke. |
| OpenTelemetry / OTLP | Keep as an external adapter option. Its richer data model supports cumulative/delta streams, but adding an SDK/exporter or collector is unnecessary for this small stdlib-only device reference. |
| Pushgateway | Do not require it for continuously running devices. Its series lifecycle and failure behavior add management work that ordinary pull avoids. |
| Extra multicast telemetry frames | Do not add them. Existing authenticated announcements/events already describe endpoint state; resource samples must not consume YWP/1's routing-description budget. |

Prometheus documents its [text exposition format](https://prometheus.io/docs/instrumenting/exposition_formats/),
[metric units and bounded labels](https://prometheus.io/docs/practices/naming/),
and [Pushgateway lifecycle limitations](https://prometheus.io/docs/practices/pushing/).
The [OpenTelemetry data model](https://opentelemetry.io/docs/specs/otel/metrics/data-model/)
informs the cumulative-counter choice. These sources support the transport
and representation decisions; the Yoke schema and implementation are original.

The TUI uses Python's optional [curses module](https://docs.python.org/3.12/library/curses.html).
Its resource panels, histories and endpoint table are inspired by
[btop](https://github.com/aristocratos/btop); no btop code or dependency is used.
Sampling is separate from drawing, and terminal updates are batched.

## Run locally

```sh
# Hardware-free, network-free dashboard preview.
yoke top --demo --once
yoke top --demo

# Observe a control VLAN and sample this collector's Linux host.
yoke --interface 192.0.2.10 --psk-file /etc/yoke/psk top

# Explicitly start the optional loopback-only HTTP exporter.
yoke --interface 192.0.2.10 --psk-file /etc/yoke/psk metrics

# Reuse an exporter, avoiding another multicast observer and host sampler.
yoke top --url http://127.0.0.1:9754

# Scriptable snapshot; stdout contains JSON only.
yoke metrics --once --format json
```

The default sample interval is one second; `--interval` accepts 0.2–60
seconds. One-shot live collection waits 2.5 seconds by default, configurable
with `--window`. `top --once --width 100 --height 30` produces plain text
without terminal escape codes. Interactive mode needs a terminal with curses.

Keys: arrows or `j`/`k` select an endpoint; `s` changes sort; Space pauses
the display while collection continues; `?` shows help; `q` or Escape quits.
Resize adapts the layout. Source failures are displayed explicitly rather
than presenting cached data as a fresh live observation.

`--demo` is synthetic and labeled DEMO. It reads no device sockets or host
resource files. Current YWP/1 endpoints are USB and HDMI. IR routing and its
device-specific counters are not implemented; the monitor does not invent
an IR device or report unavailable measurements as zero.

## Collection scope

One passive socket receives the existing control multicast group. It never
sends application datagrams, instantiates a driver or joins a media group.
Normal kernel IGMP membership traffic still occurs. With a site key, the
same authentication and replay checks as `yoke snoop` govern observations.
Open mode remains explicitly unauthenticated. Invalid packets contribute
only to aggregate verdict counters, not spoofed per-device series.

Endpoint liveness means an accepted announcement within 6.5 seconds. It is
separate from the last announced `idle`, `pending`, `active` or `error`
state. Backend `active` does not prove physical output or payload flow.
Observation counts are cumulative only while that device is retained by
this collector; restart or eviction resets them. Lost packets and events
before observer startup cannot be recovered as lifetime totals. Devices
are forgotten after the existing five-minute observation retention period,
and storage is capped by the existing 4,096-node bound.

Host panels always say **COLLECTOR HOST**. They describe the machine running
the monitor, including when that machine is a warden for a remote appliance.
`--local-node usb-desk` records an explicit association; it does not prove
that an appliance's CPU or temperature was measured. Onboard Linux devices
can run their own exporter for host resources. Existing node_exporter is
preferable when comprehensive OS/process telemetry is required.

The small host sampler reads bounded `/proc` and `/sys` files for CPU,
memory, swap, interface counters and thermal zones. The first CPU/network
rate, missing files and counter resets produce unknown values. Network
bytes are **host NIC traffic**, while device control bytes are **accepted
YWP/1 traffic**; neither is an HDMI or USB media-throughput measurement.

## Export contract

Explicit `yoke metrics` invocation creates these HTTP routes:

| Route | Representation |
| --- | --- |
| `/metrics` | Prometheus text 0.0.4, UTF-8, `HELP`/`TYPE`, cumulative counters and gauges |
| `/v1/snapshot` | JSON with `schema_version: 1`, `uptime_seconds`, `authentication`, `devices`, `observer`, and nullable `host` |

Both responses are serialized together once per sample and served from an
immutable cache. Scraping performs no host reads, device queries or event
reprocessing. There is no command/upload API. Requests and client response
sizes are bounded. JSON unknown fields are ignored for compatibility;
invalid types, nonfinite values, duplicate keys and unsupported schema
versions are rejected by the supplied Python client. The snapshot's
`sample_interval_seconds` advertises its cadence; a remote display detects
non-advancing samples using that interval, independently of HTTP success.
Older snapshots without the field use a conservative 60-second cadence.
The client bounds network transport work with one deadline; DNS resolution
follows the operating system's resolver policy.

| Metric | Meaning |
| --- | --- |
| `yoke_device_info{node_id,kind,role}` | Stable endpoint identity dimensions |
| `yoke_device_up{node_id}` | Recent accepted announcement, independently of backend state |
| `yoke_device_state{node_id,state}` | Four fixed state gauges for the last announcement |
| `yoke_device_last_seen_age_seconds{node_id}` | Monotonic announcement age at sample time |
| `yoke_device_control_frames_total{node_id}` | Accepted observed frames from an announced endpoint |
| `yoke_device_control_bytes_total{node_id}` | Bytes in those control datagrams, not media |
| `yoke_device_events_total{node_id,event}` | Observed events with fixed vocabulary and an `other` bucket |
| `yoke_observer_datagrams_total{verdict}` | Aggregate accepted/rejected observation counters |
| `yoke_observer_uptime_seconds` | This collection process's elapsed time |
| `yoke_host_*` | Available collector-host CPU, memory, NIC and temperature measurements |

Names, error text, pairing targets, versions and operation IDs are available
in JSON where applicable; they are never metric labels. There are no
per-packet time series or exported payload bytes. Unknown gauges are omitted
from Prometheus and represented as null in JSON. Use `rate(..._total[5m])`
to derive rates; don't infer packet loss from gaps in YWP sequence numbers,
which include intentionally skipped durable reservations.

## Thermite and third-party integration

A single collector on a control VLAN can export observed state for all its
endpoints without adding new endpoint transmissions. Thermite optionally
scrapes that collector using `metrics.yoke_targets` in its host configuration:

```yaml
metrics:
  yoke_targets:
    - "127.0.0.1:9754"
```

Thermite uses its existing 30-second scrape interval and generates an
optional Yoke dashboard. The exporter still samples once per second for a
smooth local/remote TUI. Monitoring does not become a home-control readiness
requirement. Other Prometheus users can configure an ordinary static target
with path `/metrics`; other applications can request `/v1/snapshot` or use
`yoke.telemetry.client.SnapshotClient`.

HTTP defaults to `127.0.0.1:9754`. To expose metadata on an explicit trusted
management address, use `--listen 192.0.2.10 --expose`. The endpoint has no
authentication; restrict it to the intended monitoring network or place it
behind an operator-managed authenticated TLS proxy. The client verifies HTTPS
normally and does not follow redirects. Never use the control PSK as an HTTP
credential. Run the exporter under your chosen service supervisor for
unattended use.

## Reuse and verification

`telemetry.model` owns the versioned snapshot; `collector` folds accepted
control observations; `host` samples OS counters; `prometheus` renders the
standard representation; `http` caches/serves it; `client` validates remote
JSON; `runtime` owns the passive worker; `tui` renders and handles keys.
`demo` supplies deterministic hardware-free samples. `telemetry.cli` only
adapts command arguments. The control core and endpoint runner are unchanged.

Run `make verify` for deterministic parsing, sampling, rendering, loopback
HTTP/multicast and CLI tests. Firmware crypto tests may require the separate
pinned SDK gate described in [ESP32 updates](ESP32-UPDATES.md). Software
performance measurements must distinguish sample/serialization work from a
cached HTTP read; they do not establish Radxa or ESP32 resource overhead.

### Software measurements

On 2026-11-02, CPython 3.14.9 on x86-64 Linux, 20 repeats of a synthetic
healthy fleet (one accepted USB announcement per device, no events or host
measurements in the fleet encoding workload):

| Retained devices | Sample, validate and encode median | JSON bytes | Prometheus bytes |
| --- | --- | --- | --- |
| 32 | 0.497 ms | 7,341 | 16,984 |
| 256 | 3.702 ms | 57,606 | 127,701 |

The cached-representation pointer read measured about 0.056 microseconds;
this excludes HTTP, TLS and network costs. Separate local `/proc`/`/sys`
sampling measured about 0.45 ms median over 100 samples. These are workstation
software measurements, not hardware endpoint budgets or throughput results.
Reproduce fleet measurements with:

```sh
python benchmarks/telemetry.py --nodes 32 --repeat 20
python benchmarks/telemetry.py --nodes 256 --repeat 20
# Or: make benchmark-telemetry NODES=32 REPEAT=20
```

There is an existing scaling limit: `Snooper` scans retained caches during
packet admission. At the full 4,096-node bound, per-datagram observation cost
is materially higher than for a small fleet; cached exposition does not
remove that cost. Measure large fleets and event storms on their intended
collector host before choosing a capacity. The upper bound is a memory and
validation limit, not a promised small-board operating point. This pass
preserves the existing snooper's expiry behavior rather than changing the
protocol observer to make a benchmark faster.
