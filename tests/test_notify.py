"""Standard startup notification without root or a running service manager."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from yoke.net.notify import SystemdNotifier


def test_filesystem_notify_socket_sends_ready_and_removes_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = str(tmp_path.parent / ("notify-" + uuid.uuid4().hex[:8]))
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
        receiver.bind(path)
        receiver.settimeout(0.5)
        monkeypatch.setenv("NOTIFY_SOCKET", path)
        notifier = SystemdNotifier.capture()
        assert "NOTIFY_SOCKET" not in os.environ
        notifier.ready()
        assert receiver.recv(64) == b"READY=1"


@pytest.mark.skipif(sys.platform != "linux", reason="Linux abstract Unix sockets")
def test_abstract_notify_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    name = "yoke-notify-test-" + uuid.uuid4().hex
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
        receiver.bind("\0" + name)
        receiver.settimeout(0.5)
        monkeypatch.setenv("NOTIFY_SOCKET", "@" + name)
        SystemdNotifier.capture().ready()
        assert receiver.recv(64) == b"READY=1"
        assert "NOTIFY_SOCKET" not in os.environ


def test_captured_socket_is_not_inherited_by_external_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOTIFY_SOCKET", str(tmp_path / "notify.sock"))
    SystemdNotifier.capture()
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os, sys; sys.exit('NOTIFY_SOCKET' in os.environ)",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=5,
    )
    assert result.returncode == 0


@pytest.mark.parametrize("address", [None, "", "relative.sock", "@"])
def test_absent_or_invalid_address_has_no_socket_side_effect(
    address: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    if address is None:
        monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    else:
        monkeypatch.setenv("NOTIFY_SOCKET", address)

    def unexpected_socket(*args: object, **kwargs: object) -> None:
        raise AssertionError("notification socket must not be opened")

    monkeypatch.setattr("yoke.net.notify.socket.socket", unexpected_socket)
    SystemdNotifier.capture().ready()
    assert "NOTIFY_SOCKET" not in os.environ


@pytest.mark.parametrize("address", ["missing.sock", "bad\0path"])
def test_optional_notification_failure_is_logged(
    address: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    notifier = SystemdNotifier(str(tmp_path / address))
    notifier.ready()
    assert "service readiness notification failed" in caplog.text
