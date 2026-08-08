"""usbip drivers: USB-over-IP via the standard Linux ``usbip`` tool.

The producer side (:class:`UsbipBindDriver`) binds physical devices to
the ``usbip-host`` driver so ``usbipd`` can export them, and advertises
``{"host", "port", "busids"}`` as its media description. The consumer
side (:class:`UsbipAttachDriver`) attaches those exported devices,
presenting them to the local USB stack. Sessions are consumer-initiated,
matching yoke's pairing model (ARCHITECTURE.md); yoke never touches the
USB payload (ADR 0002).

All process execution goes through an injected
:class:`~yoke.drivers.shellcmd.CommandRunner`.

Real-world command output this module parses (linux-tools ``usbip``):

``usbip port`` — imported devices on the consumer::

    Imported USB devices
    ====================
    Port 00: <Port in Use> at High Speed(480Mbps)
           unknown vendor : unknown product (046d:c31c)
           7-1 -> usbip://192.168.7.10:3240/1-1.2
               -> remote bus/dev 001/003

Some tool versions print ``Port 00`` without the trailing colon; both
variants are handled. With nothing attached, only the two header lines
(or empty output) appear. The ``usbip://<host>:<port>/<busid>`` URL line
identifies which remote export backs each local port.

``usbip list -l`` — local exportable devices on the producer::

     - busid 1-1 (0557:2419)
       ATEN International Co., Ltd : unknown product (0557:2419)

     - busid 1-1.2 (046d:c31c)
       Logitech, Inc. : Keyboard K120 (046d:c31c)
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from yoke.core.types import Body, DriverStatus, NodeState
from yoke.drivers.base import DriverError
from yoke.drivers.shellcmd import CommandRunner

USBIP = "usbip"
DEFAULT_USBIP_PORT = 3240

# "Port 00: <Port in Use> ..." — trailing colon optional across versions.
_PORT_RE = re.compile(r"^Port\s+(\d+):?(?:\s|$)")
# "7-1 -> usbip://192.168.7.10:3240/1-1.2"
_REMOTE_RE = re.compile(r"usbip://(?P<host>[^:/\s]+):(?P<port>\d+)/(?P<busid>\S+)")
# " - busid 1-1 (0557:2419)"
_BUSID_RE = re.compile(r"^\s*-\s+busid\s+(\S+)")


@dataclass(frozen=True)
class _ImportedPort:
    port: int
    host: str | None  # None while the URL line has not been seen/printed
    busid: str | None


def _validate_media(media: Body) -> tuple[str, int, list[str]]:
    """Check the producer-advertised media shape; DriverError if bad."""
    host = media.get("host")
    port = media.get("port")
    busids = media.get("busids")
    if not isinstance(host, str) or not host:
        raise DriverError(f"usbip media: bad host: {host!r}")
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
        raise DriverError(f"usbip media: bad port: {port!r}")
    if (
        not isinstance(busids, list)
        or not busids
        or not all(isinstance(b, str) and b for b in busids)
    ):
        raise DriverError(f"usbip media: bad busids: {busids!r}")
    return host, port, list(busids)


def _parse_port_output(output: str) -> list[_ImportedPort]:
    """Parse ``usbip port`` output into imported-port records.

    Raises :class:`DriverError` when the output matches no known shape
    (neither blank, nor the standard header, nor any ``Port NN`` line).
    """
    ports: list[_ImportedPort] = []
    saw_header = False
    for line in output.splitlines():
        stripped = line.strip()
        if stripped in ("Imported USB devices", "===================="):
            saw_header = True
            continue
        port_match = _PORT_RE.match(stripped)
        if port_match:
            ports.append(_ImportedPort(int(port_match.group(1)), None, None))
            continue
        remote_match = _REMOTE_RE.search(stripped)
        if remote_match and ports:
            last = ports[-1]
            ports[-1] = _ImportedPort(
                last.port,
                remote_match.group("host"),
                remote_match.group("busid"),
            )
    if not ports and not saw_header and output.strip():
        raise DriverError(f"unparseable `usbip port` output: {output.strip()[:200]!r}")
    return ports


class UsbipAttachDriver:
    """Consumer-side driver: attach a producer's exported USB devices.

    ``media`` (from the producer's announce) must be
    ``{"host": str, "port": int, "busids": [str, ...]}``. Note the
    stock ``usbip attach`` client always dials the daemon's default TCP
    port; the advertised ``port`` is validated and recorded but not
    passed on the command line.
    """

    def __init__(self, runner: CommandRunner, *, timeout: float = 10.0) -> None:
        self._runner = runner
        self._timeout = timeout
        self._host: str | None = None
        self._busids: list[str] = []

    def attach(self, producer: str, media: Body) -> None:
        host, _port, busids = _validate_media(media)
        for busid in busids:
            result = self._runner.run(
                [USBIP, "attach", "-r", host, "-b", busid],
                timeout=self._timeout,
            )
            if result.returncode != 0:
                raise DriverError(
                    f"usbip attach {busid} from {host} (producer {producer}) "
                    f"failed rc={result.returncode}: {result.stderr.strip()}"
                )
        self._host = host
        self._busids = busids

    def detach(self) -> None:
        """Detach every imported port. Idempotent when none are attached."""
        result = self._runner.run([USBIP, "port"], timeout=self._timeout)
        if result.returncode != 0:
            raise DriverError(
                f"usbip port failed rc={result.returncode}: {result.stderr.strip()}"
            )
        ports = _parse_port_output(result.stdout)
        for imported in ports:
            detach_result = self._runner.run(
                [USBIP, "detach", "-p", str(imported.port)],
                timeout=self._timeout,
            )
            if detach_result.returncode != 0:
                raise DriverError(
                    f"usbip detach -p {imported.port} failed "
                    f"rc={detach_result.returncode}: {detach_result.stderr.strip()}"
                )
        self._host = None
        self._busids = []

    def poll(self) -> DriverStatus:
        """ACTIVE when every requested busid is imported; PENDING when
        attached but only partially (or not yet) imported; IDLE when
        detached and nothing is imported; ERROR on command failure or
        unparseable output. Never raises."""
        try:
            result = self._runner.run([USBIP, "port"], timeout=self._timeout)
            if result.returncode != 0:
                return DriverStatus(
                    NodeState.ERROR,
                    f"usbip port rc={result.returncode}: {result.stderr.strip()}",
                )
            ports = _parse_port_output(result.stdout)
        except DriverError as exc:
            return DriverStatus(NodeState.ERROR, str(exc))
        imported = {p.busid for p in ports if p.host == self._host}
        if not self._busids:
            if ports:
                return DriverStatus(
                    NodeState.ERROR, f"{len(ports)} port(s) imported while detached"
                )
            return DriverStatus(NodeState.IDLE)
        missing = [b for b in self._busids if b not in imported]
        if not missing:
            return DriverStatus(NodeState.ACTIVE, f"{len(self._busids)} device(s)")
        return DriverStatus(NodeState.PENDING, f"awaiting busids: {', '.join(missing)}")

    def describe(self) -> Body | None:
        return None


class UsbipBindDriver:
    """Producer-side driver: export local devices via ``usbipd``.

    Producers do not attach — pairing is consumer-initiated — so
    :meth:`attach` always raises. The lifecycle verbs here are
    :meth:`bind` / :meth:`unbind`, run once at agent start/stop.
    :meth:`describe` supplies the media dict advertised in announces.
    """

    def __init__(
        self,
        runner: CommandRunner,
        *,
        host: str,
        port: int = DEFAULT_USBIP_PORT,
        busids: Sequence[str],
        timeout: float = 10.0,
    ) -> None:
        if not busids:
            raise DriverError("usbip bind driver needs at least one busid")
        self._runner = runner
        self._host = host
        self._port = port
        self._busids = list(busids)
        self._timeout = timeout

    def attach(self, producer: str, media: Body) -> None:
        raise DriverError("producer driver: attach is consumer-side only")

    def detach(self) -> None:
        """No-op (idempotent): consumers detach; producers keep exporting."""

    def bind(self) -> None:
        """Bind each busid to usbip-host (``usbip bind -b <busid>``)."""
        for busid in self._busids:
            result = self._runner.run(
                [USBIP, "bind", "-b", busid], timeout=self._timeout
            )
            if result.returncode != 0:
                raise DriverError(
                    f"usbip bind {busid} failed rc={result.returncode}: "
                    f"{result.stderr.strip()}"
                )

    def unbind(self) -> None:
        """Return each busid to its normal driver (``usbip unbind -b``)."""
        for busid in self._busids:
            result = self._runner.run(
                [USBIP, "unbind", "-b", busid], timeout=self._timeout
            )
            if result.returncode != 0:
                raise DriverError(
                    f"usbip unbind {busid} failed rc={result.returncode}: "
                    f"{result.stderr.strip()}"
                )

    def poll(self) -> DriverStatus:
        """ACTIVE when every exported busid is present in the local
        device list (``usbip list -l``); ERROR (with the missing ids or
        the failure) otherwise. Never raises."""
        try:
            result = self._runner.run([USBIP, "list", "-l"], timeout=self._timeout)
        except DriverError as exc:
            return DriverStatus(NodeState.ERROR, str(exc))
        if result.returncode != 0:
            return DriverStatus(
                NodeState.ERROR,
                f"usbip list -l rc={result.returncode}: {result.stderr.strip()}",
            )
        present = {
            m.group(1)
            for line in result.stdout.splitlines()
            if (m := _BUSID_RE.match(line))
        }
        missing = [b for b in self._busids if b not in present]
        if missing:
            return DriverStatus(
                NodeState.ERROR, f"busids not present locally: {', '.join(missing)}"
            )
        return DriverStatus(NodeState.ACTIVE, f"{len(self._busids)} device(s) local")

    def describe(self) -> Body | None:
        return {"host": self._host, "port": self._port, "busids": list(self._busids)}
