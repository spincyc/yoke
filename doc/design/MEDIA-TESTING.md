# Verifying the reference media pipeline

Every network path is proven over loopback on one host before a two-host
run. All commands here exercise synthetic source bytes and codec models,
not physical HDMI capture/display, a real JPEG XS/VC-2 codec, or hardware
throughput. The media plane is separate from YWP control traffic. Multicast
here carries unprotected reference content; ADR 0011 keeps protected HDCP
ciphertext relay point-to-point.

## Offline gate

The seeded simulator covers drop, duplication, reordering, delay,
corruption, FEC recovery, jitter, and rate adaptation. It uses the same
sans-I/O stages as the socket shell. Run the complete repository gate:

```sh
make verify
```

`yoke-media probe --frames 30` gates on synthetic frame integrity;
`simulate`, `trace`, and `snoop` without `--live` run offline. `--encoding
jpegxs` and `--encoding vc2` select ratio models, not codec libraries.
XOR FEC is offline-only because no parity wire representation is defined;
live UDP accepts `--fec none` or `--fec dup`.

Simulation JSON distinguishes actual simulated wire arrivals, injected
network drops/duplicates, post-FEC data delivery, and recovered data. Loss
uses all known source identities, including never-arriving first/last
frames. Receiver reports return on a reliable modeled 0.5 ms reverse link;
rate changes are causal model behavior, not a measured hardware response.
The report adapter uses receiver packet inter-arrival jitter without
assuming source and receiver clocks align. `--mtu` is the complete IPv4
path MTU (98..65535); the packetizer reserves transport/framing overhead.
The virtual link supports time up to 3600 seconds, fault delays/jitter up
to 60 seconds, and a cumulative 100000-step budget. Unsupported inputs or
exhausted work return an error; reduce the run or timing magnitudes.
Finite runs finalize sender parity and receiver buffers before reporting,
including diagnostics for incomplete assembly.

## Single-host real sockets

`tests/test_media_loopback.py` tests unicast UDP delivery, malformed
feedback/datagrams, finite inputs, cancellation, fragmentation, duplicate
FEC, and sender-to-sink time on a shared monotonic clock.
`tests/test_media_multicast.py` adds actual IPv4 multicast on `127.0.0.1`:
sender, receiver, and an independent passive observer each own a socket.
It proves fanout, identical sender bytes and receiver counters with and
without the observer, no observer application sends, explicit designated
feedback, and socket/membership cleanup. Unsupported or forbidden socket
errors may skip; timeouts, missing data, and unrelated I/O errors fail.

To see three independent processes, run these in three terminals on one
host, starting the receiver and snooper first:

```sh
yoke-media run --role receiver --group 239.255.42.42 --port 5004 \
  --interface 127.0.0.1 --ttl 0 --loopback --duration 15 --snoop
```

```sh
yoke-media snoop --live --group 239.255.42.42 --port 5004 \
  --interface 127.0.0.1 --ttl 0 --loopback --duration 15
```

```sh
yoke-media run --role sender --group 239.255.42.42 --port 5004 \
  --interface 127.0.0.1 --ttl 0 --loopback --frames 300
```

Sender source ports are ephemeral; listeners share the destination port.
`--no-loopback` disables same-host multicast delivery. The receiver's
`--snoop` is an additional in-process tap, while `snoop --live` is the
independent wire observer. Ctrl-C closes sockets; `--duration` provides a
bounded live observation. Timestamps are actual monotonic arrivals.

## Two hosts on a real fabric

After the single-host gate passes, run the same three commands with the
local AV-network interface address on **each** host instead of `127.0.0.1`,
and `--ttl 1` instead of `0`. Keep group and port identical. Receiver and
snooper may share one host or run on separate hosts. The sender needs no
peer address in multicast mode. `0.0.0.0` lets the kernel choose the
interface; set an explicit local IPv4 address on multihomed machines.

An explicit unicast alternative is:

```sh
# receiver host A
yoke-media run --role receiver --unicast --port 5004 --duration 15 --snoop
# sender host B
yoke-media run --role sender --unicast --peer <host-A-ip> --port 5004 --frames 300
```

A separate same-port unicast listener cannot provide multicast's fanout
guarantee. Use multicast for concurrent passive debugging.

Feedback defaults off. A single designated active receiver may supply
`--feedback-host <sender-ip> --feedback-port <sender-source-port>` to
exercise the reference feedback channel. The runner API exposes the
sender socket's selected source port; automated loopback tests use it.
The CLI does not discover that port or elect a reporter. Feedback targets
must be literal unicast IPv4 addresses; snoopers never send feedback.
Do not configure every multicast receiver as a reporter: this reference
does not implement multi-receiver aggregation or election.

## Interpreting the result

Receiver integrity counters describe synthetic frames. Passive wire
observations infer packet continuity, duplication/reordering, frame
boundaries, and arrival bitrate; they cannot infer unseen source stages
or original content integrity from a header alone. Observer histories and
inference limits appear in topology. Modeled compression ratios and
modeled raster bandwidth are distinct from actual datagram byte counts.

The shared-clock single-host test measures reference-software transit,
including jitter playout. It is not physical glass-to-glass latency or a
cross-host timing result. Real codec throughput, capture/display timing,
switch load, and hardware behavior remain bench items in `HARDWARE.md`
and `EDGE-DEVICES.md`.
Unknown sink latency fields are `null`; measured zero remains `0.0`.
