"""Durable per-agent state: atomic load/save of PersistedState.

The Persist effect contract (PROTOCOL.md: persist before ack) requires
that a crash at any point leaves either the old or the new state on
disk, never a torn file — hence write-to-temp + fsync + rename.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from yoke.core.types import PersistedState


class StateError(ValueError):
    pass


def load(path: Path) -> PersistedState:
    """Missing file is a fresh node; a corrupt file is an error the
    operator must resolve (deleting it is an explicit decision — ADR
    0007 makes rejoin safe)."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return PersistedState()
    try:
        return PersistedState.from_json(json.loads(raw))
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        raise StateError(f"{path}: corrupt state file: {exc}") from exc


def save(path: Path, state: PersistedState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    data = json.dumps(state.to_json(), sort_keys=True).encode("utf-8")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
