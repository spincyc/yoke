"""USB packet-level transfer semantics shared by every simulated component.

A bulk or interrupt transfer is a sequence of packets of at most the
endpoint's maximum packet size. A receiving buffer of size ``S`` completes
when it is full or when a short packet (including a zero-length packet)
arrives. A sender appends a zero-length packet only when asked to and the
data length is a non-zero multiple of the packet size. These rules are the
oracle against which the proxy's packet-exact forwarding is judged.
"""

from __future__ import annotations

from dataclasses import dataclass, field


def packetize(data: bytes, packet_size: int, *, zlp: bool = False) -> list[bytes]:
    """Split one transfer into packets."""
    if packet_size <= 0:
        raise ValueError("packet size must be positive")
    if not data:
        return [b""]
    packets = [data[i : i + packet_size] for i in range(0, len(data), packet_size)]
    if zlp and len(data) % packet_size == 0:
        packets.append(b"")
    return packets


class BabbleError(Exception):
    """A packet larger than the remaining receive space (babble)."""


@dataclass
class Receiver:
    """Accumulate packets into a buffer of ``capacity`` bytes."""

    capacity: int
    packet_size: int
    data: bytearray = field(default_factory=bytearray)
    done: bool = False
    short: bool = False

    def feed(self, packet: bytes) -> None:
        """Consume one packet; raise :class:`BabbleError` on babble."""
        if self.done:
            raise RuntimeError("receiver already complete")
        if len(packet) > self.packet_size:
            raise BabbleError(f"packet of {len(packet)} exceeds {self.packet_size}")
        if len(self.data) + len(packet) > self.capacity:
            raise BabbleError("packet exceeds remaining transfer length")
        self.data += packet
        if len(packet) < self.packet_size:
            self.done = self.short = True
        elif len(self.data) == self.capacity:
            self.done = True
