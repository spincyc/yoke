# 0006 — Sans-I/O protocol core with a simulated network

Date: 2026-08-08. Status: accepted.

All code must be testable offline, including every error condition met in
the wild. Therefore the protocol engine (`yoke.core`) performs no I/O and
never reads a clock: inputs are decoded frames and explicit timestamps;
outputs are effect lists (frames to send, driver commands, events, state
to persist). The asyncio shell (`yoke.net`) and the drivers are thin,
separately tested adapters.

`tests/simnet.py` schedules many cores over a virtual network with
seeded, scriptable loss, duplication, reordering, delay, and partitions.
Every failure-model row in ARCHITECTURE.md has a deterministic
reproduction there. New field incidents must land as a simnet scenario
before their fix merges — the simulation is the regression record.
