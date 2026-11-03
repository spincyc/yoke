# Contributing to Yoke

Yoke is an early software reference and endpoint engineering project.
Hardware assumptions are recorded separately from tested software behavior.
Small changes with a reproducible failure case are the most useful way to
contribute.

## Developer setup

Use Python 3.12 or later and GNU Make:

```sh
make verify
```

The Makefile creates `.scratch/venv` and installs the exact versions in the
`dev` extra. Runtime code has no third-party dependencies. Set
`PYTHON=python3.12` to select an interpreter or `VENV=.scratch/dev` to
reuse an existing environment. Generated docs, caches, and benchmark output
belong under `.scratch` and are disposable.

Useful individual targets:

```sh
make lint type test
make simulate
make benchmark-control
make benchmark-media
make docs
make docs-serve
make browser-install verify-browser
```

`make verify` includes the static documentation build. The site generator
uses Python-Markdown to render content, rewrites repository links, and
checks all generated local links and anchors. Edit site content in
`site/pages/`, canonical design documents in `doc/`, navigation in
`site/site.toml`, and presentation in `site/assets/` or `site/template.html`.
ADRs in `doc/decisions/` are published automatically.

Keep the reader paths distinct: `site/pages/` explains the product and
organizes get-started, guides, reference, and engineering content;
`doc/design/` holds the canonical detailed designs. Link a design from a
hub rather than copying it. `site/site.toml` lists published pages and
curated navigation separately. A new ADR belongs in the
[decision index](site/pages/engineering/decisions.md); publication alone
does not make it a primary navigation item. Preserve published page URLs
and add compatibility anchors when changing a heading that has inbound
links.

For browser checks, run `make browser-install verify-browser` once to
provision the Chromium revision pinned by the development Playwright
version and check the generated site. After provisioning,
`make verify-browser` reuses that browser; verification never downloads
one. Linux CI also sets `BROWSER_INSTALL_OPTIONS=--with-deps` during its
explicit installation step. Browser evidence is written under
`.scratch/browser-report`.


## Design boundaries

Read [the architecture](doc/design/ARCHITECTURE.md) before structural
changes. [The wire protocol](doc/design/PROTOCOL.md) is normative; propose
a protocol behavior change in that document and an ADR before code.
Keep the protocol core sans-I/O: callers inject time and perform effects.
Runtime imports stay in the Python standard library.

The agent and control plane never inspect media payload. Separate media
reference tools may handle unprotected synthetic content. No component may
decrypt protected content, hold HDCP secrets, alter HDCP messages or
capabilities, or instruct endpoints to weaken protection. The exact scope
is recorded in [ADR 0011](doc/decisions/0011-media-and-hdcp-scope-for-yoke-endpoints.md).

## Reproducing a problem

For protocol or driver behavior, add a deterministic offline test before
or with the fix. Include the seed and fault schedule for a simulation
failure. Use scripted driver transcripts for vendor API assumptions; label
assumptions that have not been checked on actual hardware.

For performance changes, record the interpreter, workload, machine,
parameters, and repeated measurements. Keep software model performance
separate from physical video/USB timing and real codec throughput. Avoid
machine-specific timing thresholds in CI.

Before submitting, run `make verify`; for site changes also run
`make verify-browser` after browser provisioning. Describe the user-visible change,
the reproduction, and the checks that actually ran. Report skipped socket
tests explicitly. Do not include site keys, endpoint credentials, real
site inventories, or private captures in a report.

## Publication

CI runs the software and browser gates on Python 3.12 and 3.14, plus
the separate ESP32 firmware gate. Pages publication requires all three
job families to pass and deploys the exact browser-verified static
artifact from `feature/ir`. Maintainers can also dispatch it manually
on that branch. Generated HTML is a build artifact and is not committed.
