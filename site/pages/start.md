# Start with the software

Yoke is a developer reference for controllerless endpoint pairing. Start
with offline tools, then test real sockets on one host. Real HDMI capture,
display, USB devices, and production codecs need separate integrations and
hardware validation.

## Install from source

Use Python 3.12 or later. A standard install has no third-party runtime
dependencies.

```sh
git clone https://github.com/spincyc/yoke.git
cd yoke
python3 -m venv .scratch/run
.scratch/run/bin/python -m pip install .
.scratch/run/bin/yoke --help
.scratch/run/bin/yoke-media --help
```

For a developer checkout, use `make verify` instead; it creates an isolated
environment with pinned development tools under `.scratch`. See the
[contributor guide](../../CONTRIBUTING.md).

## Run a repeatable media model

```sh
.scratch/run/bin/yoke-media probe --frames 120
.scratch/run/bin/yoke-media simulate --frames 120 --drop 0.05 --fec xor --seed 7
.scratch/run/bin/yoke-media trace --frames 3
```

`probe` is an integrity gate: a failed reference frame makes it exit
nonzero. A lossy `simulate` run reports what survived and what failed; it
is a diagnostic, so its exit status does not imply perfect delivery.

The source emits synthetic frame patterns. `raw` carries those bytes
exactly. `jpegxs` and `vc2` preserve the test payload and model compression
ratios in metadata. They do not encode or decode JPEG XS or VC-2. Reported
simulation delay comes from the model's injected clock and configuration;
it is not measured hardware latency.

## Inspect the control plane

```sh
.scratch/run/bin/yoke status
.scratch/run/bin/yoke snoop
```

These commands observe the configured control multicast group. With no
agents running, an empty status is expected. The snooper continues until
interrupted. Configure real endpoint agents from the repository's
[example files](../../examples) and the
[deployment guide](../../doc/design/DEPLOYMENT.md) before pairing devices.
A site uses a shared PSK; open mode belongs in isolated labs.

```sh
yoke pair cam-garage tv-den
yoke unpair tv-den
```

The CLI sends a pairing intent and waits for the consumer's acknowledgment.
That acknowledgment confirms control-plane acceptance; use the announced
state and driver status to establish whether media is active.

## Exercise real sockets

Run the [verification gate](verification.md) first. Then use two terminals
on one machine, selecting the loopback interface explicitly:

```sh
# Terminal 1: receive the reference multicast stream
make media-recv INTERFACE=127.0.0.1
```

```sh
# Terminal 2: send to the same group and interface
make media-send INTERFACE=127.0.0.1 FRAMES=600
```

On two hosts, select each host's local interface address on the same test
network. The multicast sender targets `GROUP`; `PEER` is used only when
`UNICAST=1`. A passive third process can independently observe:

```sh
.scratch/run/bin/yoke-media snoop --live --interface 127.0.0.1
```

Joining multicast uses the operating system's normal IGMP membership
traffic. Passive observation means no application media datagrams or
feedback are sent. Check `yoke-media run --help` for explicit feedback
settings and select at most one active feedback reporter per stream.

The [media design](../../doc/design/MEDIA.md) defines the reference transport.
The [endpoint roadmap](../../doc/design/EDGE-DEVICES.md) describes the
remaining hardware work.
