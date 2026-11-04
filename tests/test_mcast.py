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


@pytest.mark.parametrize(
    "group,port,kwargs",
    [
        ("127.0.0.1", PORT, {}),
        ("239.1.2.3/24", PORT, {}),
        ("239.1", PORT, {}),
        ("ff02::1", PORT, {}),
        (GROUP, 0, {}),
        (GROUP, 65536, {}),
        (GROUP, True, {}),
        (GROUP, PORT, {"interface": "eth0"}),
        (GROUP, PORT, {"interface": "239.1.1.1"}),
        (GROUP, PORT, {"interface": "255.255.255.255"}),
        (GROUP, PORT, {"ttl": 2}),
        (GROUP, PORT, {"ttl": True}),
        (GROUP, PORT, {"loopback": 1}),
    ],
)
def test_invalid_inputs_never_allocate_socket(
    group: str, port: int, kwargs: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object, **options: object) -> socket.socket:
        raise AssertionError("socket allocated before validation")

    monkeypatch.setattr("yoke.net.mcast.socket.socket", forbidden)
    with pytest.raises(ValueError):
        open_multicast_socket(group, port, **kwargs)  # type: ignore[arg-type]


def test_setup_failure_closes_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingSocket:
        closed = False

        def setsockopt(self, *args: object) -> None:
            raise OSError("injected socket setup failure")

        def close(self) -> None:
            self.closed = True

    sock = FailingSocket()
    monkeypatch.setattr("yoke.net.mcast.socket.socket", lambda *args: sock)
    with pytest.raises(OSError, match="injected"):
        open_multicast_socket(GROUP, PORT)
    assert sock.closed


def test_linux_membership_is_scoped_to_requested_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RecordingSocket:
        def __init__(self) -> None:
            self.options: list[tuple[object, ...]] = []

        def setsockopt(self, *args: object) -> None:
            self.options.append(args)

        def bind(self, address: object) -> None:
            pass

    sock = RecordingSocket()
    monkeypatch.setattr("yoke.net.mcast.sys.platform", "linux")
    monkeypatch.setattr("yoke.net.mcast.socket.socket", lambda *args: sock)
    opened = open_multicast_socket(GROUP, PORT, interface="127.0.0.1")
    assert opened is sock
    assert (
        socket.IPPROTO_IP,
        getattr(socket, "IP_MULTICAST_ALL", 49),
        0,
    ) in sock.options
    assert (
        socket.IPPROTO_IP,
        socket.IP_MULTICAST_IF,
        socket.inet_aton("127.0.0.1"),
    ) in sock.options


def test_other_group_on_shared_port_is_not_received() -> None:
    import sys

    if not sys.platform.startswith("linux"):
        pytest.skip("Linux multicast-all isolation")
    rx = open_or_skip(interface="127.0.0.1")
    try:
        other = open_multicast_socket("239.121.111.108", PORT, interface="127.0.0.1")
        try:
            rx.settimeout(1.0)
            other.settimeout(0.05)
            rx.sendto(b"isolated-group", (GROUP, PORT))
            try:
                data, _ = rx.recvfrom(2048)
            except TimeoutError:
                pytest.skip("loopback multicast unavailable")
            assert data == b"isolated-group"
            with pytest.raises(TimeoutError):
                other.recvfrom(2048)
        finally:
            other.close()
    finally:
        rx.close()


def test_unicast_to_control_port_is_not_received() -> None:
    """ADR 0023: the control socket reads only group-addressed datagrams."""
    rx = open_or_skip(interface="127.0.0.1")
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        rx.settimeout(1.0)
        tx.sendto(b"unicast-probe", ("127.0.0.1", PORT))
        rx.sendto(b"group-probe", (GROUP, PORT))
        try:
            data, _ = rx.recvfrom(2048)
        except TimeoutError:  # pragma: no cover - environment-dependent
            pytest.skip("loopback multicast unavailable")
        # The group datagram arrives; the earlier unicast one never does.
        assert data == b"group-probe"
        rx.settimeout(0.1)
        with pytest.raises(TimeoutError):
            rx.recvfrom(2048)
    finally:
        tx.close()
        rx.close()


def test_socket_binds_group_address(monkeypatch: pytest.MonkeyPatch) -> None:
    bound: list[object] = []

    class RecordingSocket:
        def setsockopt(self, *args: object) -> None:
            pass

        def bind(self, address: object) -> None:
            bound.append(address)

    monkeypatch.setattr("yoke.net.mcast.sys.platform", "linux")
    monkeypatch.setattr("yoke.net.mcast.socket.socket", lambda *a: RecordingSocket())
    open_multicast_socket(GROUP, PORT, interface="127.0.0.1")
    assert bound == [(GROUP, PORT)]
