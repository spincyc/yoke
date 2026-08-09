# Verifying the media pipeline

The rule: **every network path is proven over loopback on one host before
any two-host run.** The layers, cheapest first:

## 1. Offline (no sockets) — the bulk of the proof

Every stage is sans-I/O and has its own suite; the whole pipeline is
exercised over the seeded network simulator (`tests/media_simnet.py`) with
drop / dup / reorder / delay / corrupt, including FEC recovery and rate
adaptation (`tests/test_media_pipeline.py`). Deterministic, no hardware.

## 2. Loopback (real sockets, one host) — the gate before two hosts

`tests/test_media_loopback.py` runs a real `SenderRunner` and
`ReceiverRunner` over `127.0.0.1`, verifying bit-exact delivery, the
passive snooper's non-interference, malformed-datagram handling, the
feedback loop, multi-fragment reassembly, dup-path FEC, and measured
loopback latency. These skip cleanly where a sandbox forbids sockets.

Both 1 and 2 run under one command:

```
make verify        # ruff + mypy --strict + pytest (offline + loopback)
```

This is the **single-host gate**. It must be green before touching two
hosts.

## 3. Two hosts (real fabric) — one command on each

Once `make verify` is green, verify across the real network. One command
per host:

```
# host A — the display end (receiver), join the group and report:
make media-recv                      # or: yoke-media run --role receiver --snoop

# host B — the source end (sender), stream to host A:
make media-send PEER=<host-A-ip>     # or: yoke-media run --role sender --peer <A> --frames 600
```

The receiver prints end-to-end metrics (frames ok/bad, loss, FEC
recoveries, effective ratio, latency) and, with `--snoop`, the
reconstructed topology. Vary the run without editing anything:

```
make media-send PEER=10.0.0.5 ENCODING=jpegxs FEC=dup FRAMES=1200
```

## 4. Passive verification, any layer

A third, non-participating host (or process) verifies a live stream
without touching it:

```
yoke-media snoop            # join the group, render per-stream health, transmit nothing
```

This is the media analogue of `yoke snoop` for the control plane — the
conformance oracle. It reconstructs loss, reorder, dup, FEC recovery, and
frame integrity from the wire alone.

## What "green" means before hardware

`make verify` proves the control plane, the media pipeline logic, the wire
format, FEC recovery, jitter handling, rate adaptation, and the real
socket path on one host — everything except the physical media front-end
(capture/display) and the real fabric's behavior under load. Those are the
bench items in `doc/research/prototype/` (§risks). The two-host run
closes the gap to the real network; the glass-to-glass rig
(BUILD-PLAN.md §verification) closes it to physical latency.
