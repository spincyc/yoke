# Yoke

Yoke pairs HDMI and USB endpoints across an ordinary switched Ethernet
network. A **producer** (HDMI source capture box, or a box with USB devices
plugged into it) can be yoked to any single **consumer** (HDMI display
driver box, or a box attached to a computer's USB port) and re-yoked at any
time, with no central controller running anywhere.

```
yoke pair   cam-garage   tv-den        # re-route a stream
yoke unpair tv-den                     # detach a consumer
yoke status                            # who is announcing, who is paired
yoke snoop                             # passively watch the control plane
```

## Principles

- **No resident controller.** Pairing state lives in the endpoints and is
  changed by signed datagrams on a well-known multicast group. The CLI is
  ephemeral: it sends an intent, waits for the acknowledgement, and exits.
- **Transparent payload.** Yoke never decodes, re-encodes, or even parses
  the media it routes. HDMI and USB transport — including HDCP — is the
  endpoint hardware's job. Yoke is the control plane that tells endpoints
  who feeds whom.
- **Observable by design.** All control traffic, including state-change
  events from every endpoint, flows on the multicast group. `yoke snoop`
  joins passively and verifies the system without touching it.
- **Offline-testable.** The protocol core is sans-I/O: pure state machines
  driven by (frame, time) inputs. Every error condition — loss, duplication,
  reordering, partitions, conflicting intents, replay, driver failure — is
  reproduced deterministically in the test suite with no network at all.

## Media plane (software reference)

Beyond routing, yoke ships a **software AV-over-IP pipeline** that runs the
compression sweet spot (line-based, visually-lossless, ~2–4:1, sub-1 ms —
`doc/research/COMPRESSION.md`). It is built like the control
plane: sans-I/O stages (source → codec → packetize → FEC → jitter → sink)
that a passive snooper verifies **without touching the stream**, a seeded
network simulator, and a real-UDP runner. Production datapaths are
endpoint hardware or gateware (`doc/design/EDGE-DEVICES.md`) or external
processes behind drivers; this pipeline is the reference model, the test
oracle, and the debugging toolkit.

```
yoke-media simulate --drop 0.05 --fec xor   # run over a seeded lossy link, report health
yoke-media probe                            # integrity gate: exit 0 only if every frame verified
yoke-media snoop                            # passively verify a live stream, transmit nothing
yoke-media trace --frames 3                 # dump each unit's journey through every stage
yoke-media run --role receiver              # host A: real UDP, print metrics
yoke-media run --role sender --peer <A>     # host B: stream to host A
```

Verification is layered — offline sim, then real-socket loopback on one
host, then two hosts — and gated by one command (`make verify`). See
`doc/design/MEDIA.md` and `doc/design/MEDIA-TESTING.md`.

## Layout

| Path | Contents |
|---|---|
| `doc/design/ARCHITECTURE.md` | Control-plane architecture and rationale |
| `doc/design/PROTOCOL.md` | Yoke Wire Protocol v1 (normative) |
| `doc/design/MEDIA.md` | Media pipeline design; `MEDIA-TESTING.md` verification |
| `doc/decisions/` | Architecture decision records |
| `doc/design/EDGE-DEVICES.md` | Endpoint design for the 10G mesh (video + USB) |
| `doc/research/` | Feasibility verdict, hardware survey, compression analysis |
| `src/yoke/core/` | Sans-I/O control-plane core (stdlib only) |
| `src/yoke/net/` | Asyncio multicast shell |
| `src/yoke/drivers/` | Media-plane drivers (sim, igmp, usbip, jap, stream) |
| `src/yoke/media/` | Sans-I/O media pipeline, snooper, runner, `yoke-media` CLI |
| `src/yoke/` | `cli.py` (yoke), `snoop.py` (control-plane decoder) |
| `tests/` | Offline suites, seeded lossy sims, real-socket loopback |

## Development

```
make verify        # ruff + mypy --strict + pytest
```

The runtime has **zero third-party dependencies** — agents must run on
small PoE-powered boxes. Dev tools (ruff, mypy, pytest) come from
`pyproject.toml` extras.
