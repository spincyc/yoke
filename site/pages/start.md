<span id="start-with-the-software" class="legacy-anchor" aria-hidden="true"></span>
# Get started

These steps run the software tests on one computer. They need no Yoke
boxes or network setup. The [USB hardware](usb.md) uses two Radxa ZERO 3E
boards; a complete device firmware image is not available yet.

<span id="install-from-source" class="legacy-anchor" aria-hidden="true"></span>
## Install the test software

Use Python 3.12 or later:

```sh
git clone https://github.com/spincyc/yoke.git
cd yoke
python3 -m venv .scratch/run
.scratch/run/bin/python -m pip install .
.scratch/run/bin/yoke --help
```

Yoke has no third-party Python runtime dependencies. Installing from source
may download packaging tools. Contributors can use `make verify` to create
a development environment and run the repository checks.

<span id="inspect-the-control-plane" class="legacy-anchor" aria-hidden="true"></span>

<span id="run-the-pairing-software-offline" class="legacy-anchor" aria-hidden="true"></span>
## Test pairing without hardware

```sh
.scratch/run/bin/yoke simulate --json
```

The test suite runs the pairing code through
loss, duplication, split networks, restarts, and storage failures. It uses
virtual time and simulated drivers. Exit status zero means every selected
scenario passed.

To inspect a single scenario:

```sh
.scratch/run/bin/yoke simulate --scenario partition-conflict --seed 42 --trace
```

The trace shows control messages and their virtual timestamps. See the
[simulation reference](../../doc/design/SIMULATION.md) for the scenarios
and the test cases they test.

## Preview the monitor

```sh
.scratch/run/bin/yoke top --demo --once
```

This prints a sample dashboard without opening sockets or reading host
resources. Use `top --demo` in an interactive terminal to explore its
controls. The demo uses sample data.

<span id="run-a-repeatable-media-model" class="legacy-anchor" aria-hidden="true"></span>

## Run a synthetic media check

```sh
.scratch/run/bin/yoke-media probe --frames 120
.scratch/run/bin/yoke-media simulate --frames 120 --drop 0.05 --fec xor --seed 7
```

`probe` exits nonzero if a reference frame fails integrity.
`simulate` reports delivery under the selected faults; its exit status
alone does not establish perfect delivery. These commands use synthetic
frames. The optional `jpegxs` and `vc2` settings model compression ratios;
they do not run those codecs or measure hardware latency.

<span id="exercise-real-sockets" class="legacy-anchor" aria-hidden="true"></span>
<span id="terminal-1-receive-the-reference-multicast-stream" class="legacy-anchor" aria-hidden="true"></span>
<span id="terminal-2-send-to-the-same-group-and-interface" class="legacy-anchor" aria-hidden="true"></span>

## Choose your next step

<div class="link-cards"><a class="link-card" href="guides/pairing.md"><strong>Pair configured endpoints</strong><span>Commission agents, select a route, and inspect driver state.</span></a><a class="link-card" href="../../doc/design/MEDIA-TESTING.md"><strong>Exercise real sockets</strong><span>Run sender, receiver, and passive observer on loopback before using a network.</span></a><a class="link-card" href="verification.md"><strong>Verify a checkout</strong><span>Run tests, fault campaigns, and reproducible software measurements.</span></a><a class="link-card" href="usb.md"><strong>USB hardware</strong><span>See the two-box design and remaining firmware work.</span></a></div>
