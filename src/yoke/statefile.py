"""Durable per-agent state: atomic load/save of PersistedState.

The Persist effect contract (PROTOCOL.md: persist before ack) requires
that a crash at any point leaves either the old or the new state on
disk, never a torn file. The replacement is not durable until both the
temporary file and its parent directory have been fsynced (ADR 0012).
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from yoke.core.types import PersistedState


class StateError(ValueError):
    pass


MAX_STATE_BYTES = 4096


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant {value}")


def load(path: Path, *, allow_missing: bool = False) -> PersistedState:
    """Load a regular state file without following its final symlink.

    Missing files require explicit fresh-node commissioning through
    ``allow_missing``; state loss for an existing identity is an error.
    Corrupt or unreadable files require explicit operator recovery.
    """
    try:
        # O_NONBLOCK also lets us reject FIFOs without blocking on a writer.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError as exc:
        if allow_missing:
            return PersistedState()
        raise StateError(
            f"{path}: missing state file; restore commissioned state or "
            "explicitly commission a new identity"
        ) from exc
    except OSError as exc:
        raise StateError(f"{path}: cannot open state file: {exc}") from exc
    try:
        with os.fdopen(fd, "rb") as fh:
            if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                raise StateError(f"{path}: state file must be a regular file")
            raw = fh.read(MAX_STATE_BYTES + 1)
        if len(raw) > MAX_STATE_BYTES:
            raise ValueError(f"exceeds {MAX_STATE_BYTES} bytes")
        data = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
        return PersistedState.from_json(data)
    except OSError as exc:
        raise StateError(f"{path}: cannot read state file: {exc}") from exc
    except StateError:
        raise
    except (ValueError, KeyError, TypeError, IndexError, RecursionError) as exc:
        raise StateError(f"{path}: corrupt state file: {exc}") from exc


def _make_parents(path: Path) -> None:
    """Durably link each newly created directory into its own parent."""
    try:
        path.mkdir(mode=0o700)
    except FileNotFoundError:
        _make_parents(path.parent)
        _make_parents(path)
        return
    except FileExistsError:
        return
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def lock(path: Path) -> Iterator[None]:
    """Hold the POSIX agent lease from before load through shutdown.

    The sidecar survives state replacement and is never removed: unlinking
    it could let a new opener lock a different inode while a holder lives.
    The state directory must be controlled by the endpoint operator.
    """
    lock_path = path.with_name(path.name + ".lock")
    try:
        _make_parents(path.parent)
        fd = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
        )
    except OSError as exc:
        raise StateError(f"{path}: cannot lock state file: {exc}") from exc
    try:
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise StateError(f"{lock_path}: state lock must be a regular file")
            os.fchmod(fd, 0o600)
        except OSError as exc:
            raise StateError(f"{path}: cannot lock state file: {exc}") from exc
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise StateError(f"{path}: state file already in use") from exc
            raise StateError(f"{path}: cannot lock state file: {exc}") from exc
        yield
    finally:
        # Closing releases flock even when startup or the runner raises.
        os.close(fd)


def save(path: Path, state: PersistedState) -> None:
    """Atomically replace state; propagate every durability failure.

    A failure after replacement may leave the new state present. Callers
    must still treat that save as failed, rather than acknowledge it.
    """
    # Validate before creating anything or replacing previously durable state.
    validated = PersistedState.from_json(state.to_json())
    data = json.dumps(validated.to_json(), sort_keys=True).encode("utf-8")
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISREG(mode):
            raise StateError(f"{path}: state file must be a regular file")
    _make_parents(path.parent)
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        # mkstemp uses O_EXCL and a random name; a stale or malicious fixed
        # .tmp path cannot be followed or truncated, even across writers.
        fd, tmp = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        try:
            try:
                # Enforce the final mode even with a restrictive umask.
                os.fchmod(fd, 0o600)
                written = 0
                while written < len(data):
                    try:
                        count = os.write(fd, data[written:])
                    except InterruptedError:
                        continue
                    if count <= 0:
                        raise OSError("state file write made no progress")
                    written += count
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, path)
            os.fsync(directory_fd)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(tmp)
    finally:
        os.close(directory_fd)
