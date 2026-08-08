"""Fully in-memory driver for tests and simulation.

``SimDriver`` implements the :class:`~yoke.drivers.base.Driver` protocol
with no I/O at all. It records every attach/detach call in order and can
be scripted to fail in every way the failure model cares about (ADR
0006): attach failures for the first N attempts, a one-shot detach
failure, a forced poll status, and a canned sequence of flaky poll
results. A producer-mode instance is constructed with the ``media`` dict
it should advertise via :meth:`describe`.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from yoke.core.types import Body, DriverStatus, NodeState
from yoke.drivers.base import DriverError

AttachCall = tuple[str, str, Body]
DetachCall = tuple[str]
Call = AttachCall | DetachCall


@dataclass
class SimDriver:
    """Scriptable in-memory Driver.

    Default healthy behavior: ``poll()`` reports ``IDLE`` before any
    attach, ``ACTIVE`` after a successful attach, and ``IDLE`` again
    after detach. Attaching while already attached is an idempotent
    re-attach: it succeeds and is recorded like any other call.

    Call recording is ordering-only (no timestamps): :attr:`calls`
    holds ``("attach", producer, media)`` and ``("detach",)`` tuples in
    invocation order, including calls that were scripted to fail.
    """

    media: Body | None = None
    calls: list[Call] = field(default_factory=list)
    attached_to: str | None = None
    attached_media: Body | None = None

    _fail_attach_remaining: int = 0
    _fail_detach_once: bool = False
    _forced_status: DriverStatus | None = None
    _flaky_poll: deque[DriverStatus] = field(default_factory=deque)
    _last_error: str = ""

    # -- scripting -----------------------------------------------------

    def fail_attach_times(self, n: int) -> None:
        """Make the next ``n`` attach calls raise :class:`DriverError`."""
        self._fail_attach_remaining = n

    def fail_detach_once(self) -> None:
        """Make the next detach call raise :class:`DriverError`."""
        self._fail_detach_once = True

    def set_status(self, status: DriverStatus | None) -> None:
        """Force ``poll()`` to return ``status``; ``None`` restores the
        default state-derived behavior."""
        self._forced_status = status

    def flaky_poll(self, statuses: list[DriverStatus]) -> None:
        """Queue statuses that successive ``poll()`` calls return (in
        order) before default behavior resumes."""
        self._flaky_poll.extend(statuses)

    # -- Driver protocol -----------------------------------------------

    def attach(self, producer: str, media: Body) -> None:
        self.calls.append(("attach", producer, media))
        if self._fail_attach_remaining > 0:
            self._fail_attach_remaining -= 1
            self._last_error = f"scripted attach failure for {producer!r}"
            raise DriverError(self._last_error)
        self.attached_to = producer
        self.attached_media = media
        self._last_error = ""

    def detach(self) -> None:
        self.calls.append(("detach",))
        if self._fail_detach_once:
            self._fail_detach_once = False
            self._last_error = "scripted detach failure"
            raise DriverError(self._last_error)
        self.attached_to = None
        self.attached_media = None
        self._last_error = ""

    def poll(self) -> DriverStatus:
        if self._forced_status is not None:
            return self._forced_status
        if self._flaky_poll:
            return self._flaky_poll.popleft()
        if self._last_error:
            return DriverStatus(NodeState.ERROR, self._last_error)
        if self.attached_to is not None:
            return DriverStatus(NodeState.ACTIVE, f"attached to {self.attached_to}")
        return DriverStatus(NodeState.IDLE)

    def describe(self) -> Body | None:
        return self.media
