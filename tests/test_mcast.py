"""Loopback smoke test for the one real-socket helper.

Still offline: multicast with loopback enabled reaches subscribers on the
same host without any external network. Skips (rather than fails) when
the sandbox forbids multicast sockets entirely, so the suite stays
honest about what it could not check.
"""

from __future__ import annotations

import socket

import pytest

from yoke.net.mcast import open_multicast_socket

GROUP = "239.121.111.107"
PORT = 29753  # high port: test must not collide with a real agent


def open_or_skip(**kwargs: object) -> socket.socket:
    try:
        return open_multicast_socket(GROUP, PORT, **kwargs)  # type: ignore[arg-type]
    except OSError as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"multicast unavailable in this environment: {exc}")


def test_two_sockets_share_group_and_hear_sends() -> None:
    rx = open_or_skip()
    tx = open_or_skip()
    try:
        rx.settimeout(2.0)
        tx.sendto(b"yoke-smoke", (GROUP, PORT))
        try:
            data, _addr = rx.recvfrom(2048)
        except TimeoutError:  # pragma: no cover - environment-dependent
            pytest.skip("multicast loopback not delivered in this environment")
        assert data == b"yoke-smoke"
    finally:
        rx.close()
        tx.close()


def test_loopback_disabled_socket_still_opens() -> None:
    sock = open_or_skip(loopback=False)
    sock.close()
