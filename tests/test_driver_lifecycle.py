"""Deterministic OS failures and resource ownership, without media I/O."""

from __future__ import annotations

import signal
import socket
import subprocess
from types import SimpleNamespace

import pytest

from yoke.core.types import NodeState
from yoke.drivers.base import DriverError
from yoke.drivers.igmp import IgmpJoinDriver
from yoke.drivers.shellcmd import SubprocessRunner
from yoke.drivers.stream import StreamDriver, SubprocessSupervisor


class TestCommandFailures:
    @pytest.mark.parametrize(
        "argv", [[], "echo unsafe", [""], ["echo", 1], ["echo", "bad\x00arg"]]
    )
    def test_invalid_argv_never_starts_a_command(
        self, monkeypatch: pytest.MonkeyPatch, argv: list[str]
    ) -> None:
        def forbidden(*args: object, **kwargs: object) -> None:
            raise AssertionError("subprocess must not be called")

        monkeypatch.setattr(subprocess, "run", forbidden)
        with pytest.raises(DriverError, match="command"):
            SubprocessRunner().run(argv, timeout=1.0)

    @pytest.mark.parametrize(
        "timeout", [0, -1, True, float("nan"), float("inf"), 10**1000]
    )
    def test_invalid_timeout_never_starts_a_command(
        self, monkeypatch: pytest.MonkeyPatch, timeout: float
    ) -> None:
        def forbidden(*args: object, **kwargs: object) -> None:
            raise AssertionError("subprocess must not be called")

        monkeypatch.setattr(subprocess, "run", forbidden)
        with pytest.raises(DriverError, match="finite and positive"):
            SubprocessRunner().run(["usbip", "port"], timeout=timeout)

    @pytest.mark.parametrize(
        "error", [PermissionError("denied"), OSError("exec format")]
    )
    def test_os_errors_use_the_driver_error_boundary(
        self, monkeypatch: pytest.MonkeyPatch, error: OSError
    ) -> None:
        def fail(*args: object, **kwargs: object) -> None:
            raise error

        monkeypatch.setattr(subprocess, "run", fail)
        with pytest.raises(DriverError, match=str(error)):
            SubprocessRunner().run(["usbip", "port"], timeout=1.0)

    def test_timeout_is_a_driver_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fail(*args: object, **kwargs: object) -> None:
            raise subprocess.TimeoutExpired("usbip", 0.1)

        monkeypatch.setattr(subprocess, "run", fail)
        with pytest.raises(DriverError, match=r"timed out after 0\.1s"):
            SubprocessRunner().run(["usbip", "port"], timeout=0.1)

    def test_pipeline_start_os_error_is_a_driver_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fail(*args: object, **kwargs: object) -> None:
            raise PermissionError("pipeline denied")

        monkeypatch.setattr("yoke.drivers.stream.shutil.which", lambda _: "/pipeline")
        monkeypatch.setattr(subprocess, "Popen", fail)
        with pytest.raises(DriverError, match="pipeline denied"):
            SubprocessSupervisor().start(["pipeline"])


class Membership:
    """No receive method: a health check cannot inspect a media packet."""

    def __init__(self, *, join_fails: bool = False, close_fails: bool = False) -> None:
        self.join_fails = join_fails
        self.close_fails = close_fails
        self.closed = False

    def setsockopt(self, level: int, optname: int, value: int | bytes, /) -> None:
        if optname == socket.IP_ADD_MEMBERSHIP and self.join_fails:
            raise OSError("membership denied")

    def bind(self, address: tuple[str, int], /) -> None:
        pass

    def close(self) -> None:
        if self.close_fails:
            raise OSError("close unconfirmed")
        self.closed = True


GROUP = {"group": "239.10.0.7", "port": 5004}


class TestMembershipOwnership:
    def test_socket_creation_failure_is_reported(self) -> None:
        def fail() -> Membership:
            raise OSError("no descriptors")

        driver = IgmpJoinDriver(fail)
        with pytest.raises(DriverError, match="no descriptors"):
            driver.attach("prod-a", GROUP)
        assert driver.poll().state is NodeState.ERROR

    def test_failed_leave_blocks_another_join_until_retry(self) -> None:
        first = Membership(close_fails=True)
        second = Membership()
        created: list[Membership] = []

        def factory() -> Membership:
            sock = first if not created else second
            created.append(sock)
            return sock

        driver = IgmpJoinDriver(factory)
        driver.attach("prod-a", GROUP)
        with pytest.raises(DriverError, match="teardown unconfirmed"):
            driver.attach("prod-b", {**GROUP, "group": "239.10.0.8"})
        assert created == [first]
        assert driver.poll().state is NodeState.ERROR
        first.close_fails = False
        driver.attach("prod-b", {**GROUP, "group": "239.10.0.8"})
        assert first.closed
        assert created == [first, second]
        assert driver.poll().state is NodeState.ACTIVE
        assert (
            driver.poll().detail == "member of 239.10.0.8:5004; media flow unverified"
        )

    def test_failed_join_cleanup_retains_the_socket_when_close_fails(self) -> None:
        sock = Membership(join_fails=True, close_fails=True)
        driver = IgmpJoinDriver(lambda: sock)
        with pytest.raises(DriverError, match="socket teardown unconfirmed"):
            driver.attach("prod-a", GROUP)
        assert driver.poll().state is NodeState.ERROR
        sock.close_fails = False
        driver.detach()
        assert sock.closed
        assert driver.poll().state is NodeState.IDLE


class FinishedLeader:
    """The leader exited; an external worker can still occupy its group."""

    pid = 12345
    args = ("fake-pipeline",)

    def poll(self) -> int:
        return 0

    def wait(self, timeout: float) -> int:
        return 0


class TestPipelineGroup:
    @pytest.mark.parametrize("kill_works", [True, False])
    def test_exited_leader_does_not_hide_a_lingering_worker(
        self, monkeypatch: pytest.MonkeyPatch, kill_works: bool
    ) -> None:
        from yoke.drivers import stream

        alive = True
        signals: list[int] = []
        launches: list[dict[str, object]] = []
        clock = 0.0

        def killpg(pid: int, sig: int) -> None:
            nonlocal alive
            assert pid == FinishedLeader.pid
            if not alive:
                raise ProcessLookupError
            if sig:
                signals.append(sig)
            if sig == signal.SIGKILL and kill_works:
                alive = False

        def monotonic() -> float:
            nonlocal clock
            clock += 1.0
            return clock

        def popen(*args: object, **kwargs: object) -> FinishedLeader:
            launches.append(kwargs)
            return FinishedLeader()

        monkeypatch.setattr(stream, "os", SimpleNamespace(name="posix", killpg=killpg))
        monkeypatch.setattr(
            stream, "time", SimpleNamespace(monotonic=monotonic, sleep=lambda _: None)
        )
        monkeypatch.setattr(stream.shutil, "which", lambda _: "/fake-pipeline")
        monkeypatch.setattr(subprocess, "Popen", popen)
        driver = StreamDriver(
            SubprocessSupervisor(),
            command_builder=lambda *_: ["fake-pipeline"],
            stop_timeout=0.1,
        )
        driver.attach("prod-a", {})
        assert driver.poll().state is NodeState.ERROR
        assert "exited with code 0" in driver.poll().detail
        assert launches[0]["start_new_session"] is True
        if kill_works:
            driver.detach()
            assert not alive
            assert driver.poll().state is NodeState.IDLE
        else:
            with pytest.raises(DriverError, match="teardown unconfirmed"):
                driver.attach("prod-b", {})
            assert driver.poll().state is NodeState.ERROR
        assert len(launches) == 1
        assert signals == [signal.SIGTERM, signal.SIGKILL]
