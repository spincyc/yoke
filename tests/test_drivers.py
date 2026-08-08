"""Driver tests: usbip via FakeRunner, SimDriver scripting, IgmpJoinDriver
via a fake socket factory. No real subprocesses (one minimal, skip-marked
/bin/echo exception for SubprocessRunner), no sockets, no network."""

from __future__ import annotations

import os
import socket
import struct
from collections.abc import Sequence

import pytest

from yoke.core.types import Body, DriverStatus, NodeState
from yoke.drivers.base import Driver, DriverError
from yoke.drivers.igmp import IgmpJoinDriver, MembershipSocket
from yoke.drivers.shellcmd import CommandResult, FakeRunner, SubprocessRunner
from yoke.drivers.sim import SimDriver
from yoke.drivers.usbip import UsbipAttachDriver, UsbipBindDriver

OK = CommandResult(0, "", "")

MEDIA: Body = {"host": "192.168.7.10", "port": 3240, "busids": ["1-1", "1-1.2"]}

PORT_OUTPUT_BOTH = """\
Imported USB devices
====================
Port 00: <Port in Use> at High Speed(480Mbps)
       unknown vendor : unknown product (046d:c31c)
       7-1 -> usbip://192.168.7.10:3240/1-1
           -> remote bus/dev 001/002
Port 01: <Port in Use> at High Speed(480Mbps)
       unknown vendor : unknown product (046d:c534)
       7-2 -> usbip://192.168.7.10:3240/1-1.2
           -> remote bus/dev 001/003
"""

PORT_OUTPUT_PARTIAL = """\
Imported USB devices
====================
Port 00: <Port in Use> at High Speed(480Mbps)
       unknown vendor : unknown product (046d:c31c)
       7-1 -> usbip://192.168.7.10:3240/1-1
           -> remote bus/dev 001/002
"""

# Older tool variant: no colon after the port number.
PORT_OUTPUT_NO_COLON = """\
Imported USB devices
====================
Port 00 <Port in Use> at High Speed(480Mbps)
       unknown vendor : unknown product (046d:c31c)
       7-1 -> usbip://192.168.7.10:3240/1-1
           -> remote bus/dev 001/002
"""

PORT_OUTPUT_EMPTY = "Imported USB devices\n====================\n"

LIST_LOCAL_OUTPUT = """\
 - busid 1-1 (0557:2419)
   ATEN International Co., Ltd : unknown product (0557:2419)

 - busid 1-1.2 (046d:c31c)
   Logitech, Inc. : Keyboard K120 (046d:c31c)
"""


# --- shellcmd ---------------------------------------------------------------


class TestFakeRunner:
    def test_records_calls_and_matches_prefix(self) -> None:
        runner = FakeRunner()
        runner.script(["usbip", "attach"], OK)
        result = runner.run(["usbip", "attach", "-r", "h", "-b", "1-1"], timeout=5.0)
        assert result == OK
        assert runner.calls == [("usbip", "attach", "-r", "h", "-b", "1-1")]
        assert runner.timeouts == [5.0]

    def test_unexpected_command_is_assertion_error(self) -> None:
        runner = FakeRunner()
        with pytest.raises(AssertionError, match="unexpected command"):
            runner.run(["rm", "-rf", "/"], timeout=1.0)

    def test_scripted_exception_and_callable(self) -> None:
        runner = FakeRunner()
        runner.script(["boom"], DriverError("command timed out after 10s: boom"))
        runner.script(["echo"], lambda argv: CommandResult(0, " ".join(argv[1:]), ""))
        with pytest.raises(DriverError, match="timed out"):
            runner.run(["boom"], timeout=10.0)
        assert runner.run(["echo", "hi"], timeout=1.0).stdout == "hi"


class TestSubprocessRunner:
    @pytest.mark.skipif(not os.access("/bin/echo", os.X_OK), reason="no /bin/echo")
    def test_runs_tiny_command(self) -> None:
        result = SubprocessRunner().run(["/bin/echo", "yoke"], timeout=5.0)
        assert result.returncode == 0
        assert result.stdout.strip() == "yoke"
        assert result.stderr == ""

    def test_missing_binary_is_driver_error(self) -> None:
        with pytest.raises(DriverError, match="command not found"):
            SubprocessRunner().run(["/nonexistent/yoke-no-such-bin"], timeout=1.0)


# --- usbip consumer ---------------------------------------------------------


def attach_driver(runner: FakeRunner) -> UsbipAttachDriver:
    return UsbipAttachDriver(runner, timeout=7.0)


class TestUsbipAttach:
    def test_attach_runs_one_command_per_busid(self) -> None:
        runner = FakeRunner()
        runner.script(["usbip", "attach"], OK)
        attach_driver(runner).attach("prod-a", MEDIA)
        assert runner.calls == [
            ("usbip", "attach", "-r", "192.168.7.10", "-b", "1-1"),
            ("usbip", "attach", "-r", "192.168.7.10", "-b", "1-1.2"),
        ]
        assert runner.timeouts == [7.0, 7.0]

    def test_per_busid_failure_raises_with_stderr(self) -> None:
        runner = FakeRunner()
        runner.script(["usbip", "attach", "-r", "192.168.7.10", "-b", "1-1"], OK)
        runner.script(
            ["usbip", "attach", "-r", "192.168.7.10", "-b", "1-1.2"],
            CommandResult(1, "", "usbip: error: attach failed: device busy"),
        )
        with pytest.raises(DriverError, match="device busy"):
            attach_driver(runner).attach("prod-a", MEDIA)

    def test_timeout_surfaces_as_driver_error(self) -> None:
        runner = FakeRunner()
        runner.script(
            ["usbip", "attach"],
            DriverError("command timed out after 7s: usbip attach"),
        )
        with pytest.raises(DriverError, match="timed out"):
            attach_driver(runner).attach("prod-a", MEDIA)

    def test_missing_binary_surfaces_as_driver_error(self) -> None:
        runner = FakeRunner()
        runner.script(["usbip"], DriverError("command not found: usbip"))
        with pytest.raises(DriverError, match="not found"):
            attach_driver(runner).attach("prod-a", MEDIA)

    @pytest.mark.parametrize(
        "media",
        [
            {},
            {"host": "", "port": 3240, "busids": ["1-1"]},
            {"host": "h", "port": "3240", "busids": ["1-1"]},
            {"host": "h", "port": 0, "busids": ["1-1"]},
            {"host": "h", "port": True, "busids": ["1-1"]},
            {"host": "h", "port": 3240, "busids": []},
            {"host": "h", "port": 3240, "busids": ["1-1", 7]},
            {"host": "h", "port": 3240, "busids": "1-1"},
        ],
    )
    def test_bad_media_shape(self, media: Body) -> None:
        with pytest.raises(DriverError, match="usbip media"):
            attach_driver(FakeRunner()).attach("prod-a", media)

    def test_detach_detaches_each_parsed_port(self) -> None:
        runner = FakeRunner()
        runner.script(["usbip", "port"], CommandResult(0, PORT_OUTPUT_BOTH, ""))
        runner.script(["usbip", "detach"], OK)
        attach_driver(runner).detach()
        assert runner.calls == [
            ("usbip", "port"),
            ("usbip", "detach", "-p", "0"),
            ("usbip", "detach", "-p", "1"),
        ]

    def test_detach_handles_no_colon_port_variant(self) -> None:
        runner = FakeRunner()
        runner.script(["usbip", "port"], CommandResult(0, PORT_OUTPUT_NO_COLON, ""))
        runner.script(["usbip", "detach"], OK)
        attach_driver(runner).detach()
        assert ("usbip", "detach", "-p", "0") in runner.calls

    def test_detach_idempotent_when_nothing_attached(self) -> None:
        runner = FakeRunner()
        runner.script(["usbip", "port"], CommandResult(0, PORT_OUTPUT_EMPTY, ""))
        driver = attach_driver(runner)
        driver.detach()
        driver.detach()
        assert runner.calls == [("usbip", "port"), ("usbip", "port")]

    def _attached(self, runner: FakeRunner) -> UsbipAttachDriver:
        runner.script(["usbip", "attach"], OK)
        driver = attach_driver(runner)
        driver.attach("prod-a", MEDIA)
        return driver

    def test_poll_active_when_all_busids_imported(self) -> None:
        runner = FakeRunner()
        driver = self._attached(runner)
        runner.script(["usbip", "port"], CommandResult(0, PORT_OUTPUT_BOTH, ""))
        assert driver.poll().state is NodeState.ACTIVE

    def test_poll_pending_when_partially_imported(self) -> None:
        runner = FakeRunner()
        driver = self._attached(runner)
        runner.script(["usbip", "port"], CommandResult(0, PORT_OUTPUT_PARTIAL, ""))
        status = driver.poll()
        assert status.state is NodeState.PENDING
        assert "1-1.2" in status.detail

    def test_poll_idle_when_detached_and_none_imported(self) -> None:
        runner = FakeRunner()
        runner.script(["usbip", "port"], CommandResult(0, PORT_OUTPUT_EMPTY, ""))
        assert attach_driver(runner).poll().state is NodeState.IDLE

    def test_poll_error_on_unparseable_output_without_raising(self) -> None:
        runner = FakeRunner()
        driver = self._attached(runner)
        runner.script(["usbip", "port"], CommandResult(0, "kernel spew\nnonsense", ""))
        status = driver.poll()
        assert status.state is NodeState.ERROR
        assert "unparseable" in status.detail

    def test_poll_error_on_command_failure_without_raising(self) -> None:
        runner = FakeRunner()
        driver = self._attached(runner)
        runner.script(["usbip", "port"], CommandResult(1, "", "open vhci_driver"))
        assert driver.poll().state is NodeState.ERROR

    def test_poll_error_when_runner_raises(self) -> None:
        runner = FakeRunner()
        driver = self._attached(runner)
        runner.script(["usbip", "port"], DriverError("command not found: usbip"))
        status = driver.poll()
        assert status.state is NodeState.ERROR
        assert "not found" in status.detail

    def test_describe_is_none_and_satisfies_protocol(self) -> None:
        driver = attach_driver(FakeRunner())
        assert driver.describe() is None
        assert isinstance(driver, Driver)


# --- usbip producer ---------------------------------------------------------


def bind_driver(runner: FakeRunner) -> UsbipBindDriver:
    return UsbipBindDriver(
        runner, host="192.168.7.10", busids=["1-1", "1-1.2"], timeout=7.0
    )


class TestUsbipBind:
    def test_describe_advertises_media(self) -> None:
        assert bind_driver(FakeRunner()).describe() == MEDIA

    def test_attach_refused_on_producer(self) -> None:
        with pytest.raises(DriverError, match="producer driver"):
            bind_driver(FakeRunner()).attach("x", {})

    def test_detach_is_noop(self) -> None:
        runner = FakeRunner()
        bind_driver(runner).detach()
        assert runner.calls == []

    def test_bind_and_unbind_run_per_busid(self) -> None:
        runner = FakeRunner()
        runner.script(["usbip", "bind"], OK)
        runner.script(["usbip", "unbind"], OK)
        driver = bind_driver(runner)
        driver.bind()
        driver.unbind()
        assert runner.calls == [
            ("usbip", "bind", "-b", "1-1"),
            ("usbip", "bind", "-b", "1-1.2"),
            ("usbip", "unbind", "-b", "1-1"),
            ("usbip", "unbind", "-b", "1-1.2"),
        ]

    def test_bind_failure_includes_stderr(self) -> None:
        runner = FakeRunner()
        runner.script(
            ["usbip", "bind"],
            CommandResult(1, "", "usbip: error: bind failed: no such device"),
        )
        with pytest.raises(DriverError, match="no such device"):
            bind_driver(runner).bind()

    def test_poll_active_when_all_busids_local(self) -> None:
        runner = FakeRunner()
        runner.script(["usbip", "list", "-l"], CommandResult(0, LIST_LOCAL_OUTPUT, ""))
        assert bind_driver(runner).poll().state is NodeState.ACTIVE

    def test_poll_error_names_missing_busids(self) -> None:
        runner = FakeRunner()
        runner.script(
            ["usbip", "list", "-l"],
            CommandResult(0, " - busid 1-1 (0557:2419)\n   x : y\n", ""),
        )
        status = bind_driver(runner).poll()
        assert status.state is NodeState.ERROR
        assert "1-1.2" in status.detail

    def test_poll_error_on_command_failure_without_raising(self) -> None:
        runner = FakeRunner()
        runner.script(["usbip", "list"], DriverError("command not found: usbip"))
        assert bind_driver(runner).poll().state is NodeState.ERROR

    def test_empty_busids_rejected(self) -> None:
        with pytest.raises(DriverError, match="at least one busid"):
            UsbipBindDriver(FakeRunner(), host="h", busids=[])


# --- sim --------------------------------------------------------------------


class TestSimDriver:
    def test_default_lifecycle(self) -> None:
        driver = SimDriver()
        assert driver.poll().state is NodeState.IDLE
        driver.attach("prod-a", {"group": "239.0.0.1", "port": 5004})
        assert driver.poll().state is NodeState.ACTIVE
        driver.detach()
        assert driver.poll().state is NodeState.IDLE
        assert driver.calls == [
            ("attach", "prod-a", {"group": "239.0.0.1", "port": 5004}),
            ("detach",),
        ]
        assert isinstance(driver, Driver)

    def test_reattach_is_idempotent_and_recorded(self) -> None:
        driver = SimDriver()
        driver.attach("prod-a", {})
        driver.attach("prod-a", {})
        assert driver.poll().state is NodeState.ACTIVE
        assert len(driver.calls) == 2

    def test_fail_attach_times(self) -> None:
        driver = SimDriver()
        driver.fail_attach_times(2)
        for _ in range(2):
            with pytest.raises(DriverError):
                driver.attach("prod-a", {})
            assert driver.poll().state is NodeState.ERROR
        driver.attach("prod-a", {})  # third attempt succeeds
        assert driver.poll().state is NodeState.ACTIVE
        assert len(driver.calls) == 3

    def test_fail_detach_once(self) -> None:
        driver = SimDriver()
        driver.attach("prod-a", {})
        driver.fail_detach_once()
        with pytest.raises(DriverError):
            driver.detach()
        driver.detach()
        assert driver.poll().state is NodeState.IDLE

    def test_set_status_forces_poll_until_cleared(self) -> None:
        driver = SimDriver()
        driver.set_status(DriverStatus(NodeState.PENDING, "media down"))
        assert driver.poll() == DriverStatus(NodeState.PENDING, "media down")
        driver.set_status(None)
        assert driver.poll().state is NodeState.IDLE

    def test_flaky_poll_sequence_then_default(self) -> None:
        driver = SimDriver()
        driver.attach("prod-a", {})
        driver.flaky_poll(
            [
                DriverStatus(NodeState.PENDING, "blip"),
                DriverStatus(NodeState.ERROR, "flap"),
            ]
        )
        assert driver.poll().state is NodeState.PENDING
        assert driver.poll().state is NodeState.ERROR
        assert driver.poll().state is NodeState.ACTIVE

    def test_producer_mode_describe(self) -> None:
        media: Body = {"group": "239.1.2.3", "port": 5004}
        assert SimDriver(media=media).describe() == media
        assert SimDriver().describe() is None


# --- igmp -------------------------------------------------------------------


class FakeSocket:
    def __init__(self, *, fail: str | None = None) -> None:
        self.fail = fail  # "bind" | "membership" | None
        self.setsockopt_calls: list[tuple[int, int, int | bytes]] = []
        self.bind_calls: list[tuple[str, int]] = []
        self.closed = False

    def setsockopt(self, level: int, optname: int, value: int | bytes, /) -> None:
        if self.fail == "membership" and optname == socket.IP_ADD_MEMBERSHIP:
            raise OSError(19, "No such device")
        self.setsockopt_calls.append((level, optname, value))

    def bind(self, address: tuple[str, int], /) -> None:
        if self.fail == "bind":
            raise OSError(98, "Address already in use")
        self.bind_calls.append(address)

    def close(self) -> None:
        self.closed = True


class FakeSocketFactory:
    def __init__(self, *, fail: str | None = None) -> None:
        self.fail = fail
        self.sockets: list[FakeSocket] = []

    def __call__(self) -> MembershipSocket:
        sock = FakeSocket(fail=self.fail)
        self.sockets.append(sock)
        return sock


GROUP_MEDIA: Body = {"group": "239.10.0.7", "port": 5004}


class TestIgmpJoinDriver:
    def test_join_binds_and_adds_membership(self) -> None:
        factory = FakeSocketFactory()
        driver = IgmpJoinDriver(factory)
        driver.attach("prod-a", GROUP_MEDIA)
        (sock,) = factory.sockets
        assert sock.bind_calls == [("", 5004)]
        mreq = struct.pack(
            "4s4s", socket.inet_aton("239.10.0.7"), socket.inet_aton("0.0.0.0")
        )
        assert (socket.SOL_SOCKET, socket.SO_REUSEADDR, 1) in sock.setsockopt_calls
        assert (
            socket.IPPROTO_IP,
            socket.IP_ADD_MEMBERSHIP,
            mreq,
        ) in sock.setsockopt_calls
        assert driver.poll() == DriverStatus(
            NodeState.ACTIVE, "member of 239.10.0.7:5004"
        )
        assert driver.describe() is None
        assert isinstance(driver, Driver)

    def test_detach_closes_socket_and_is_idempotent(self) -> None:
        factory = FakeSocketFactory()
        driver = IgmpJoinDriver(factory)
        driver.attach("prod-a", GROUP_MEDIA)
        driver.detach()
        driver.detach()
        assert factory.sockets[0].closed
        assert driver.poll().state is NodeState.IDLE

    def test_reattach_leaves_old_group_first(self) -> None:
        factory = FakeSocketFactory()
        driver = IgmpJoinDriver(factory)
        driver.attach("prod-a", GROUP_MEDIA)
        driver.attach("prod-b", {"group": "239.10.0.8", "port": 5004})
        first, second = factory.sockets
        assert first.closed
        assert not second.closed
        assert driver.poll().detail == "member of 239.10.0.8:5004"

    @pytest.mark.parametrize(
        "media",
        [
            {},
            {"group": 239, "port": 5004},
            {"group": "not-an-ip", "port": 5004},
            {"group": "239.10.0.7", "port": "5004"},
            {"group": "239.10.0.7", "port": 0},
            {"group": "239.10.0.7", "port": True},
        ],
    )
    def test_bad_media_shape(self, media: Body) -> None:
        driver = IgmpJoinDriver(FakeSocketFactory())
        with pytest.raises(DriverError, match="igmp media"):
            driver.attach("prod-a", media)
        assert driver.poll().state is NodeState.ERROR

    def test_non_multicast_group_rejected(self) -> None:
        driver = IgmpJoinDriver(FakeSocketFactory())
        with pytest.raises(DriverError, match="not multicast"):
            driver.attach("prod-a", {"group": "10.0.0.7", "port": 5004})

    def test_socket_failure_closes_socket_and_polls_error(self) -> None:
        factory = FakeSocketFactory(fail="membership")
        driver = IgmpJoinDriver(factory)
        with pytest.raises(DriverError, match=r"join 239\.10\.0\.7:5004 failed"):
            driver.attach("prod-a", GROUP_MEDIA)
        assert factory.sockets[0].closed
        assert driver.poll().state is NodeState.ERROR
        driver.detach()  # detach clears the failure
        assert driver.poll().state is NodeState.IDLE

    def test_bind_failure_polls_error(self) -> None:
        driver = IgmpJoinDriver(FakeSocketFactory(fail="bind"))
        with pytest.raises(DriverError, match="failed"):
            driver.attach("prod-a", GROUP_MEDIA)
        assert driver.poll().state is NodeState.ERROR


# --- shellcmd result --------------------------------------------------------


def test_command_result_is_frozen() -> None:
    result = CommandResult(0, "out", "err")
    assert result.ok
    with pytest.raises(AttributeError):
        result.returncode = 1  # type: ignore[misc]


def test_fake_runner_satisfies_protocol() -> None:
    from yoke.drivers.shellcmd import CommandRunner

    def check(runner: CommandRunner, argv: Sequence[str]) -> CommandResult:
        return runner.run(argv, timeout=1.0)

    runner = FakeRunner()
    runner.script(["x"], OK)
    assert check(runner, ["x"]) == OK
