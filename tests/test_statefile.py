"""Durable storage faults, exercised offline on disposable files."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from yoke import statefile
from yoke.core.types import Pairing, PersistedState


def state_bytes(**changes: Any) -> bytes:
    data = PersistedState(seq=17, pairing=Pairing("cam-1", (3, "cli-ksh"))).to_json()
    data.update(changes)
    return json.dumps(data).encode()


def assert_no_temporary_files(path: Path) -> None:
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name]


def test_missing_state_requires_explicit_commissioning(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    with pytest.raises(statefile.StateError, match="missing state file"):
        statefile.load(path)
    assert statefile.load(path, allow_missing=True) == PersistedState()
    assert not path.exists()


def test_commissioning_does_not_ignore_corruption(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_bytes(b"corrupt")
    with pytest.raises(statefile.StateError, match="corrupt state file"):
        statefile.load(path, allow_missing=True)
    assert path.read_bytes() == b"corrupt"


def test_short_and_interrupted_writes_are_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.json"
    original_write = os.write
    calls = 0

    def partial_write(fd: int, data: bytes) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise InterruptedError(errno.EINTR, "interrupted")
        return original_write(fd, data[:7])

    monkeypatch.setattr(os, "write", partial_write)
    expected = PersistedState(seq=17, pairing=Pairing("cam-1", (3, "cli-ksh")))
    statefile.save(path, expected)
    assert statefile.load(path) == expected
    assert calls > 2
    assert_no_temporary_files(path)


def test_file_sync_precedes_replace_and_directory_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.json"
    events: list[str] = []
    original_sync = os.fsync
    original_replace = os.replace

    def sync(fd: int) -> None:
        events.append(
            "directory-sync" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file-sync"
        )
        original_sync(fd)

    def replace(src: str, dst: Path) -> None:
        events.append("replace")
        assert statefile.load(Path(src)).seq == 19
        original_replace(src, dst)

    monkeypatch.setattr(os, "fsync", sync)
    monkeypatch.setattr(os, "replace", replace)
    statefile.save(path, PersistedState(seq=19))
    assert events == ["file-sync", "replace", "directory-sync"]


@pytest.mark.parametrize("failure", ["zero-write", "write", "file-sync", "replace"])
def test_failure_before_replace_keeps_old_state_and_cleans_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    path = tmp_path / "state.json"
    statefile.save(path, PersistedState(seq=7))
    before = path.read_bytes()

    def broken(*args: object) -> None:
        raise OSError(errno.ENOSPC, "injected storage failure")

    if failure == "zero-write":
        monkeypatch.setattr(os, "write", lambda fd, data: 0)
    elif failure == "write":
        original_write = os.write
        calls = 0

        def broken_write(fd: int, data: bytes) -> int:
            nonlocal calls
            calls += 1
            if calls == 1:
                return original_write(fd, data[:3])
            raise OSError(errno.ENOSPC, "disk full after partial write")

        monkeypatch.setattr(os, "write", broken_write)
    elif failure == "file-sync":
        monkeypatch.setattr(os, "fsync", broken)
    else:
        monkeypatch.setattr(os, "replace", broken)
    with pytest.raises(OSError):
        statefile.save(path, PersistedState(seq=8))
    assert path.read_bytes() == before
    assert_no_temporary_files(path)


def test_directory_sync_failure_is_reported_even_after_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.json"
    statefile.save(path, PersistedState(seq=7))
    original_sync = os.fsync

    def sync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "directory sync failed")
        original_sync(fd)

    monkeypatch.setattr(os, "fsync", sync)
    with pytest.raises(OSError, match="directory sync failed"):
        statefile.save(path, PersistedState(seq=8))
    assert statefile.load(path).seq == 8
    assert_no_temporary_files(path)


def test_new_parent_directories_are_synced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "one" / "two" / "state.json"
    synced: list[tuple[int, int]] = []
    original_sync = os.fsync

    def sync(fd: int) -> None:
        info = os.fstat(fd)
        if stat.S_ISDIR(info.st_mode):
            synced.append((info.st_dev, info.st_ino))
        original_sync(fd)

    monkeypatch.setattr(os, "fsync", sync)
    statefile.save(path, PersistedState(seq=1))
    expected = [tmp_path, path.parent.parent, path.parent]
    assert synced == [(p.stat().st_dev, p.stat().st_ino) for p in expected]
    assert statefile.load(path).seq == 1


def test_replacement_and_temporary_file_have_private_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.json"
    path.write_bytes(state_bytes())
    path.chmod(0o666)
    original_write = os.write

    def write(fd: int, data: bytes) -> int:
        assert stat.S_IMODE(os.fstat(fd).st_mode) == 0o600
        return original_write(fd, data)

    monkeypatch.setattr(os, "write", write)
    statefile.save(path, PersistedState(seq=2))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_fixed_temporary_symlink_cannot_clobber_another_file(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    victim = tmp_path / "unrelated"
    victim.write_bytes(b"keep me")
    fixed_temp = path.with_name(path.name + ".tmp")
    fixed_temp.symlink_to(victim)
    statefile.save(path, PersistedState(seq=3))
    assert statefile.load(path).seq == 3
    assert victim.read_bytes() == b"keep me"
    assert fixed_temp.is_symlink()


@pytest.mark.parametrize("dangling", [False, True])
def test_symlink_state_is_refused_for_load_and_save(
    tmp_path: Path, dangling: bool
) -> None:
    target = tmp_path / "target.json"
    if not dangling:
        target.write_bytes(state_bytes())
    path = tmp_path / "state.json"
    path.symlink_to(target)
    with pytest.raises(statefile.StateError):
        statefile.load(path)
    with pytest.raises(statefile.StateError, match="regular file"):
        statefile.save(path, PersistedState(seq=2))
    assert path.is_symlink()
    if dangling:
        assert not target.exists()
    else:
        assert target.read_bytes() == state_bytes()


@pytest.mark.parametrize("kind", ["directory", "fifo"])
def test_nonregular_state_is_refused_without_blocking(
    tmp_path: Path, kind: str
) -> None:
    path = tmp_path / "state.json"
    if kind == "directory":
        path.mkdir()
    else:
        os.mkfifo(path)
    with pytest.raises(statefile.StateError):
        statefile.load(path)
    with pytest.raises(statefile.StateError, match="regular file"):
        statefile.save(path, PersistedState(seq=2))


@pytest.mark.parametrize(
    "raw",
    [
        b"\xff",
        b'{"seq":1,"seq":2,"pairing":{"producer":null,"version":[0,""]}}',
        b'{"seq":1,"pairing":{"producer":null,"producer":"cam","version":[0,""]}}',
        b'{"seq":NaN,"pairing":{"producer":null,"version":[0,""]}}',
        b"[" * 1100 + b"]" * 1100,
        b" " * (statefile.MAX_STATE_BYTES + 1),
        state_bytes(seq=True),
        state_bytes(seq="17"),
        state_bytes(seq=1.5),
        state_bytes(seq=-1),
        state_bytes(seq=2**63),
        state_bytes(pairing=[]),
        state_bytes(pairing={"producer": 7, "version": [3, "cli"]}),
        state_bytes(pairing={"producer": "Bad_ID", "version": [3, "cli"]}),
        state_bytes(pairing={"producer": None, "version": [True, "cli"]}),
        state_bytes(pairing={"producer": None, "version": [-1, "cli"]}),
        state_bytes(pairing={"producer": None, "version": [1, ""]}),
        state_bytes(pairing={"producer": None, "version": [0, "cli"]}),
        state_bytes(pairing={"producer": "cam", "version": [0, ""]}),
        state_bytes(pairing={"producer": None, "version": [3, "cli", "extra"]}),
        state_bytes(pairing={"producer": None, "version": [3, "cli"], "extra": 1}),
        state_bytes(schema_version=2),
    ],
)
def test_corruption_is_an_error_and_is_preserved(tmp_path: Path, raw: bytes) -> None:
    path = tmp_path / "state.json"
    path.write_bytes(raw)
    with pytest.raises(statefile.StateError, match="corrupt state file"):
        statefile.load(path)
    assert path.read_bytes() == raw
    assert_no_temporary_files(path)


def test_read_errors_have_path_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.json"

    def denied(*args: object) -> None:
        raise PermissionError(errno.EACCES, "injected denial")

    monkeypatch.setattr(os, "open", denied)
    with pytest.raises(statefile.StateError, match="cannot open state file") as error:
        statefile.load(path)
    assert str(path) in str(error.value)


def test_invalid_in_memory_state_does_not_replace_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    statefile.save(path, PersistedState(seq=7))
    before = path.read_bytes()
    with pytest.raises(ValueError):
        statefile.save(path, PersistedState(seq=-1))
    assert path.read_bytes() == before
    assert_no_temporary_files(path)


def test_lock_excludes_independent_descriptors_and_survives_state_replacement(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.json"
    lock_path = tmp_path / "state.json.lock"
    with statefile.lock(path):
        statefile.save(path, PersistedState(seq=1))
        lock_inode = lock_path.stat().st_ino
        statefile.save(path, PersistedState(seq=2))
        with lock_path.open("rb") as other, pytest.raises(BlockingIOError):
            fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with (
            pytest.raises(statefile.StateError, match="state file already in use"),
            statefile.lock(path),
        ):
            pytest.fail("second holder entered")
        assert lock_path.stat().st_ino == lock_inode
    assert lock_path.stat().st_ino == lock_inode
    with statefile.lock(path):
        assert statefile.load(path).seq == 2
        assert lock_path.stat().st_ino == lock_inode


def test_lock_excludes_another_process_then_releases(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    lock_path = tmp_path / "state.json.lock"
    script = """
import fcntl
import sys
with open(sys.argv[1], "rb") as lock_file:
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(3)
"""

    def probe() -> int:
        return subprocess.run(
            [sys.executable, "-c", script, str(lock_path)],
            capture_output=True,
            check=False,
            timeout=10,
        ).returncode

    with statefile.lock(path):
        assert probe() == 3
    assert probe() == 0


def test_lock_creates_private_parent_and_preserves_stable_file(tmp_path: Path) -> None:
    path = tmp_path / "one" / "two" / "state.json"
    with statefile.lock(path):
        lock_path = path.with_name("state.json.lock")
        assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert not path.exists()
    lock_path.write_bytes(b"old unused lock metadata")
    lock_path.chmod(0o666)
    with statefile.lock(path):
        assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600
        assert lock_path.read_bytes() == b"old unused lock metadata"


def test_locks_are_scoped_to_each_state_file(tmp_path: Path) -> None:
    with (
        statefile.lock(tmp_path / "first.json"),
        statefile.lock(tmp_path / "second.json"),
    ):
        assert sorted(p.name for p in tmp_path.iterdir()) == [
            "first.json.lock",
            "second.json.lock",
        ]


def test_lock_releases_when_body_raises(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    with pytest.raises(RuntimeError, match="injected failure"), statefile.lock(path):
        raise RuntimeError("injected failure")
    with statefile.lock(path):
        assert path.with_name("state.json.lock").is_file()


@pytest.mark.parametrize("phase", ["permissions", "flock"])
def test_lock_closes_descriptor_when_acquisition_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    path = tmp_path / "state.json"
    descriptors: list[int] = []

    def fail(fd: int, operation: int) -> None:
        descriptors.append(fd)
        raise OSError(errno.EIO, "injected acquisition failure")

    with monkeypatch.context() as patch:
        if phase == "permissions":
            patch.setattr(os, "fchmod", fail)
        else:
            patch.setattr(fcntl, "flock", fail)
        with (
            pytest.raises(statefile.StateError, match="cannot lock state file"),
            statefile.lock(path),
        ):
            pytest.fail("failed acquisition entered")
    assert len(descriptors) == 1
    with pytest.raises(OSError) as error:
        os.fstat(descriptors[0])
    assert error.value.errno == errno.EBADF
    with statefile.lock(path):
        pass


@pytest.mark.parametrize("kind", ["symlink", "dangling-symlink", "directory", "fifo"])
def test_lock_refuses_unsafe_files(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "state.json"
    lock_path = path.with_name("state.json.lock")
    victim = tmp_path / "unrelated"
    if kind in {"symlink", "dangling-symlink"}:
        if kind == "symlink":
            victim.write_bytes(b"keep me")
        lock_path.symlink_to(victim)
    elif kind == "directory":
        lock_path.mkdir()
    else:
        os.mkfifo(lock_path)
    with pytest.raises(statefile.StateError), statefile.lock(path):
        pytest.fail("unsafe lock entered")
    if kind == "symlink":
        assert victim.read_bytes() == b"keep me"
    elif kind == "dangling-symlink":
        assert not victim.exists()
