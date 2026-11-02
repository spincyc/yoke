"""Optional systemd startup readiness over its inherited Unix datagram socket.

The local control runtime owns readiness. Endpoint/media health continues
through ordinary driver status; it does not gate service startup.
"""

from __future__ import annotations

import logging
import os
import socket

_log = logging.getLogger("yoke.agent")


class SystemdNotifier:
    """A captured NOTIFY_SOCKET, removed before drivers can spawn children."""

    def __init__(self, address: str | None) -> None:
        self._address: str | None = None
        if not address:
            return
        if address.startswith("@") and len(address) > 1:
            self._address = "\0" + address[1:]
        elif address.startswith("/"):
            self._address = address
        else:
            _log.warning("ignoring invalid NOTIFY_SOCKET address")

    @classmethod
    def capture(cls) -> SystemdNotifier:
        return cls(os.environ.pop("NOTIFY_SOCKET", None))

    def ready(self) -> None:
        if self._address is None:
            return
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
                sock.setblocking(False)
                sock.sendto(b"READY=1", self._address)
        except (OSError, ValueError) as exc:
            _log.warning("service readiness notification failed: %s", exc)
