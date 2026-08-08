# 0008 — Implementation language: Python, stdlib-only at runtime

Date: 2026-08-08. Status: accepted.

Agents run on small embedded-Linux boxes and must be trivially
deployable (single directory copy, no wheel-building on-device). The
surrounding tooling ecosystem (telos, homelab) is Python, and the
control plane is I/O-light — throughput is irrelevant, correctness and
auditability are everything.

Accepted: Python ≥ 3.12, `yoke` runtime imports **stdlib only**
(`json`, `hmac`, `hashlib`, `socket`, `asyncio`, `dataclasses`, …).
Development dependencies (pytest, ruff, mypy) live behind a `dev` extra
and never ship. If a future endpoint cannot host CPython, the sans-I/O
core (ADR 0006) is the porting boundary and the wire protocol document
is the contract; `PROTOCOL.md`, not this codebase, is what a Go/Rust
port implements.
