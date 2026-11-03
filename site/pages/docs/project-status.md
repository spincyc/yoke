# What you can use today

Yoke is an early open hardware and software project. You can run and test
the control software today. A complete Yoke hardware connection has not
been built and bench-tested.

| Area | Available now | Next boundary |
|---|---|---|
| Pairing | Endpoint agents, durable routes, authenticated control traffic, recovery, and offline fault simulation | Commission and validate your chosen endpoint backend |
| HDMI | Just Add Power adapter, multicast membership reference, and supervised GStreamer receiver adapter | Actual appliances, capture/display, and device API assumptions need bench validation |
| USB | Linux USB/IP export/import backend with local import reconciliation | Imports end in the consumer’s Linux stack; a physical USB gadget/proxy output to another computer is unimplemented |
| IR | ESP32-S3/W5500 Ethernet firmware-updater reference | IR learning, routing, and transmission are unimplemented |
| Monitoring | Passive CLI, terminal dashboard, Prometheus, and versioned JSON snapshots | Driver state and collector-host resources do not establish physical output or endpoint media throughput |
| Reference media | Synthetic frames, packetization, FEC, jitter, integrity checks, and real-socket tests | `jpegxs` and `vc2` are ratio models, not codec implementations |
| Hardware | Endpoint proposals and USB enclosure CAD/assembly documentation | Fit, thermal behavior, real peripherals, video quality, and physical latency need bench work |

The proposed 4K120-over-10GbE paths and latency goals are engineering
targets. Software model timing does not measure capture-to-display
latency. The [research archive](../engineering/research.md) retains the
dated studies behind those targets.

## Choose a useful starting point

- **No hardware:** follow [Get started](../start.md) for offline pairing,
  monitor previews, and synthetic media checks.
- **Linux or AVoIP lab:** compare [backends](../reference/backends.md),
  then use the [deployment guide](../../../doc/design/DEPLOYMENT.md).
- **Hardware development:** start with the
  [engineering map](../engineering/index.md) and preserve the distinction
  between a proposal, a software test, and a bench measurement.

Pairing is independent of Thermite, Prometheus, and other managers. A
successful pairing acknowledgment means the route was saved. Backend
`active` means that backend confirmed its attachment; check the physical
device separately. The [verification guide](../verification.md) explains
what each test layer establishes.
