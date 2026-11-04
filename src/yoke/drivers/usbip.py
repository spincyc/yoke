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
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

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


def _port_ids(value: object) -> set[int]:
    if (
        not isinstance(value, list)
        or len(value) > _MAX_DEVICES
        or not all(type(p) is int and 0 <= p <= 65535 for p in value)
    ):
        raise ValueError("invalid local port identities")
    return set(value)


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


def _label(argv: list[str]) -> str:
    """``usbip <verb>`` without option values, for a short error prefix."""
    args = argv[1:]
    if args[:1] == ["--tcp-port"]:
        args = args[2:]
    return " ".join([argv[0], *args[:1]])


def _run(runner: CommandRunner, argv: list[str], timeout: float) -> str:
    result = runner.run(argv, timeout=timeout)
    if result.returncode:
        # Cause first, command last: wire detail is truncated to 128 bytes,
        # while the local log keeps the full text (ADR 0023).
        cause = result.stderr.strip() or "(no stderr)"
        raise DriverError(
            f"{_label(argv)} failed rc={result.returncode}: {cause} [{' '.join(argv)}]"
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
        # Record-less ports already present when each owned import was last
        # seen with its connection record (ADR 0024). Absent: legacy journal.
        self._baseline: dict[_Remote, set[int]] = {}
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
                    or data["schema"] not in (1, 2, 3)
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
                baseline: dict[_Remote, set[int]] = {}
                fields = {"host", "port", "busid"}
                for record in imports:
                    if not isinstance(record, dict) or (
                        set(record) != fields
                        and (
                            data["schema"] < 3
                            or set(record) != fields | {"unknown_before"}
                        )
                    ):
                        raise ValueError("invalid ownership record")
                    owned_remote = _Remote(
                        _host(record["host"]),
                        _port(record["port"]),
                        _busids([record["busid"]])[0],
                    )
                    owned.add(owned_remote)
                    if "unknown_before" in record:
                        baseline[owned_remote] = _port_ids(record["unknown_before"])
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
                self._baseline = baseline
            except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
                raise DriverError(
                    f"usbip ownership journal is unusable: {exc}"
                ) from exc
            finally:
                os.close(fd)
        self._loaded = True

    def _save(
        self,
        owned: set[_Remote],
        recovery: dict[_Remote, set[int]] | None = None,
        baseline: dict[_Remote, set[int]] | None = None,
    ) -> None:
        recovery = {
            r: set(ids)
            for r, ids in (self._recovery if recovery is None else recovery).items()
            if r in owned
        }
        baseline = {
            r: set(ids)
            for r, ids in (self._baseline if baseline is None else baseline).items()
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
                    "schema": 3,
                    "imports": [
                        {"host": r.host, "port": r.port, "busid": r.busid}
                        | (
                            {"unknown_before": sorted(baseline[r])}
                            if r in baseline
                            else {}
                        )
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
        self._baseline = baseline

    def _refresh_recovery(self, ports: list[_ImportedPort]) -> None:
        """Resolve recorded port disappearance, never infer an unknown owner.

        A missing owned import can only occupy a record-less port that was
        absent when the import was last seen with its record (ADR 0024);
        older record-less ports are unrelated and never block re-attach.
        """
        known = {p.remote for p in ports if p.remote is not None}
        unknown = {p.port for p in ports if p.remote is None}
        owned = set(self._owned)
        recovery = {r: set(ids) for r, ids in self._recovery.items()}
        baseline = {r: set(ids) for r, ids in self._baseline.items()}
        for remote, candidates in tuple(recovery.items()):
            if not candidates & unknown:
                del recovery[remote]
                if remote not in known:
                    owned.discard(remote)
        for remote in owned & known:
            baseline[remote] = set(unknown)
        for remote in owned - known:
            if remote in recovery:
                continue
            # Legacy journals carry no baseline: keep ADR 0014's
            # conservative attribution until the import is seen again.
            candidates = unknown - baseline.get(remote, set())
            if candidates:
                recovery[remote] = candidates
        baseline = {r: ids for r, ids in baseline.items() if r in owned}
        if (
            owned != self._owned
            or recovery != self._recovery
            or baseline != self._baseline
        ):
            self._save(owned, recovery, baseline)

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
                current = self._ports()
                matches = [p for p in current if p.remote == remote]
                if matches:
                    if remote not in self._owned:
                        raise DriverError(
                            f"usbip import appeared without ownership: {remote}"
                        )
                    if all(p.active for p in matches):
                        continue
                    self._remove({remote})
                    current = self._ports()
                created.add(remote)
                # Durable intent before the side effect, with the record-less
                # ports that already exist and so cannot be this import.
                before = {p.port for p in current if p.remote is None}
                self._save(
                    self._owned | {remote}, baseline={**self._baseline, remote: before}
                )
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


# --- producer data-plane admission (ADR 0024) --------------------------------

#: Agent-written request (its runtime directory) and helper-written status
#: (a root-owned runtime directory, so the agent cannot plant a symlink).
DEFAULT_ADMISSION_REQUEST = Path("/run/yoke/usbip-admission.json")
DEFAULT_ADMISSION_STATUS = Path("/run/yoke-admission/status.json")
ADMISSION_TABLE = "yoke_usbip"
MAX_ADMITTED = 16
_MAX_ADMISSION_FILE = 4096
#: Producer readiness probes (finding 8): sysfs every poll; the daemon's
#: device list on change, after failure, and otherwise at most this often.
DAEMON_PROBE_INTERVAL = 30.0
DAEMON_RETRY_INTERVAL = 5.0
#: A request the helper has not applied after this long is written again, so
#: a path-unit trigger lost while the helper was running cannot wedge it.
ADMISSION_REREQUEST = 5.0

Peers = tuple[tuple[str, str], ...]


def _admissible_ipv4(value: object) -> str:
    """A unicast IPv4 literal that may be allowlisted (never loopback)."""
    if not isinstance(value, str):
        raise ValueError(f"admission address must be a string: {value!r}")
    try:
        address = ipaddress.IPv4Address(value)
    except ValueError as exc:
        raise ValueError(f"admission address is not IPv4: {value!r}") from exc
    if (
        address.is_multicast
        or address.is_unspecified
        or address.is_loopback
        or address.is_reserved
        or address == ipaddress.IPv4Address("255.255.255.255")
    ):
        raise ValueError(f"admission address is not a unicast host: {value}")
    return str(address)


def _read_private_json(path: Path, what: str) -> object:
    """Bounded JSON from a regular file opened without following symlinks."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f"{what} must be a regular file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(_MAX_ADMISSION_FILE + 1)
    finally:
        os.close(fd)
    if len(raw) > _MAX_ADMISSION_FILE:
        raise ValueError(f"{what} is oversized")
    return json.loads(raw, object_pairs_hook=_unique_object)


def _write_atomic(path: Path, data: object, mode: int) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, sort_keys=True, separators=(",", ":"))
        os.replace(temporary, path)
        temporary = ""
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)


def render_admission_ruleset(port: int, allow: Sequence[str]) -> str:
    """The one fixed nftables table the root helper loads (ADR 0024).

    Only new TCP connections to the USB/IP port are filtered. The script
    recreates the table atomically in one ``nft -f`` transaction. The real
    kernel effect is ⚠ bench-unverified.
    """
    port = _port(port)
    addresses = sorted({_admissible_ipv4(a) for a in allow})
    if len(addresses) > MAX_ADMITTED:
        raise ValueError("too many admitted addresses")
    elements = f"\t\telements = {{ {', '.join(addresses)} }}\n" if addresses else ""
    return (
        f"add table inet {ADMISSION_TABLE}\n"
        f"delete table inet {ADMISSION_TABLE}\n"
        f"table inet {ADMISSION_TABLE} {{\n"
        "\tset consumers4 {\n"
        "\t\ttype ipv4_addr\n"
        f"{elements}"
        "\t}\n"
        "\tchain input {\n"
        "\t\ttype filter hook input priority filter; policy accept;\n"
        f"\t\ttcp dport {port} ct state established,related accept\n"
        f'\t\ttcp dport {port} iifname "lo" accept\n'
        f"\t\ttcp dport {port} ip saddr @consumers4 accept\n"
        f"\t\ttcp dport {port} drop\n"
        "\t}\n"
        "}\n"
    )


def read_admission_request(path: Path, *, port: int) -> tuple[int, list[str]]:
    """Validate an agent-written request at the privilege boundary.

    An absent request means the empty allowlist (closed port). The port
    must equal the helper's own configured port: the agent cannot choose
    which port is filtered.
    """
    try:
        data = _read_private_json(path, "admission request")
    except FileNotFoundError:
        return 0, []
    if not isinstance(data, dict) or set(data) != {
        "schema",
        "generation",
        "port",
        "allow",
        "peers",
    }:
        raise ValueError("admission request has unexpected fields")
    generation = data["generation"]
    if data["schema"] != 1 or type(data["schema"]) is not int:
        raise ValueError("unknown admission request schema")
    if type(generation) is not int or not 0 <= generation < 2**63:
        raise ValueError("invalid admission generation")
    if type(data["port"]) is not int or data["port"] != port:
        raise ValueError(f"admission request port must be {port}")
    allow = data["allow"]
    if not isinstance(allow, list) or len(allow) > MAX_ADMITTED:
        raise ValueError("invalid admission allow list")
    addresses = [_admissible_ipv4(a) for a in allow]
    if len(set(addresses)) != len(addresses):
        raise ValueError("duplicate admission addresses")
    peers = data["peers"]
    if (
        not isinstance(peers, dict)
        or len(peers) > MAX_ADMITTED
        or not all(isinstance(k, str) and len(k) <= 32 for k in peers)
        or not all(isinstance(v, str) and v in addresses for v in peers.values())
    ):
        raise ValueError("invalid admission peer labels")
    return generation, sorted(addresses)


def apply_admission(
    *,
    request: Path,
    status: Path,
    port: int,
    runner: CommandRunner,
    nft: str = "nft",
    timeout: float = 10.0,
) -> int:
    """Root helper body: validate, render, load, then report (exit status).

    An invalid request fails closed: the empty allowlist is loaded and the
    failure is reported, so the producer shows ``error``.
    """
    port = _port(port)
    generation = 0
    allow: list[str] = []
    error: str | None = None
    try:
        generation, allow = read_admission_request(request, port=port)
    except (OSError, ValueError, RecursionError) as exc:
        error = f"rejected admission request: {exc}"
    ruleset = status.with_name(status.name + ".nft")
    try:
        status.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        text = render_admission_ruleset(port, allow)
        fd, temporary = tempfile.mkstemp(prefix=".ruleset.", dir=status.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(text)
            os.replace(temporary, ruleset)
            temporary = ""
        finally:
            if temporary:
                Path(temporary).unlink(missing_ok=True)
        _run(runner, [nft, "-f", str(ruleset)], _timeout(timeout))
    except (OSError, ValueError, DriverError) as exc:
        error = f"{error}; " if error else ""
        error += f"nftables load failed: {exc}"
        allow = []
    report: dict[str, object] = {
        "schema": 1,
        "generation": generation,
        "port": port,
        "allow": allow,
        "applied": error is None,
    }
    if error is not None:
        report["error"] = error[:512]
    try:
        _write_atomic(status, report, 0o644)
    except OSError:
        return 1
    return 0 if error is None else 1


class NoAdmission:
    """Legacy behavior: the USB/IP port stays open to the LAN."""

    enforcing = False

    def apply(self, peers: Peers) -> None:
        """Nothing to enforce."""

    def status(self) -> tuple[NodeState | None, str]:
        return None, ""


class NftablesAdmission:
    """Request allowlists from the root helper; report what it applied.

    The agent writes only a validated data request into its own runtime
    directory; the helper owns the firewall and writes a status file in a
    root-owned directory. Readiness compares the applied allowlist with the
    latest request, so a stale status from an earlier process never counts.
    """

    enforcing = True

    def __init__(
        self,
        *,
        port: int,
        request_path: Path = DEFAULT_ADMISSION_REQUEST,
        status_path: Path = DEFAULT_ADMISSION_STATUS,
        clock: Callable[[], float] = time.monotonic,
        rerequest_after: float = ADMISSION_REREQUEST,
    ) -> None:
        self._port = _port(port)
        self._request_path = request_path
        self._status_path = status_path
        self._clock = clock
        self._rerequest_after = _timeout(rerequest_after)
        self._generation = 0
        self._requested: list[str] | None = None
        self._labels: dict[str, str] = {}
        self._requested_at = 0.0
        self._error: str | None = None

    def apply(self, peers: Peers) -> None:
        labels: dict[str, str] = {}
        for consumer, address in peers:
            try:
                labels[consumer] = _admissible_ipv4(address)
            except ValueError:
                continue  # never widen admission for an unusable address
            if len(labels) == MAX_ADMITTED:
                break
        self._write(sorted(set(labels.values())), labels)

    def _write(self, allow: list[str], labels: dict[str, str]) -> None:
        self._generation += 1
        request = {
            "schema": 1,
            "generation": self._generation,
            "port": self._port,
            "allow": allow,
            "peers": labels,
        }
        try:
            _write_atomic(self._request_path, request, 0o644)
        except OSError as exc:
            self._error = f"admission request write failed: {exc}"
            raise DriverError(self._error) from exc
        self._error = None
        self._requested = allow
        self._labels = labels
        self._requested_at = self._clock()

    def status(self) -> tuple[NodeState | None, str]:
        if self._error is not None:
            return NodeState.ERROR, self._error
        if self._requested is None:
            return NodeState.PENDING, "admission allowlist not yet requested"
        try:
            data = _read_private_json(self._status_path, "admission status")
        except FileNotFoundError:
            return NodeState.PENDING, "admission helper has not reported"
        except (OSError, ValueError, RecursionError) as exc:
            return NodeState.ERROR, f"admission status unreadable: {exc}"
        if not isinstance(data, dict):
            return NodeState.ERROR, "admission status unreadable"
        if data.get("applied") is not True:
            return NodeState.ERROR, f"admission helper failed: {data.get('error')}"
        if data.get("port") != self._port or data.get("allow") != self._requested:
            if self._clock() - self._requested_at >= self._rerequest_after:
                try:
                    self._write(self._requested, self._labels)
                except DriverError as exc:
                    return NodeState.ERROR, str(exc)
            return (
                NodeState.PENDING,
                f"admission allowlist {self._requested} not yet applied",
            )
        return None, f"; admission {len(self._requested)} consumer address(es)"


class UsbipAdmission(Protocol):
    enforcing: bool

    def apply(self, peers: Peers) -> None: ...

    def status(self) -> tuple[NodeState | None, str]: ...


class UsbipBindDriver:
    """Prove export readiness for explicitly configured devices.

    ``export_status`` returns 0 for an absent/unbound device, 1 for available and
    2 for in-use usbip-host exports; other states raise DriverError. The
    default probe reads Linux sysfs; tests inject deterministic alternatives.
    usbipd is independently provisioned, never started implicitly here.
    With ``bindings="external"`` binding belongs to root provisioning (udev
    and ``yoke-usbip-bind@.service``) and this driver never runs ``usbip
    bind``/``unbind`` (ADR 0024). ``admission`` restricts new data sessions to
    the consumers the control plane admitted.
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
        bindings: str = "agent",
        admission: UsbipAdmission | None = None,
        clock: Callable[[], float] = time.monotonic,
        daemon_probe_interval: float = DAEMON_PROBE_INTERVAL,
        daemon_retry_interval: float = DAEMON_RETRY_INTERVAL,
    ) -> None:
        self._host, self._port, self._busids = _validate_media(
            {"host": host, "port": port, "busids": list(busids)}
        )
        if bindings not in ("agent", "external"):
            raise DriverError(f"usbip bindings must be agent or external: {bindings!r}")
        self._admission: UsbipAdmission = admission or NoAdmission()
        if self._admission.enforcing:
            try:
                ipaddress.IPv4Address(self._host)
            except ValueError as exc:
                raise DriverError(
                    "usbip admission requires an IPv4 literal host"
                ) from exc
        self._manage_bindings = bindings == "agent"
        self._runner = runner
        self._timeout = _timeout(timeout)
        self._sysfs_root = sysfs_root
        self._status = export_status or self._sysfs_status
        self._exporting = False
        self._owned_bindings: set[str] = set()
        self._clock = clock
        self._probe_interval = _timeout(daemon_probe_interval)
        self._retry_interval = _timeout(daemon_retry_interval)
        self._probe_due = -math.inf
        self._probe_ok = False
        self._probed_status: dict[str, int] | None = None
        self._exported: set[str] = set()

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

    def admit(self, peers: Peers) -> None:
        """Replace the data-plane admission set (ADR 0024)."""
        self._admission.apply(peers)

    def bind(self) -> None:
        self._exporting = True
        if not self._manage_bindings:
            return
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
        try:
            # Orderly stop closes the data plane to new sessions.
            self._admission.apply(())
        except DriverError as exc:
            errors.append(str(exc))
        for busid in sorted(self._owned_bindings):
            try:
                if self._status(busid):
                    _run(self._runner, [USBIP, "unbind", "-b", busid], self._timeout)
                self._owned_bindings.discard(busid)
            except DriverError as exc:
                errors.append(str(exc))
        if errors:
            raise DriverError("; ".join(errors))

    def _probe_daemon(self, status: dict[str, int]) -> None:
        """Ask usbipd for its export list only when the answer may differ."""
        now = self._clock()
        if now < self._probe_due and (
            not self._probe_ok or status == self._probed_status
        ):
            return
        try:
            output = _run(
                self._runner,
                [USBIP, "--tcp-port", str(self._port), "list", "-r", "127.0.0.1"],
                self._timeout,
            )
        except DriverError:
            self._probe_ok = False
            self._probe_due = now + self._retry_interval
            raise
        self._exported = {
            m[1] for line in output.splitlines() if (m := _EXPORT_RE.match(line))
        }
        self._probed_status = dict(status)
        missing = [b for b, v in status.items() if v == 1 and b not in self._exported]
        self._probe_ok = not missing
        self._probe_due = now + (
            self._probe_interval if self._probe_ok else self._retry_interval
        )

    def poll(self) -> DriverStatus:
        try:
            status = {busid: self._status(busid) for busid in self._busids}
            if (
                self._manage_bindings
                and self._exporting
                and any(value == 0 for value in status.values())
            ):
                # Startup failures and unplug/replug need repair even though
                # the producer core never emits consumer AttachMedia effects.
                self.bind()
                status = {busid: self._status(busid) for busid in self._busids}
            unbound = [b for b, value in status.items() if not value]
            if unbound:
                hint = (
                    ""
                    if self._manage_bindings
                    else "; bindings are provisioned by yoke-usbip-bind@BUSID.service"
                )
                raise DriverError(
                    f"usbip devices not bound to usbip-host: {', '.join(unbound)}"
                    + hint
                )
            self._probe_daemon(status)
            missing = [
                b
                for b, value in status.items()
                if value == 1 and b not in self._exported
            ]
            if missing:
                raise DriverError(
                    f"usbipd is not advertising bound devices: {', '.join(missing)}"
                )
            admission_state, note = self._admission.status()
            if admission_state is not None:
                return DriverStatus(admission_state, note)
            return DriverStatus(
                NodeState.ACTIVE, f"{len(status)} device(s) exported{note}"
            )
        except DriverError as exc:
            return DriverStatus(NodeState.ERROR, str(exc))

    def describe(self) -> Body | None:
        return {"host": self._host, "port": self._port, "busids": list(self._busids)}
