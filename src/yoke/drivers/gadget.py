"""``usb-gadget`` consumer driver: route a remote device to the local UDC.

Metadata only (ADRs 0002, 0011, 0022). The driver never opens the gadget,
the USB/IP socket or any payload: it sends generation-fenced routes to the
separate gadget worker (:mod:`yoke.usb.worker`) over
:mod:`yoke.usb.control` and reports evidence from two sources, the
worker's status and the kernel's UDC sysfs ``state`` attribute.

``ACTIVE`` means the worker presents the requested device *and* the UDC
reports ``configured`` (or ``suspended``): the computer has enumerated and
configured it. It is not proof that an application is using the device.
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable
from pathlib import Path

from yoke.config import AgentConfig, ConfigError
from yoke.core.types import Body, DriverStatus, Kind, NodeState, Role
from yoke.drivers.base import DriverError, positive_timeout
from yoke.usb.control import (
    DEFAULT_SOCKET,
    ControlClient,
    ControlError,
    Route,
    Target,
    WorkerStatus,
    validate_busid,
    validate_source,
)

_UDC_NAME = re.compile(r"[A-Za-z0-9_.:-]{1,127}\Z")
CONFIGURED = frozenset({"configured", "suspended"})


def target_from_media(
    media: Body, busid: str | None, source: str | None = None
) -> Target:
    """Select one device from a producer's advertised USB/IP exports."""
    busids = media.get("busids")
    if not isinstance(busids, list) or not busids or len(busids) > 64:
        raise DriverError(f"usb-gadget media: bad busids: {busids!r}")
    if busid is None:
        if len(busids) != 1:
            raise DriverError(
                f"producer advertises {len(busids)} devices; a gadget presents one. "
                "Set driver.busid"
            )
        busid = busids[0]
    if busid not in busids:
        raise DriverError(f"producer does not advertise busid {busid!r}")
    try:
        return Target(media.get("host", ""), media.get("port", 0), busid, source)
    except ControlError as exc:
        raise DriverError(f"usb-gadget media: {exc}") from exc


class UsbGadgetDriver:
    """Consumer driver for the computer-side box (ADR 0022)."""

    def __init__(
        self,
        *,
        udc: str,
        socket_path: str = DEFAULT_SOCKET,
        busid: str | None = None,
        source: str | None = None,
        ack_timeout: float = 10.0,
        stale_after: float = 5.0,
        sysfs_udc: Path = Path("/sys/class/udc"),
        client: ControlClient | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not _UDC_NAME.fullmatch(udc):
            raise DriverError(f"bad UDC name: {udc!r}")
        if busid is not None:
            try:
                validate_busid(busid)
            except ControlError as exc:
                raise DriverError(str(exc)) from exc
        try:
            self._source = validate_source(source)
        except ControlError as exc:
            raise DriverError(str(exc)) from exc
        self._udc = udc
        self._busid = busid
        self._timeout = positive_timeout(ack_timeout, "usb-gadget ack timeout")
        self._stale_after = positive_timeout(stale_after, "usb-gadget stale bound")
        self._client = client or ControlClient(socket_path, timeout=self._timeout)
        self._state_file = sysfs_udc / udc / "state"
        self._clock = clock
        self._generation: int | None = None
        self._routed = False  # no attach/detach yet: physical route unknown
        self._wanted: Target | None = None
        self._error: str | None = None

    def _udc_state(self) -> str:
        try:
            return self._state_file.read_text()[:64].strip()
        except OSError:
            return "unavailable"

    def _status(self) -> WorkerStatus:
        try:
            return self._client.status()
        except ControlError as exc:
            raise DriverError(str(exc)) from exc

    def _send(self, current: WorkerStatus, target: Target | None) -> WorkerStatus:
        generation = max(current.generation, self._generation or 0) + 1
        try:
            return self._client.route(Route(generation, target))
        except ControlError as exc:
            raise DriverError(str(exc)) from exc

    def attach(self, producer: str, media: Body) -> None:
        """Idempotent: a worker already presenting this target is adopted.

        Re-sending an identical route would tear the session down and make
        the computer re-enumerate, so repeated attaches (including after an
        agent restart) only confirm the worker's evidence.
        """
        target = target_from_media(media, self._busid, self._source)
        self._routed = True
        self._wanted = target
        current = self._status()
        if current.target == target and current.state != "error":
            self._generation = current.generation
            self._error = None
            return
        status = self._send(current, target)
        if status.target != target:
            self._error = (
                f"gadget worker did not adopt route to {target} "
                f"(state {status.state}: {status.detail})"
            )
            raise DriverError(self._error)
        self._generation = status.generation
        self._error = None

    def detach(self) -> None:
        self._routed = True
        self._wanted = None
        current = self._status()
        if current.target is None and current.state == "idle":
            self._generation = current.generation
            self._error = None
            return
        status = self._send(current, None)
        if status.target is not None or status.state != "idle":
            self._error = (
                f"gadget teardown unconfirmed (state {status.state}: {status.detail})"
            )
            raise DriverError(self._error)
        self._generation = status.generation
        self._error = None

    def poll(self) -> DriverStatus:
        try:
            status = self._client.status()
        except ControlError as exc:
            return DriverStatus(NodeState.ERROR, str(exc))
        if self._error:
            return DriverStatus(NodeState.ERROR, self._error)
        if not math.isfinite(status.updated) or (
            abs(self._clock() - status.updated) > self._stale_after
        ):
            return DriverStatus(NodeState.ERROR, "gadget worker status is stale")
        wanted = self._wanted
        if not self._routed:
            if status.target is None:
                return DriverStatus(NodeState.IDLE, "gadget worker idle")
            return DriverStatus(
                NodeState.PENDING, f"worker presents {status.target}; awaiting route"
            )
        if status.generation != self._generation or status.target != wanted:
            return DriverStatus(
                NodeState.ERROR,
                f"gadget worker route lost (generation {status.generation}, "
                f"expected {self._generation}); reapplying",
            )
        if wanted is None:
            return DriverStatus(NodeState.IDLE, "gadget detached")
        udc_state = self._udc_state()
        device = status.device
        what = f"{device.get('vendor', '?')}:{device.get('product', '?')} from {wanted}"
        if status.state in ("rejected", "error"):
            return DriverStatus(NodeState.ERROR, f"{status.state}: {status.detail}")
        if status.state in CONFIGURED and udc_state in CONFIGURED:
            return DriverStatus(
                NodeState.ACTIVE,
                f"{what} configured at {status.link_speed or 'unknown speed'}; "
                f"UDC {self._udc} state {udc_state}; application use unverified",
            )
        if status.state == "connecting":
            return DriverStatus(
                NodeState.PENDING, f"connecting to {wanted}: {status.detail}"
            )
        return DriverStatus(
            NodeState.PENDING,
            f"{what} presented; awaiting computer enumeration "
            f"(worker {status.state}, UDC {udc_state})",
        )

    def describe(self) -> Body | None:
        return None


def _number(table: dict[str, object], key: str, default: float) -> float:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"driver.{key}: expected a finite positive number")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ConfigError(f"driver.{key}: number is too large") from exc
    if not math.isfinite(number) or number <= 0:
        raise ConfigError(f"driver.{key}: expected a finite positive number")
    return number


def build_usb_gadget_driver(cfg: AgentConfig) -> UsbGadgetDriver:
    """Validate ``[driver]`` for ``type = "usb-gadget"``."""
    if cfg.node.role is not Role.CONSUMER or cfg.node.kind is not Kind.USB:
        raise ConfigError("driver.type: usb-gadget requires a usb consumer")
    table = cfg.driver
    known = {"type", "udc", "socket", "busid", "source", "ack_timeout", "stale_after"}
    unknown = sorted(set(table) - known)
    if unknown:
        raise ConfigError(f"driver.{unknown[0]}: unknown usb-gadget key")
    udc = table.get("udc")
    if not isinstance(udc, str) or not _UDC_NAME.fullmatch(udc):
        raise ConfigError("driver.udc: required UDC name, e.g. fcc00000.usb")
    socket_path = table.get("socket", DEFAULT_SOCKET)
    if not isinstance(socket_path, str) or not socket_path.startswith("/"):
        raise ConfigError("driver.socket: expected an absolute path")
    busid = table.get("busid")
    if busid is not None:
        try:
            validate_busid(busid)
        except ControlError as exc:
            raise ConfigError(f"driver.busid: {exc}") from exc
    # Producer admission accepts the address this consumer announces from.
    source = table.get("source")
    if source is None and cfg.interface != "0.0.0.0":
        source = cfg.interface
    try:
        source = validate_source(source)
    except ControlError as exc:
        raise ConfigError(f"driver.source: {exc}") from exc
    return UsbGadgetDriver(
        udc=udc,
        socket_path=socket_path,
        busid=busid if isinstance(busid, str) else None,
        source=source,
        ack_timeout=_number(table, "ack_timeout", 10.0),
        stale_after=_number(table, "stale_after", 5.0),
    )
