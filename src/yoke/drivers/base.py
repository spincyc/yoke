"""Media-plane driver contract.

A driver owns the translation from yoke's abstract pairing to one
device's native mechanism (IGMP join, appliance API call, ``usbip``
attach). Drivers are synchronous and non-blocking-fast, or they raise;
the runner isolates slow device I/O with a thread. Drivers must never
receive or inspect media payload (ADR 0002/0004).
"""

from __future__ import annotations

import math
from typing import Protocol, runtime_checkable

from yoke.core.types import Body, DriverStatus


class DriverError(Exception):
    """Attach/detach failed. The core turns this into evt driver-error
    plus a capped-backoff retry; it never crashes the agent."""


def positive_timeout(value: object, name: str) -> float:
    """Validate a bounded driver I/O deadline without bool coercion."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DriverError(f"{name} must be finite and positive")
    try:
        timeout = float(value)
    except OverflowError as exc:
        raise DriverError(f"{name} must be finite and positive") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise DriverError(f"{name} must be finite and positive")
    return timeout


@runtime_checkable
class Driver(Protocol):
    def attach(self, producer: str, media: Body) -> None:
        """Start consuming ``producer``'s media described by ``media``."""

    def detach(self) -> None:
        """Stop consuming; repeated success is harmless.

        Raise DriverError and retain ownership when teardown is uncertain.
        A replacement must not start until the old resource is quiescent.
        """

    def poll(self) -> DriverStatus:
        """Report attachment state and describe the evidence in detail.

        ACTIVE confirms backend readiness, not observed media payload flow.
        """

    def describe(self) -> Body | None:
        """Producer side: the ``media`` dict to advertise in announces
        (multicast group/port for HDMI; host/port/busids for USB).
        Consumer side: return None."""
