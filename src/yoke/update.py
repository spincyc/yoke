"""Offline, operator-controlled Linux application releases (ADR 0018).

This file deliberately imports only the standard library. Provision a fixed
copy at /opt/yoke/updater.py so recovery never depends on a candidate release.
It has no listener, automatic download, commissioning, or state migration.
"""

from __future__ import annotations

import argparse
import contextlib
import email.parser
import fcntl
import hashlib
import io
import json
import math
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
import zipfile
import zlib
from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Protocol

FORMAT = "yoke-linux-release-v1"
MAX_ARCHIVE = 96 * 1024 * 1024
MAX_WHEEL = 64 * 1024 * 1024
MAX_JSON = 16 * 1024
MAX_OUTPUT = 64 * 1024
RELEASE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
WHEEL = re.compile(r"yoke-[A-Za-z0-9_.+]+-py3-none-any\.whl\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
PHASES = frozenset(
    {
        "idle",
        "staging",
        "staged",
        "stage-failed",
        "activating",
        "healthy",
        "rolling-back",
        "rolled-back",
        "recovery-required",
    }
)
INTERRUPTED = frozenset({"activating", "rolling-back", "recovery-required"})


class UpdateError(Exception):
    """A rejected or failed operation; never an implicit state reset."""


@dataclass(frozen=True)
class RunResult:
    returncode: int
    output: str = ""


class CommandRunner(Protocol):
    def run(self, argv: Sequence[str], *, timeout: float) -> RunResult: ...


class SubprocessRunner:
    """Bounded argv-only subprocesses, with no inherited Python/notify env."""

    def run(self, argv: Sequence[str], *, timeout: float) -> RunResult:
        if (
            isinstance(argv, (str, bytes))
            or not argv
            or any(not isinstance(s, str) or "\x00" in s for s in argv)
            or not argv[0]
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise UpdateError("invalid command or timeout")
        try:
            proc = subprocess.Popen(
                list(argv),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env={"PATH": os.defpath, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
            )
        except OSError as exc:
            raise UpdateError(f"cannot start {argv[0]}: {exc}") from exc
        assert proc.stdout is not None
        output = bytearray()
        deadline = time.monotonic() + timeout
        selector = selectors.DefaultSelector()
        try:
            selector.register(proc.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise UpdateError(f"command timed out: {argv[0]}")
                for key, _ in selector.select(min(remaining, 0.25)):
                    chunk = os.read(key.fd, 4096)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        break
                    output.extend(chunk)
                    if len(output) > MAX_OUTPUT:
                        raise UpdateError(f"command output exceeds limit: {argv[0]}")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise UpdateError(f"command timed out: {argv[0]}")
            try:
                returncode = proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired as exc:
                raise UpdateError(f"command timed out: {argv[0]}") from exc
            return RunResult(returncode, output.decode("utf-8", errors="replace"))
        finally:
            selector.close()
            proc.stdout.close()
            # The process group includes children that might retain stdout or
            # outlive the command. Timeout is not proof they stopped.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired as exc:
                raise UpdateError("command process could not be reaped") from exc


def _checked(runner: CommandRunner, argv: Sequence[str], timeout: float) -> str:
    result = runner.run(argv, timeout=timeout)
    if result.returncode != 0:
        raise UpdateError(
            f"command failed ({result.returncode}): {argv[0]}: "
            f"{result.output[-2048:].strip()}"
        )
    return result.output


def _release(value: object) -> str:
    if not isinstance(value, str) or not RELEASE.fullmatch(value):
        raise UpdateError("invalid release identifier")
    return value


def _digest(value: object) -> str:
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise UpdateError("SHA-256 must be 64 lowercase hexadecimal characters")
    return value


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise UpdateError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _json(data: bytes) -> dict[str, object]:
    if len(data) > MAX_JSON:
        raise UpdateError("JSON exceeds size limit")
    try:
        result: object = json.loads(data.decode("utf-8"), object_pairs_hook=_unique)
        # Reject nonfinite JSON accepted by Python's permissive decoder.
        json.dumps(result, allow_nan=False, ensure_ascii=False).encode("utf-8")
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise UpdateError("invalid UTF-8 JSON") from exc
    if not isinstance(result, dict):
        raise UpdateError("expected a JSON object")
    return result


def _regular(path: Path) -> BinaryIO:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise UpdateError(f"cannot read regular file {path}: {exc}") from exc
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise UpdateError(f"not a regular file: {path}")
    return os.fdopen(fd, "rb")


def _small(path: Path) -> bytes:
    with _regular(path) as stream:
        data = stream.read(MAX_JSON + 1)
    if len(data) > MAX_JSON:
        raise UpdateError(f"file exceeds size limit: {path}")
    return data


def _safe_member(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        bool(name)
        and not name.startswith("/")
        and "\\" not in name
        and "\x00" not in name
        and all(p not in {"", ".", ".."} for p in name.split("/"))
        and str(path) == name
    )


def _validate_wheel(filename: str, data: bytes) -> None:
    if not WHEEL.fullmatch(filename) or not 0 < len(data) <= MAX_WHEEL:
        raise UpdateError("expected a bounded yoke-*-py3-none-any.whl")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as wheel:
            entries = wheel.infolist()
            if len(entries) > 4096:
                raise UpdateError("wheel has too many entries")
            names: set[str] = set()
            metadata_dirs: set[str] = set()
            expanded = 0
            for entry in entries:
                name = entry.filename.rstrip("/") if entry.is_dir() else entry.filename
                if (
                    not _safe_member(name)
                    or name in names
                    or entry.flag_bits & 1
                    or entry.compress_type
                    not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                    or stat.S_IFMT(entry.external_attr >> 16)
                    not in {0, stat.S_IFREG, stat.S_IFDIR}
                ):
                    raise UpdateError(
                        "unsafe, duplicate, linked, or encrypted wheel entry"
                    )
                names.add(name)
                top = name.split("/", 1)[0]
                if top != "yoke":
                    if not re.fullmatch(r"yoke-[A-Za-z0-9_.+]+\.dist-info", top):
                        raise UpdateError("wheel contains files outside Yoke package")
                    metadata_dirs.add(top)
                expanded += entry.file_size
                if expanded > MAX_WHEEL:
                    raise UpdateError("expanded wheel exceeds size limit")
            if len(metadata_dirs) != 1 or "yoke/cli.py" not in names:
                raise UpdateError(
                    "wheel needs Yoke CLI and exactly one dist-info directory"
                )
            info = metadata_dirs.pop()
            parser = email.parser.BytesParser()
            metadata = parser.parsebytes(wheel.read(f"{info}/METADATA"))
            tags = parser.parsebytes(wheel.read(f"{info}/WHEEL"))
            dependencies = metadata.get_all("Requires-Dist", [])
            dev_dependencies_only = not dependencies or (
                metadata.get_all("Provides-Extra") == ["dev"]
                and all(
                    re.fullmatch(r"[^;\r\n]+;\s*extra\s*==\s*(['\"])dev\1", item)
                    for item in dependencies
                )
            )
            if (
                metadata.get_all("Name") != ["yoke"]
                or not dev_dependencies_only
                or tags.get_all("Root-Is-Purelib") != ["true"]
                or tags.get_all("Tag") != ["py3-none-any"]
                or wheel.testzip() is not None
            ):
                raise UpdateError("wheel must be dependency-free pure Python Yoke")
    except (
        zipfile.BadZipFile,
        KeyError,
        RuntimeError,
        OSError,
        EOFError,
        zlib.error,
        UnicodeError,
        ValueError,
    ) as exc:
        raise UpdateError(f"invalid wheel: {exc}") from exc


@dataclass(frozen=True)
class Bundle:
    release: str
    filename: str
    wheel: bytes
    sha256: str

    def manifest(self) -> dict[str, object]:
        return {
            "format": FORMAT,
            "release": self.release,
            "state_format": 1,
            "wheel": {
                "filename": self.filename,
                "size": len(self.wheel),
                "sha256": hashlib.sha256(self.wheel).hexdigest(),
            },
        }


def _manifest(data: bytes) -> tuple[str, str, int, str]:
    obj = _json(data)
    if (
        set(obj) != {"format", "release", "state_format", "wheel"}
        or obj["format"] != FORMAT
        or type(obj["state_format"]) is not int
        or obj["state_format"] != 1
    ):
        raise UpdateError("unsupported release manifest or state format")
    release = _release(obj["release"])
    wheel = obj["wheel"]
    if not isinstance(wheel, dict) or set(wheel) != {"filename", "size", "sha256"}:
        raise UpdateError("invalid wheel manifest")
    filename, size = wheel["filename"], wheel["size"]
    if (
        not isinstance(filename, str)
        or not WHEEL.fullmatch(filename)
        or type(size) is not int
        or not 0 < size <= MAX_WHEEL
    ):
        raise UpdateError("invalid wheel name or size")
    return release, filename, size, _digest(wheel["sha256"])


def verify_bundle(path: Path, expected_sha256: str) -> Bundle:
    expected_sha256 = _digest(expected_sha256)
    with _regular(path) as stream:
        if not 0 < os.fstat(stream.fileno()).st_size <= MAX_ARCHIVE:
            raise UpdateError("archive exceeds size limit or is empty")
        # Parse the same bounded snapshot that was hashed. A writable incoming
        # file changing between hash verification and extraction cannot race us.
        data = stream.read(MAX_ARCHIVE + 1)
        if len(data) > MAX_ARCHIVE:
            raise UpdateError("archive exceeds size limit")
        digest = hashlib.sha256(data).hexdigest()
        if digest != expected_sha256:
            raise UpdateError("artifact SHA-256 mismatch")
        members: dict[str, bytes] = {}
        end = 0
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
                for member in archive:
                    if (
                        len(members) >= 2
                        or not member.isfile()
                        or member.issparse()
                        or member.pax_headers
                        # tarfile transparently consumes PAX/GNU headers. A
                        # member must have one plain header at the next offset.
                        or member.offset != end
                        or member.offset_data != member.offset + 512
                        or not _safe_member(member.name)
                        or data[member.offset + 257 : member.offset + 265]
                        != b"ustar\x0000"
                        or "/" in member.name
                        or member.name in members
                        or not 0
                        < member.size
                        <= (MAX_JSON if member.name == "manifest.json" else MAX_WHEEL)
                    ):
                        raise UpdateError("unsafe or unsupported archive member")
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        raise UpdateError("unreadable archive member")
                    members[member.name] = extracted.read(member.size + 1)
                    end = member.offset_data + ((member.size + 511) // 512) * 512
        except (tarfile.TarError, OSError, EOFError) as exc:
            raise UpdateError(f"invalid uncompressed release archive: {exc}") from exc
        if any(data[end:]):
            raise UpdateError("nonzero data after archive terminator")
    if "manifest.json" not in members or len(members) != 2:
        raise UpdateError("archive needs manifest.json and exactly one wheel")
    release, filename, size, wheel_digest = _manifest(members["manifest.json"])
    wheel = members.get(filename)
    if (
        wheel is None
        or len(wheel) != size
        or hashlib.sha256(wheel).hexdigest() != wheel_digest
    ):
        raise UpdateError("wheel does not match manifest")
    _validate_wheel(filename, wheel)
    return Bundle(release, filename, wheel, digest)


def build_bundle(wheel: Path, release: str, output: Path) -> dict[str, object]:
    release = _release(release)
    with _regular(wheel) as stream:
        data = stream.read(MAX_WHEEL + 1)
    _validate_wheel(wheel.name, data)
    bundle = Bundle(release, wheel.name, data, "")
    manifest = json.dumps(bundle.manifest(), sort_keys=True).encode("utf-8")
    # Exclusive creation prevents silently replacing a reviewed artifact.
    with output.open("xb") as destination:
        with tarfile.open(
            fileobj=destination, mode="w:", format=tarfile.USTAR_FORMAT
        ) as archive:
            for name, contents in (("manifest.json", manifest), (wheel.name, data)):
                info = tarfile.TarInfo(name)
                info.size, info.mode, info.mtime = len(contents), 0o644, 0
                archive.addfile(info, io.BytesIO(contents))
        destination.flush()
        os.fsync(destination.fileno())
    with _regular(output) as stream:
        hasher = hashlib.sha256()
        while chunk := stream.read(65536):
            hasher.update(chunk)
        digest = hasher.hexdigest()
    return {
        "schema_version": 1,
        "release": release,
        "artifact": str(output),
        "sha256": digest,
    }


class Installer(Protocol):
    def install(self, wheel: Path, destination: Path) -> None: ...


@dataclass
class WheelInstaller:
    runner: CommandRunner
    python: str = sys.executable

    def install(self, wheel: Path, destination: Path) -> None:
        # The fixed standalone script bypasses package Requires-Python metadata.
        if sys.version_info < (3, 12):  # noqa: UP036
            raise UpdateError("host Python 3.12 or newer is required")
        _checked(self.runner, [self.python, "-I", "-m", "venv", str(destination)], 120)
        python = str(destination / "bin/python")
        _checked(
            self.runner,
            [
                python,
                "-I",
                "-m",
                "pip",
                "--isolated",
                "--disable-pip-version-check",
                "--no-input",
                "install",
                "--no-index",
                "--no-deps",
                "--no-compile",
                "--no-cache-dir",
                "--only-binary=:all:",
                str(wheel),
            ],
            120,
        )
        _checked(self.runner, [python, "-I", "-m", "yoke.cli", "--help"], 15)


class Service(Protocol):
    def check_contract(self) -> None: ...
    def stop(self) -> None: ...
    def start(self) -> None: ...
    def healthy(self) -> None: ...


@dataclass
class SystemdService:
    root: Path
    runner: CommandRunner
    unit: str = "yoked.service"
    systemctl: str = "/usr/bin/systemctl"
    pause: Callable[[float], None] = time.sleep

    def _show(self, unit: str, properties: str) -> dict[str, str]:
        text = _checked(
            self.runner,
            [self.systemctl, "show", unit, f"--property={properties}", "--no-pager"],
            10,
        )
        return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)

    def check_contract(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@:-]{0,127}\.service", self.unit):
            raise UpdateError("invalid systemd service name")
        props = self._show(self.unit, "Type,NotifyAccess,ExecStart,Requires,After")
        start = props.get("ExecStart", "")
        if (
            props.get("Type") != "notify"
            or props.get("NotifyAccess") != "main"
            or start.count("path=") != 1
            or f"path={self.root}/current/venv/bin/python ;" not in start
            or " -m yoke.cli agent " not in start
        ):
            raise UpdateError(
                "service must notify readiness and execute current/venv Python agent"
            )
        recovery = "yoke-update-recover.service"
        if any(
            recovery not in props.get(key, "").split() for key in ("Requires", "After")
        ):
            raise UpdateError("service must require and start after the recovery unit")
        recovery_props = self._show(recovery, "Type,RemainAfterExit,ExecStart,User")
        command = recovery_props.get("ExecStart", "")
        if (
            recovery_props.get("Type") != "oneshot"
            or recovery_props.get("RemainAfterExit") != "yes"
            or recovery_props.get("User") not in {"", "root"}
            or command.count("path=") != 1
            or "path=/usr/bin/python3 ;" not in command
            or f"argv[]=/usr/bin/python3 {self.root}/updater.py recover --offline ;"
            not in command
        ):
            raise UpdateError(
                "recovery unit must run the fixed offline updater once per boot as root"
            )
        with _regular(self.root / "updater.py") as stream:
            info = os.fstat(stream.fileno())
            if info.st_uid != os.geteuid() or info.st_mode & 0o022:
                raise UpdateError(
                    "fixed updater must be caller-owned and not group/other writable"
                )
        _checked(
            self.runner,
            [self.systemctl, "is-active", "--quiet", recovery],
            10,
        )

    def stop(self) -> None:
        _checked(self.runner, [self.systemctl, "stop", self.unit], 100)

    def start(self) -> None:
        # Type=notify makes systemctl wait for the main process's READY=1.
        _checked(self.runner, [self.systemctl, "start", self.unit], 100)

    def healthy(self) -> None:
        for index in range(3):
            if index:
                self.pause(1)
            _checked(
                self.runner, [self.systemctl, "is-active", "--quiet", self.unit], 10
            )


@dataclass(frozen=True)
class State:
    phase: str = "idle"
    active: str | None = None
    previous: str | None = None
    candidate: str | None = None
    detail: str = ""


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _directory(path: Path) -> None:
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o022
    ):
        raise UpdateError(
            f"directory must be caller-owned and not group/other writable: {path}"
        )


@dataclass
class Updater:
    root: Path
    installer: Installer
    service: Service

    def __post_init__(self) -> None:
        self.root = self.root.absolute()
        if self.root.resolve() != self.root:
            raise UpdateError("update root must not traverse symbolic links")

    def _prepare(self) -> None:
        self.root.mkdir(mode=0o755, exist_ok=True)
        _directory(self.root)
        (self.root / "releases").mkdir(mode=0o755, exist_ok=True)
        _directory(self.root / "releases")

    @contextlib.contextmanager
    def _lock(self) -> Iterator[None]:
        self._prepare()
        try:
            fd = os.open(
                self.root / ".update.lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
            )
        except OSError as exc:
            raise UpdateError(f"cannot open update lock: {exc}") from exc
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
            ):
                raise UpdateError("unsafe update lock")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise UpdateError("another updater holds the update lock") from exc
            yield
        finally:
            os.close(fd)

    def _current(self) -> str | None:
        path = self.root / "current"
        if not path.is_symlink():
            if path.exists():
                raise UpdateError("current must be a release symlink")
            return None
        target = os.readlink(path)
        if not target.startswith("releases/") or target.count("/") != 1:
            raise UpdateError("current has an unsafe target")
        release = _release(target.split("/", 1)[1])
        return release

    def _installed(self, release: str, *, baseline: bool = False) -> Path:
        path = self.root / "releases" / _release(release)
        _directory(path)
        if not (path / "venv/bin/python").is_file():
            raise UpdateError(f"release has no installed Python: {release}")
        metadata = path / "release.json"
        if baseline and release == "initial" and not metadata.exists():
            return path
        obj = _json(_small(metadata))
        if set(obj) != {"manifest", "sha256"} or not isinstance(obj["manifest"], dict):
            raise UpdateError("invalid installed release metadata")
        manifest = json.dumps(obj["manifest"]).encode("utf-8")
        actual, _, _, _ = _manifest(manifest)
        _digest(obj["sha256"])
        if actual != release:
            raise UpdateError("installed release identifier mismatch")
        return path

    def _state(self) -> State:
        path = self.root / "update-state.json"
        if not path.exists() and not path.is_symlink():
            return State(active=self._current())
        obj = _json(_small(path))
        if set(obj) != {
            "schema_version",
            "phase",
            "active",
            "previous",
            "candidate",
            "detail",
        }:
            raise UpdateError("invalid update journal")
        phase, detail = obj["phase"], obj["detail"]
        if (
            type(obj["schema_version"]) is not int
            or obj["schema_version"] != 1
            or not isinstance(phase, str)
            or phase not in PHASES
            or not isinstance(detail, str)
            or len(detail) > 4096
        ):
            raise UpdateError("invalid update journal state")
        refs: list[str | None] = []
        for key in ("active", "previous", "candidate"):
            value = obj[key]
            refs.append(None if value is None else _release(value))
        return State(phase, refs[0], refs[1], refs[2], detail)

    def _write(self, state: State) -> None:
        # A diagnostic must never make its own recovery journal unreadable.
        # 2048 characters fit MAX_JSON even at six JSON escape bytes each.
        state = replace(state, detail=state.detail[:2048])
        data = json.dumps(
            {"schema_version": 1, **asdict(state)}, sort_keys=True, ensure_ascii=False
        ).encode("utf-8", errors="replace")
        name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=self.root, prefix=".update-state-", delete=False
            ) as stream:
                name = stream.name
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.root / "update-state.json")
            name = None
            _sync_directory(self.root)
        finally:
            if name is not None:
                Path(name).unlink(missing_ok=True)

    def _switch(self, release: str) -> None:
        self._installed(release, baseline=True)
        path = self.root / f".current-{uuid.uuid4().hex}"
        try:
            path.symlink_to(f"releases/{release}")
            os.replace(path, self.root / "current")
            _sync_directory(self.root)
        finally:
            path.unlink(missing_ok=True)

    def status(self) -> dict[str, object]:
        state = self._state()
        releases = self.root / "releases"
        published: list[str] = []
        if releases.exists():
            for path in sorted(releases.iterdir()):
                if RELEASE.fullmatch(path.name):
                    try:
                        self._installed(path.name, baseline=path.name == "initial")
                    except (UpdateError, OSError):
                        continue
                    published.append(path.name)
        return {
            "schema_version": 1,
            "current": self._current(),
            "releases": published,
            "state": asdict(state),
        }

    def stage(
        self, artifact: Path, digest: str, *, dry_run: bool = False
    ) -> dict[str, object]:
        bundle = verify_bundle(artifact, digest)
        destination = self.root / "releases" / bundle.release
        if destination.exists() or destination.is_symlink():
            raise UpdateError("release already exists; choose a new identifier")
        if self._state().phase in INTERRUPTED:
            raise UpdateError("recover the interrupted activation before staging")
        if dry_run:
            return {
                "schema_version": 1,
                "action": "stage",
                "dry_run": True,
                "release": bundle.release,
                "sha256": bundle.sha256,
            }
        with self._lock():
            state = self._state()
            if state.phase in INTERRUPTED:
                raise UpdateError("recover the interrupted activation before staging")
            if destination.exists() or destination.is_symlink():
                raise UpdateError("release already exists; choose a new identifier")
            active = self._current()
            self._write(State("staging", active, state.previous, bundle.release))
            # venv is not relocatable: its generated scripts contain absolute
            # shebangs. Reserve the permanent path, publish release.json last.
            destination.mkdir(mode=0o700)
            try:
                wheel = destination / bundle.filename
                wheel.write_bytes(bundle.wheel)
                self.installer.install(wheel, destination / "venv")
                wheel.unlink()
                metadata = {"manifest": bundle.manifest(), "sha256": bundle.sha256}
                # The service account has read/execute access, never write access.
                # venv includes trusted host-Python symlinks; wheel links were refused.
                destination.chmod(0o755)
                for directory, _, files in os.walk(destination):
                    Path(directory).chmod(0o755)
                    for filename in files:
                        path = Path(directory) / filename
                        if not path.is_symlink():
                            mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
                            path.chmod(mode)
                            with path.open("rb") as stream:
                                os.fsync(stream.fileno())
                    _sync_directory(Path(directory))
                marker = destination / ".release.json"
                with marker.open("xb") as stream:
                    stream.write(json.dumps(metadata, sort_keys=True).encode("utf-8"))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(marker, destination / "release.json")
                _sync_directory(destination)
                _sync_directory(self.root / "releases")
                self._write(State("staged", active, state.previous, bundle.release))
            except Exception as exc:
                shutil.rmtree(destination, ignore_errors=True)
                self._write(
                    State(
                        "stage-failed",
                        active,
                        state.previous,
                        bundle.release,
                        str(exc)[:4096],
                    )
                )
                raise UpdateError(f"staging failed: {exc}") from exc
            return self.status()

    def _restore(
        self, previous: str, candidate: str | None, reason: str, *, offline: bool
    ) -> State:
        self._write(
            State(
                "rolling-back", self._state().active, previous, candidate, reason[:4096]
            )
        )
        try:
            if not offline:
                self.service.stop()
            self._switch(previous)
            if not offline:
                self.service.start()
                self.service.healthy()
        except Exception as exc:
            state = State(
                "recovery-required",
                self._state().active,
                previous,
                candidate,
                f"{reason}; rollback failed: {exc}"[:4096],
            )
            self._write(state)
            return state
        state = State("rolled-back", previous, None, candidate, reason[:4096])
        self._write(state)
        return state

    def activate(
        self,
        release: str,
        *,
        dry_run: bool = False,
        rollback: bool = False,
        expected_current: str | None = None,
    ) -> dict[str, object]:
        release = _release(release)
        self._installed(release, baseline=rollback)
        self.service.check_contract()
        previous = self._current()
        if expected_current is not None and previous != expected_current:
            raise UpdateError("current changed before rollback; retry")
        if previous is None or previous == release:
            raise UpdateError("activation requires a different working current release")
        self._installed(previous, baseline=True)
        if self._state().phase in INTERRUPTED:
            raise UpdateError("recover the interrupted activation first")
        if dry_run:
            return {
                "schema_version": 1,
                "action": "rollback" if rollback else "activate",
                "dry_run": True,
                "release": release,
                "previous": previous,
            }
        with self._lock():
            if self._state().phase in INTERRUPTED:
                raise UpdateError("recover the interrupted activation first")
            if self._current() != previous:
                raise UpdateError("current changed before activation; retry")
            self._installed(previous, baseline=True)
            self._installed(release, baseline=rollback)
            self._write(State("activating", previous, previous, release))
            try:
                self.service.stop()
                self._switch(release)
                self.service.start()
                self.service.healthy()
                self._write(
                    State("rolled-back" if rollback else "healthy", release, previous)
                )
            except (Exception, KeyboardInterrupt) as exc:
                self._restore(
                    previous, release, f"activation failed: {exc}", offline=False
                )
            return self.status()

    def rollback(self, *, dry_run: bool = False) -> dict[str, object]:
        state = self._state()
        previous = state.previous
        if previous is None:
            raise UpdateError("no previous healthy activation is recorded")
        current = self._current()
        if state.active != current or current is None:
            raise UpdateError("current no longer matches the rollback journal")
        return self.activate(
            previous, dry_run=dry_run, rollback=True, expected_current=current
        )

    def recover(self, *, offline: bool = False) -> dict[str, object]:
        with self._lock():
            state = self._state()
            if state.phase in INTERRUPTED:
                if state.previous is None:
                    raise UpdateError("interrupted activation has no recovery target")
                if not offline:
                    self.service.check_contract()
                self._restore(
                    state.previous,
                    state.candidate,
                    "interrupted activation recovered",
                    offline=offline,
                )
            elif state.phase == "staging":
                self._write(
                    State(
                        "stage-failed",
                        self._current(),
                        state.previous,
                        state.candidate,
                        "staging interrupted; current release retained",
                    )
                )
            return self.status()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/opt/yoke"))
    parser.add_argument("--service", default="yoked.service")
    parser.add_argument("--systemctl", default="/usr/bin/systemctl")
    commands = parser.add_subparsers(dest="command", required=True)
    bundle = commands.add_parser(
        "bundle", help="build an offline artifact from a reviewed wheel"
    )
    bundle.add_argument("--wheel", type=Path, required=True)
    bundle.add_argument("--release", required=True)
    bundle.add_argument("--output", type=Path, required=True)
    stage = commands.add_parser(
        "stage", help="verify and install without changing current"
    )
    stage.add_argument("artifact", type=Path)
    stage.add_argument("--sha256", required=True)
    stage.add_argument("--dry-run", action="store_true")
    activate = commands.add_parser(
        "activate", help="explicitly activate a staged release"
    )
    activate.add_argument("release")
    activate.add_argument("--dry-run", action="store_true")
    rollback = commands.add_parser(
        "rollback", help="return to the previous healthy release"
    )
    rollback.add_argument("--dry-run", action="store_true")
    recover = commands.add_parser("recover", help="recover an interrupted activation")
    recover.add_argument("--offline", action="store_true")
    commands.add_parser("status", help="show local update journal and current release")
    args = parser.parse_args(argv)
    try:
        if args.command == "bundle":
            result = build_bundle(args.wheel, args.release, args.output)
        else:
            runner = SubprocessRunner()
            root = args.root.absolute()
            updater = Updater(
                root,
                WheelInstaller(runner),
                SystemdService(root, runner, args.service, args.systemctl),
            )
            if args.command == "stage":
                result = updater.stage(args.artifact, args.sha256, dry_run=args.dry_run)
            elif args.command == "activate":
                result = updater.activate(args.release, dry_run=args.dry_run)
            elif args.command == "rollback":
                result = updater.rollback(dry_run=args.dry_run)
            elif args.command == "recover":
                result = updater.recover(offline=args.offline)
            else:
                result = updater.status()
        print(json.dumps(result, sort_keys=True))
        state = result.get("state")
        if isinstance(state, dict):
            if state.get("phase") == "recovery-required":
                return 4
            if (
                args.command in {"activate", "rollback"}
                and state.get("phase") == "rolled-back"
                and state.get("candidate") is not None
            ):
                return 3
        return 0
    except (UpdateError, OSError) as exc:
        print(json.dumps({"schema_version": 1, "error": str(exc)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
