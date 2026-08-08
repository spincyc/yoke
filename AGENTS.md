# Yoke — contributor guidance

- `doc/design/PROTOCOL.md` is normative for the wire protocol; behavior
  changes require editing it (with an ADR in `doc/decisions/`) before
  code. `doc/design/ARCHITECTURE.md` explains why the shape is what it
  is; read it before proposing structural change.
- Two invariants are load-bearing and non-negotiable (ADRs 0002, 0004):
  no yoke code path may receive or inspect media payload, and no
  component may participate in HDCP in any way — including never
  instructing an endpoint to disable or downgrade protection.
- The `yoke.core` package is sans-I/O: no sockets, no clock reads, no
  filesystem. All I/O belongs in `yoke.net`, drivers, or the CLI shell.
- Every code path must be exercisable offline. Error conditions
  encountered in the field land as a deterministic test (simnet scenario
  or scripted driver transcript) before or with their fix.
- Runtime code imports stdlib only (ADR 0008). Dev tools live behind the
  `dev` extra.
- Verification gate: `make verify` (ruff format check + ruff check +
  mypy --strict + pytest) must pass before commit.
- Hardware claims marked ⚠ in `doc/research/HARDWARE.md` are
  bench-unverified; code encoding such a claim must isolate it (as
  `drivers/jap.py` does) and its tests must state the assumption.
