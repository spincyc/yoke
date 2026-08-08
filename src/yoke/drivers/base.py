"""Media-plane driver contract.

A driver owns the translation from yoke's abstract pairing to one
device's native mechanism (IGMP join, appliance API call, ``usbip``
attach). Drivers are synchronous and non-blocking-fast, or they raise;
the runner isolates slow device I/O with a thread. Drivers must never
receive or inspect media payload (ADR 0002/0004).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from yoke.core.types import Body, DriverStatus


class DriverError(Exception):
    """Attach/detach failed. The core turns this into evt driver-error
    plus a capped-backoff retry; it never crashes the agent."""


@runtime_checkable
class Driver(Protocol):
    def attach(self, producer: str, media: Body) -> None:
        """Start consuming ``producer``'s media described by ``media``."""

    def detach(self) -> None:
        """Stop consuming. Must be idempotent."""

    def poll(self) -> DriverStatus:
        """Report current media-plane state (ACTIVE/PENDING/ERROR)."""

    def describe(self) -> Body | None:
        """Producer side: the ``media`` dict to advertise in announces
        (multicast group/port for HDMI; host/port/busids for USB).
        Consumer side: return None."""
