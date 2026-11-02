"""Owned-resource reconciliation for the Linux USB/IP reference backend.

Imports enter the consumer's Linux USB stack; no physical OTG gadget is
implemented here. The kernel carries payload. See ADR 0014 for ownership
journal recovery, local-operator race limits, and export-readiness probes.
"""

from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import stat
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from yoke.core.types import Body, DriverStatus, NodeState
from yoke.drivers.base import DriverError
from yoke.drivers.shellcmd import CommandRunner

USBIP = "usbip"
DEFAULT_USBIP_PORT = 3240
_MAX_OUTPUT = 65536
_MAX_DEVICES = 64
_BUSID = r"[0-9]+-[0-9]+(?:\.[0-9]+)*"
_BUSID_RE = re.compile(rf"{_BUSID}\Z")
_EXPORT_RE = re.compile(rf"^\s*({_BUSID}):\s")
_PORT_RE = re.compile(r"^Port\s+(\d+):?(?:\s|$)")
_REMOTE_RE = re.compile(r"usbip://([^\s/]+):(\d+)/([^\s/]+)\Z")
_HOST_RE = re.compile(r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?\.?\Z")


def _host(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 253:
        raise DriverError(f"usbip media: bad host: {value!r}")
    candidate = value[1:-1] if value.startswith("[") and value.endswith("]") else value
    if "%" in candidate and not re.fullmatch(
        r"[a-zA-Z0-9_.-]{1,64}", candidate.split("%", 1)[1]
    ):
        raise DriverError(f"usbip media: bad host: {value!r}")
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        if not _HOST_RE.fullmatch(value) or any(
            not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
            for label in value.rstrip(".").split(".")
        ):
            raise DriverError(f"usbip media: bad host: {value!r}") from None
        return value.lower().rstrip(".")


def _port(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 < value < 65536:
        raise DriverError(f"usbip media: bad port: {value!r}")
    return value


def _busids(value: object) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or len(value) > _MAX_DEVICES
        or not all(
            isinstance(b, str) and len(b) <= 31 and _BUSID_RE.fullmatch(b)
            for b in value
        )
        or len(set(value)) != len(value)
    ):
        raise DriverError(f"usbip media: bad busids: {value!r}")
    return value


def _validate_media(media: Body) -> tuple[str, int, list[str]]:
    host = _host(media.get("host"))
    port = _port(media.get("port"))
    busids = _busids(media.get("busids"))
    # Linux record_connection() uses snprintf(buff, 100, "%s %s %s\n").
    if any(len(f"{host} {port} {busid}\n".encode()) > 99 for busid in busids):
        raise DriverError("usbip media: target exceeds 99-byte connection record")
    return host, port, busids


def _timeout(value: float) -> float:
    try:
        valid = not isinstance(value, bool) and math.isfinite(value) and value > 0
    except (OverflowError, TypeError):
        valid = False
    if not valid:
        raise DriverError("usbip timeout must be finite and positive")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate ownership JSON key")
        result[key] = value
    return result


@dataclass(frozen=True, order=True)
class _Remote:
    host: str
    port: int
    busid: str


@dataclass(frozen=True)
class _ImportedPort:
    port: int
    remote: _Remote | None
    active: bool


def _parse_port_output(output: str) -> list[_ImportedPort]:
    if len(output) > _MAX_OUTPUT:
        raise DriverError("oversize `usbip port` output")
    records: list[_ImportedPort] = []
    current: int | None = None
    active = False
    saw_header = False
    for line in output.splitlines():
        text = line.strip()
        if not text:
            continue
        if text in ("Imported USB devices", "===================="):
            saw_header = True
            continue
        match = _PORT_RE.match(text)
        if match:
            if current is not None:
                records.append(_ImportedPort(current, None, active))
            if len(match[1]) > 4:
                raise DriverError("unparseable `usbip port` output: bad port number")
            current = int(match[1])
            active = "<Port in Use>" in text
            continue
        if "-> usbip://" in text:
            match = _REMOTE_RE.search(text)
            if current is None:
                raise DriverError("unparseable `usbip port` output: unexpected URL")
            try:
                if match is None or len(match[2]) > 5:
                    raise DriverError("bad remote URL")
                remote: _Remote | None = _Remote(
                    _host(match[1]), _port(int(match[2])), _busids([match[3]])[0]
                )
            except DriverError:
                remote = None
            records.append(_ImportedPort(current, remote, active))
            current = None
    if current is not None:
        records.append(_ImportedPort(current, None, active))
    if (
        not records
        and output.strip()
        and output.strip() != "Imported USB devices\n===================="
    ):
        raise DriverError("unparseable `usbip port` output")
    if not records and output.strip() and not saw_header:
        raise DriverError("unparseable `usbip port` output")
    if (
        len({p.port for p in records}) != len(records)
        or len({p.remote for p in records if p.remote is not None})
        != len([p for p in records if p.remote is not None])
        or len(records) > _MAX_DEVICES
    ):
        raise DriverError("unparseable `usbip port` output: duplicate or excess ports")
    return records


def _run(runner: CommandRunner, argv: list[str], timeout: float) -> str:
    result = runner.run(argv, timeout=timeout)
    if result.returncode:
        raise DriverError(
            f"{' '.join(argv)} failed rc={result.returncode}: {result.stderr.strip()}"
        )
    if len(result.stdout) > _MAX_OUTPUT:
        raise DriverError(f"oversize {argv[0]} output")
    return result.stdout


class UsbipAttachDriver:
    """Reconcile owned imports; optional durable journal permits restart cleanup.

    Existing imports without journal ownership are conflicts, including ones
    matching the requested export. Other imports are ignored. Constructor is
    I/O-free; journal loading happens on the serialized driver worker.
    """

    def __init__(
        self,
        runner: CommandRunner,
        *,
        timeout: float = 10.0,
        ownership_file: Path | None = None,
    ) -> None:
        self._runner = runner
        self._timeout = _timeout(timeout)
        self._ownership_file = ownership_file
        self._loaded = False
        self._owned: set[_Remote] = set()
        self._recovery: dict[_Remote, set[int]] = {}
        self._wanted: set[_Remote] | None = None
        self._error: str | None = None

    def _ports(self) -> list[_ImportedPort]:
        return _parse_port_output(_run(self._runner, [USBIP, "port"], self._timeout))

    def _load(self) -> None:
        if self._loaded:
            return
        path = self._ownership_file
        if path is not None:
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            except FileNotFoundError:
                self._loaded = True
                return
            except OSError as exc:
                raise DriverError(f"usbip ownership read: {exc}") from exc
            try:
                metadata = os.fstat(fd)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
                    raise DriverError(
                        "usbip ownership journal must be a private regular file"
                    )
                with os.fdopen(fd, "rb", closefd=False) as stream:
                    raw = stream.read(_MAX_OUTPUT + 1)
                if len(raw) > _MAX_OUTPUT:
                    raise DriverError("usbip ownership journal is oversized")
                data = json.loads(raw, object_pairs_hook=_unique_object)
                if (
                    not isinstance(data, dict)
                    or type(data["schema"]) is not int
                    or data["schema"] not in (1, 2)
                    or set(data)
                    != (
                        {"schema", "imports"}
                        if data["schema"] == 1
                        else {"schema", "imports", "recovery"}
                    )
                ):
                    raise ValueError("unknown ownership schema")
                imports = data["imports"]
                if not isinstance(imports, list) or len(imports) > _MAX_DEVICES:
                    raise ValueError("invalid ownership imports")
                owned = set()
                for record in imports:
                    if not isinstance(record, dict) or set(record) != {
                        "host",
                        "port",
                        "busid",
                    }:
                        raise ValueError("invalid ownership record")
                    owned.add(
                        _Remote(
                            _host(record["host"]),
                            _port(record["port"]),
                            _busids([record["busid"]])[0],
                        )
                    )
                if len(owned) != len(imports):
                    raise ValueError("duplicate ownership imports")
                recovery: dict[_Remote, set[int]] = {}
                records = data.get("recovery", [])
                if not isinstance(records, list) or len(records) > _MAX_DEVICES:
                    raise ValueError("invalid recovery records")
                for record in records:
                    if not isinstance(record, dict) or set(record) != {
                        "host",
                        "port",
                        "busid",
                        "local_ports",
                    }:
                        raise ValueError("invalid recovery record")
                    remote = _Remote(
                        _host(record["host"]),
                        _port(record["port"]),
                        _busids([record["busid"]])[0],
                    )
                    candidates = record["local_ports"]
                    if (
                        remote not in owned
                        or remote in recovery
                        or not isinstance(candidates, list)
                        or not candidates
                        or len(candidates) > _MAX_DEVICES
                        or not all(
                            type(p) is int and 0 <= p <= 65535 for p in candidates
                        )
                    ):
                        raise ValueError("invalid recovery port identities")
                    recovery[remote] = set(candidates)
                self._owned = owned
                self._recovery = recovery
            except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
                raise DriverError(
                    f"usbip ownership journal is unusable: {exc}"
                ) from exc
            finally:
                os.close(fd)
        self._loaded = True

    def _save(
        self, owned: set[_Remote], recovery: dict[_Remote, set[int]] | None = None
    ) -> None:
        recovery = {
            r: set(ids)
            for r, ids in (self._recovery if recovery is None else recovery).items()
            if r in owned
        }
        path = self._ownership_file
        if path is not None:
            temporary: str | None = None
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.is_symlink() or (path.exists() and not path.is_file()):
                    raise DriverError("usbip ownership journal must be a regular file")
                fd, temporary = tempfile.mkstemp(
                    prefix=f".{path.name}.", dir=path.parent
                )
                data = {
                    "schema": 2,
                    "imports": [
                        {"host": r.host, "port": r.port, "busid": r.busid}
                        for r in sorted(owned)
                    ],
                    "recovery": [
                        {
                            "host": r.host,
                            "port": r.port,
                            "busid": r.busid,
                            "local_ports": sorted(ids),
                        }
                        for r, ids in sorted(recovery.items())
                    ],
                }
                with os.fdopen(fd, "w") as stream:
                    json.dump(data, stream, sort_keys=True)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
                temporary = None
                directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            except OSError as exc:
                raise DriverError(f"usbip ownership save: {exc}") from exc
            finally:
                if temporary is not None:
                    Path(temporary).unlink(missing_ok=True)
        self._owned = set(owned)
        self._recovery = recovery

    def _refresh_recovery(self, ports: list[_ImportedPort]) -> None:
        """Resolve recorded port disappearance, never infer an unknown owner."""
        known = {p.remote for p in ports if p.remote is not None}
        unknown = {p.port for p in ports if p.remote is None}
        owned = set(self._owned)
        recovery = {r: set(ids) for r, ids in self._recovery.items()}
        for remote, candidates in tuple(recovery.items()):
            if not candidates & unknown:
                del recovery[remote]
                if remote not in known:
                    owned.discard(remote)
        for remote in owned - known:
            if unknown and remote not in recovery:
                recovery[remote] = set(unknown)
        if owned != self._owned or recovery != self._recovery:
            self._save(owned, recovery)

    def _recovery_error(self) -> DriverError:
        ports = sorted({p for ids in self._recovery.values() for p in ids})
        return DriverError(
            f"RECOVERY_REQUIRED: USB/IP port(s) {ports} lack connection metadata; "
            "restore their connection records or identify and manually detach "
            "the ambiguous ports with usbip detach -p PORT; retain the ownership "
            "journal until the ports are resolved"
        )

    def _remove(self, targets: set[_Remote]) -> None:
        errors: list[str] = []
        for target in sorted(targets):
            try:
                ports = self._ports()
                self._refresh_recovery(ports)
                matched = {p.port for p in ports if p.remote == target}
                # Refresh before each detach: a vhci port can be reused.
                for imported in ports:
                    if imported.remote == target:
                        _run(
                            self._runner,
                            [USBIP, "detach", "-p", str(imported.port)],
                            self._timeout,
                        )
                ports = self._ports()
                if any(p.remote == target for p in ports):
                    raise DriverError(f"usbip import still present: {target}")
                if matched and not any(
                    p.port in matched and p.remote is None for p in ports
                ):
                    # We just observed and removed these exact imports; do not
                    # mistake an older unrelated unknown port for their return.
                    self._save(self._owned - {target})
                self._refresh_recovery(ports)
                if target in self._recovery:
                    raise self._recovery_error()
                self._save(self._owned - {target})
            except DriverError as exc:
                errors.append(str(exc))
        if errors:
            raise DriverError("; ".join(errors))

    def attach(self, producer: str, media: Body) -> None:
        host, port, busids = _validate_media(media)
        wanted = {_Remote(host, port, b) for b in busids}
        self._load()
        initial = self._ports()
        self._refresh_recovery(initial)
        if self._recovery:
            raise self._recovery_error()
        present = {p.remote for p in initial if p.remote is not None}
        initial_ports = {p.port for p in initial}
        foreign = wanted & (present - self._owned)
        if foreign:
            raise DriverError(
                f"usbip import is not owned by this driver: {sorted(foreign)}"
            )
        self._wanted = wanted
        self._error = None
        self._remove(self._owned - wanted)
        created: set[_Remote] = set()
        try:
            for remote in sorted(wanted):
                matches = [p for p in self._ports() if p.remote == remote]
                if matches:
                    if remote not in self._owned:
                        raise DriverError(
                            f"usbip import appeared without ownership: {remote}"
                        )
                    if all(p.active for p in matches):
                        continue
                    self._remove({remote})
                created.add(remote)
                self._save(self._owned | {remote})  # durable intent before side effect
                _run(
                    self._runner,
                    [
                        USBIP,
                        "--tcp-port",
                        str(port),
                        "attach",
                        "-r",
                        host,
                        "-b",
                        remote.busid,
                    ],
                    self._timeout,
                )
            missing = wanted - {p.remote for p in self._ports() if p.active}
            if missing:
                raise DriverError(f"usbip attach not confirmed: {sorted(missing)}")
        except DriverError as exc:
            detail = str(exc)
            try:
                ports = self._ports()
                unknown_new = {
                    p.port for p in ports if p.remote is None
                } - initial_ports
                known = {p.remote for p in ports if p.remote is not None}
                missing_created = created - known
                if unknown_new:
                    recovery = dict(self._recovery)
                    for remote in missing_created:
                        recovery[remote] = set(unknown_new)
                    self._save(self._owned, recovery)
                else:
                    # No new unattributed port exists: missing imports were
                    # not created, even if unrelated unknown ports predate us.
                    self._save(self._owned - missing_created)
                self._remove(created & self._owned)
            except DriverError as cleanup:
                detail += f"; cleanup failed: {cleanup}"
            self._error = detail
            raise DriverError(detail) from exc

    def detach(self) -> None:
        self._load()
        self._wanted = set()
        try:
            self._remove(set(self._owned))
        except DriverError as exc:
            self._error = str(exc)
            raise
        self._error = None

    def poll(self) -> DriverStatus:
        try:
            self._load()
            ports = self._ports()
            self._refresh_recovery(ports)
            if self._recovery:
                raise self._recovery_error()
            present = {p.remote for p in ports if p.active and p.remote is not None}
            unknown = sorted(p.port for p in ports if p.remote is None)
            note = (
                f"; RECOVERY_REQUIRED for unrelated USB/IP ports {unknown}; "
                "left untouched"
                if unknown
                else ""
            )
            if self._error:
                return DriverStatus(NodeState.ERROR, self._error)
            if self._wanted is None:
                if self._owned:
                    return DriverStatus(
                        NodeState.PENDING, "owned imports await route reconciliation"
                    )
                return DriverStatus(NodeState.IDLE, note.lstrip("; "))
            if not self._wanted:
                return DriverStatus(NodeState.IDLE, note.lstrip("; "))
            missing = self._wanted - present
            if missing:
                # An error schedules core retry; passive PENDING cannot repair imports.
                return DriverStatus(
                    NodeState.ERROR,
                    f"awaiting busids: {', '.join(r.busid for r in sorted(missing))}",
                )
            return DriverStatus(
                NodeState.ACTIVE, f"{len(self._wanted)} device(s){note}"
            )
        except DriverError as exc:
            return DriverStatus(NodeState.ERROR, str(exc))

    def describe(self) -> Body | None:
        return None


class UsbipBindDriver:
    """Idempotently bind explicitly configured exports and prove readiness.

    ``export_status`` returns 0 for an absent/unbound device, 1 for available and
    2 for in-use usbip-host exports; other states raise DriverError. The
    default probe reads Linux sysfs; tests inject deterministic alternatives.
    usbipd is independently provisioned, never started implicitly here.
    """

    def __init__(
        self,
        runner: CommandRunner,
        *,
        host: str,
        port: int = DEFAULT_USBIP_PORT,
        busids: Sequence[str],
        timeout: float = 10.0,
        export_status: Callable[[str], int] | None = None,
        sysfs_root: Path = Path("/sys/bus/usb/devices"),
    ) -> None:
        self._host, self._port, self._busids = _validate_media(
            {"host": host, "port": port, "busids": list(busids)}
        )
        self._runner = runner
        self._timeout = _timeout(timeout)
        self._sysfs_root = sysfs_root
        self._status = export_status or self._sysfs_status
        self._exporting = False
        self._owned_bindings: set[str] = set()

    def _sysfs_status(self, busid: str) -> int:
        device = self._sysfs_root / busid
        try:
            if not device.exists():
                return 0
            if (device / "driver").resolve().name != "usbip-host":
                return 0
            status = int((device / "usbip_status").read_text().strip())
            if status not in (1, 2):
                raise DriverError(
                    f"usbip export {busid} is in kernel error state {status}"
                )
            return status
        except (OSError, ValueError) as exc:
            raise DriverError(f"usbip export status {busid}: {exc}") from exc

    def attach(self, producer: str, media: Body) -> None:
        raise DriverError("producer driver: attach is consumer-side only")

    def detach(self) -> None:
        """Consumers detach; producers retain configured exports."""

    def bind(self) -> None:
        self._exporting = True
        created: list[str] = []
        try:
            for busid in self._busids:
                if self._status(busid):
                    continue
                created.append(busid)
                self._owned_bindings.add(busid)
                _run(self._runner, [USBIP, "bind", "-b", busid], self._timeout)
                if self._status(busid) not in (1, 2):
                    raise DriverError(f"usbip bind not confirmed: {busid}")
        except DriverError as exc:
            errors = [str(exc)]
            for busid in reversed(created):
                try:
                    if self._status(busid):
                        _run(
                            self._runner, [USBIP, "unbind", "-b", busid], self._timeout
                        )
                    self._owned_bindings.discard(busid)
                except DriverError as cleanup:
                    errors.append(f"cleanup failed: {cleanup}")
            raise DriverError("; ".join(errors)) from exc

    def unbind(self) -> None:
        self._exporting = False
        errors: list[str] = []
        for busid in sorted(self._owned_bindings):
            try:
                if self._status(busid):
                    _run(self._runner, [USBIP, "unbind", "-b", busid], self._timeout)
                self._owned_bindings.discard(busid)
            except DriverError as exc:
                errors.append(str(exc))
        if errors:
            raise DriverError("; ".join(errors))

    def poll(self) -> DriverStatus:
        try:
            status = {busid: self._status(busid) for busid in self._busids}
            if self._exporting and any(value == 0 for value in status.values()):
                # Startup failures and unplug/replug need repair even though
                # the producer core never emits consumer AttachMedia effects.
                self.bind()
                status = {busid: self._status(busid) for busid in self._busids}
            unbound = [b for b, value in status.items() if not value]
            if unbound:
                raise DriverError(
                    f"usbip devices not bound to usbip-host: {', '.join(unbound)}"
                )
            output = _run(
                self._runner,
                [USBIP, "--tcp-port", str(self._port), "list", "-r", "127.0.0.1"],
                self._timeout,
            )
            exported = {
                m[1] for line in output.splitlines() if (m := _EXPORT_RE.match(line))
            }
            missing = [
                b for b, value in status.items() if value == 1 and b not in exported
            ]
            if missing:
                raise DriverError(
                    f"usbipd is not advertising bound devices: {', '.join(missing)}"
                )
            return DriverStatus(NodeState.ACTIVE, f"{len(status)} device(s) exported")
        except DriverError as exc:
            return DriverStatus(NodeState.ERROR, str(exc))

    def describe(self) -> Body | None:
        return {"host": self._host, "port": self._port, "busids": list(self._busids)}
