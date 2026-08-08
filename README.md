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

## Layout

| Path | Contents |
|---|---|
| `doc/design/ARCHITECTURE.md` | System architecture and rationale |
| `doc/design/PROTOCOL.md` | Yoke Wire Protocol v1 (normative) |
| `doc/decisions/` | Architecture decision records |
| `doc/research/` | Hardware research: which endpoints to buy |
| `src/yoke/core/` | Sans-I/O protocol core (stdlib only) |
| `src/yoke/net/` | Asyncio multicast shell |
| `src/yoke/drivers/` | Media-plane drivers (sim, igmp, usbip, …) |
| `src/yoke/` | `cli.py` (yoke), `snoop.py` (decoder) |
| `tests/` | Offline test suite incl. simulated lossy network |

## Development

```
make verify        # ruff + mypy --strict + pytest
```

The runtime has **zero third-party dependencies** — agents must run on
small PoE-powered boxes. Dev tools (ruff, mypy, pytest) come from
`pyproject.toml` extras.
