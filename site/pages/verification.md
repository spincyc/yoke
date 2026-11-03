# Evidence before endpoints

Every result needs a boundary. A deterministic simulation can establish
state-machine behavior under its fault model. A UDP loopback test can
establish behavior through real local sockets. Neither measures an HDMI
link's physical latency or proves a device vendor's API assumptions.

## One local gate

```sh
make verify
```

This creates `.scratch/venv`, installs the pinned development tools, and
runs formatting, lint, strict type checking, pytest, and a strict static
site build. Set `PYTHON=python3.12` or `VENV=.scratch/dev` to choose an
interpreter or reuse an existing developer environment. Test and tool
caches stay under `.scratch`.

| Layer | What it establishes | What remains outside it |
|---|---|---|
| Pure unit tests | Frame validation, persistence effects, state transitions, driver contracts | Vendor implementation behavior |
| Seeded simulations | Repeatable loss, delay, duplication, partitions, replay, and recovery scenarios | Unmodeled network behavior |
| Real-socket loopback | Local UDP paths, independent observers, and transport integration | A loaded physical fabric |
| Software benchmarks | Timing and resource behavior on the machine that ran them | Codec silicon or glass-to-glass latency |
| Endpoint bench work | Still pending | Capture/display, real USB gear, HDCP relay timing, and hardware latency |

Tests requiring socket support skip when the execution environment forbids
sockets. Read the skip summary before claiming that the loopback layer ran.

## Reproduce a fault campaign

```sh
make simulate
```

The control-plane campaign runs fixed seeds and reports structured
results. Keep the seed, scenario, and complete result when reporting a
failure. This is offline testing, using the same protocol state machines
as the network shell.

The media reference has its own fault controls:

```sh
.scratch/venv/bin/yoke-media simulate --frames 1000 --drop 0.05 --fec xor --seed 7 --json
.scratch/venv/bin/yoke-media probe --frames 1000 --json
```

A fault model has assumptions. FEC does not promise recovery from arbitrary
loss. A successful synthetic frame verifies the model's payload and digest,
not a real capture source or interoperable JPEG XS bitstream.

## Measure software costs

```sh
make benchmark-control
make benchmark-media
```

These targets run repeated software measurements and report their parameters.
The [software performance report](../../doc/design/PERFORMANCE.md) records
measured workloads, reproduction commands, and the scope of each result.
Keep the interpreter, machine, workload, seed, and repeat count with any
result. Compare like-for-like runs on the same machine. Benchmarks are
informational; CI does not impose timing thresholds on shared runners.

To change the media workload:

```sh
make benchmark-media BENCH_FRAMES=10000 REPEAT=5
```

Historical codec benchmarks in the [research archive](../../doc/research/FEASIBILITY.md)
are dated evidence with their own conditions. They are separate from these
reference pipeline measurements and have not been reproduced by this site
build.

## Build and inspect the docs

```sh
make docs
make docs-serve
```

The output is `.scratch/site`. Open `http://127.0.0.1:8000` while the
server runs. The site uses local CSS, local JavaScript, and self-hosted fonts;
no CDN, analytics, or runtime service is required. Content and navigation
remain available with JavaScript disabled.

The builder renders repository Markdown with pinned Python-Markdown,
resolves cross-document links, and rejects missing local targets,
missing anchors, duplicate output paths, and unpublished Markdown pages.
The test suite also checks deterministic output and Pages subpath support.
Research pages carry an explicit status banner so historical feasibility
claims are not presented as verified products.

## Check the browser experience

```sh
make browser-install verify-browser
```

The explicit install step provisions the Chromium revision pinned by the
development Playwright version. Subsequent `make verify-browser` runs
reuse it; verification never downloads a browser. CI adds
`BROWSER_INSTALL_OPTIONS=--with-deps` to the installation step for its
Linux runners. Reports and browser evidence live under
`.scratch/browser-report`.

The browser gate checks the generated site as a visitor would use it,
including narrow screens, navigation, local search, JavaScript fallback,
and the pairing demonstration. It runs separately from the ordinary
software gate so browser provisioning stays explicit.

## CI and publication

Pull requests and pushes run the software and browser gates on Python
3.12 and 3.14, plus the separate ESP32 firmware gate. Publication requires
all of them to pass. The workflow uploads the exact browser-verified
static artifact from `feature/ir`, or through a manual dispatch on that
branch. GitHub Pages serves that artifact. No build tool runs in visitors'
browsers.

Repository maintainers enable **Settings → Pages → Source → GitHub
Actions**. The workflow needs `pages: write` and `id-token: write` only in
its deployment job. Source changes and documentation changes pass the same
verification gate before publication.
