"""Linux USB/IP transcripts and journal recovery, without hardware/network.

The fake models CLI metadata, not USB payload or physical gadget behavior.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from yoke.core.types import Body, NodeState
from yoke.drivers.base import Driver, DriverError
from yoke.drivers.shellcmd import CommandResult, FakeRunner
from yoke.drivers.usbip import (
    DAEMON_PROBE_INTERVAL,
    DAEMON_RETRY_INTERVAL,
    NftablesAdmission,
    UsbipAttachDriver,
    UsbipBindDriver,
    apply_admission,
    render_admission_ruleset,
)

Remote = tuple[str, int, str]
MEDIA: Body = {"host": "192.168.7.10", "port": 3240, "busids": ["1-1", "1-1.2"]}
ONE: Body = {"host": "192.168.7.10", "port": 3240, "busids": ["1-1"]}


class UsbipTranscript:
    def __init__(self) -> None:
        self.imports: dict[int, Remote] = {}
        self.bindings = {"1-1": 0, "1-1.2": 0}
        self.daemon_ready = True
        self.hidden_exports: set[str] = set()
        self.fail_attach: str | None = None
        self.timeout_after_attach: str | None = None
        self.record_failure: str | None = None
        self.crash_after_import = False
        self.unknown_ports: set[int] = set()
        self.fail_detach = False
        self.fail_bind: str | None = None
        self.output: str | None = None
        self.colon = ":"
        self.calls: list[tuple[str, ...]] = []
        self.timeouts: list[float] = []
        self.before_attach: Path | None = None

    def status(self, busid: str) -> int:
        if busid not in self.bindings:
            raise DriverError(f"usbip busid not present locally: {busid}")
        return self.bindings[busid]

    def run(self, argv: Sequence[str], *, timeout: float) -> CommandResult:
        self.calls.append(tuple(argv))
        self.timeouts.append(timeout)
        args = list(argv[1:])
        port = 3240
        if args[:1] == ["--tcp-port"]:
            port = int(args[1])
            args = args[2:]
        if args == ["port"]:
            output = "Imported USB devices\n====================\n"
            for local, remote in sorted(self.imports.items()):
                host, tcp, busid = remote
                output += (
                    f"Port {local:02d}{self.colon} <Port in Use> "
                    "at High Speed(480Mbps)\n"
                )
                if local in self.unknown_ports:
                    output += (
                        "       7-1 -> unknown host, remote port and remote busid\n"
                    )
                else:
                    output += f"       unknown vendor : unknown product\n       7-1 -> usbip://{host}:{tcp}/{busid}\n"
            return CommandResult(
                0, self.output if self.output is not None else output, ""
            )
        if args[:1] == ["attach"]:
            host, busid = args[2], args[4]
            if self.before_attach is not None:
                journal = json.loads(self.before_attach.read_text())
                assert {"host": host, "port": port, "busid": busid} in [
                    {k: r[k] for k in ("host", "port", "busid")}
                    for r in journal["imports"]
                ]
            if busid == self.fail_attach:
                return CommandResult(1, "", "device busy")
            assert (host, port, busid) not in self.imports.values(), "duplicate attach"
            local = next(i for i in range(64) if i not in self.imports)
            self.imports[local] = (host, port, busid)
            if busid == self.record_failure:
                self.unknown_ports.add(local)
                if self.crash_after_import:
                    raise SystemExit("process died before connection record")
                raise DriverError("record_connection failed after kernel attach")
            if busid == self.timeout_after_attach:
                raise DriverError("command timed out after remote import succeeded")
            return CommandResult(0, "", "")
        if args[:1] == ["detach"]:
            if self.fail_detach:
                return CommandResult(1, "", "detach failed")
            self.imports.pop(int(args[2]), None)
            return CommandResult(0, "", "")
        if args[:1] == ["bind"]:
            if args[2] == self.fail_bind:
                return CommandResult(1, "", "bind failed")
            assert self.bindings[args[2]] == 0, "duplicate bind"
            self.bindings[args[2]] = 1
            return CommandResult(0, "", "")
        if args[:1] == ["unbind"]:
            self.bindings[args[2]] = 0
            return CommandResult(0, "", "")
        if args == ["list", "-r", "127.0.0.1"]:
            if not self.daemon_ready:
                return CommandResult(1, "", "connection refused")
            output = "Exportable USB devices\n======================\n - 127.0.0.1\n"
            output += "".join(
                f" {b}: test device\n"
                for b, s in self.bindings.items()
                if s == 1 and b not in self.hidden_exports
            )
            return CommandResult(0, output, "")
        raise AssertionError(f"unexpected command: {argv}")

    def commands(self, verb: str) -> list[tuple[str, ...]]:
        return [c for c in self.calls if verb in c[1:]]


def consumer(fake: UsbipTranscript, journal: Path | None = None) -> UsbipAttachDriver:
    return UsbipAttachDriver(fake, timeout=7.0, ownership_file=journal)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def producer(
    fake: UsbipTranscript, clock: FakeClock | None = None, **options: object
) -> UsbipBindDriver:
    return UsbipBindDriver(
        fake,
        host="192.168.7.10",
        busids=["1-1", "1-1.2"],
        timeout=7.0,
        export_status=fake.status,
        clock=clock or FakeClock(),
        **options,  # type: ignore[arg-type]
    )


def test_repeated_attach_reconciles_without_duplicate_imports() -> None:
    fake = UsbipTranscript()
    driver = consumer(fake)
    driver.attach("prod-a", MEDIA)
    driver.attach("prod-a", MEDIA)
    assert len(fake.commands("attach")) == 2
    assert all(t == 7.0 for t in fake.timeouts)
    assert driver.poll().state is NodeState.ACTIVE
    assert fake.commands("attach")[0] == (
        "usbip",
        "--tcp-port",
        "3240",
        "attach",
        "-r",
        "192.168.7.10",
        "-b",
        "1-1",
    )
    assert driver.describe() is None
    assert isinstance(driver, Driver)


def test_replace_and_detach_preserve_unrelated_imports() -> None:
    fake = UsbipTranscript()
    unrelated = ("other", 3240, "2-1")
    fake.imports[4] = unrelated
    driver = consumer(fake)
    driver.attach("prod-a", MEDIA)
    driver.attach("prod-b", {"host": "other-new", "port": 4321, "busids": ["3-1"]})
    assert set(fake.imports.values()) == {unrelated, ("other-new", 4321, "3-1")}
    driver.detach()
    driver.detach()
    assert fake.imports == {4: unrelated}
    assert driver.poll().state is NodeState.IDLE


def test_never_adopt_preexisting_unowned_import() -> None:
    fake = UsbipTranscript()
    fake.imports[3] = ("192.168.7.10", 3240, "1-1")
    driver = consumer(fake)
    with pytest.raises(DriverError, match="not owned"):
        driver.attach("prod-a", ONE)
    driver.detach()
    assert len(fake.imports) == 1
    assert not fake.commands("detach")


def test_partial_attach_failure_rolls_back_new_imports() -> None:
    fake = UsbipTranscript()
    fake.fail_attach = "1-1.2"
    driver = consumer(fake)
    with pytest.raises(DriverError, match="device busy"):
        driver.attach("prod-a", MEDIA)
    assert not fake.imports
    assert driver.poll().state is NodeState.ERROR
    fake.fail_attach = None
    driver.attach("prod-a", MEDIA)
    assert driver.poll().state is NodeState.ACTIVE


def test_partial_failure_preserves_existing_owned_requested_import() -> None:
    fake = UsbipTranscript()
    driver = consumer(fake)
    driver.attach("prod-a", ONE)
    fake.fail_attach = "1-1.2"
    with pytest.raises(DriverError, match="device busy"):
        driver.attach("prod-a", MEDIA)
    assert list(fake.imports.values()) == [("192.168.7.10", 3240, "1-1")]
    assert not fake.commands("detach")


def test_timeout_after_success_is_reconciled_and_cleaned() -> None:
    fake = UsbipTranscript()
    fake.timeout_after_attach = "1-1"
    driver = consumer(fake)
    with pytest.raises(DriverError, match="timed out"):
        driver.attach("prod-a", ONE)
    assert not fake.imports
    assert len(fake.commands("detach")) == 1


def test_failed_cleanup_is_recorded_for_restart_retry(tmp_path: Path) -> None:
    journal = tmp_path / "owned.json"
    fake = UsbipTranscript()
    fake.fail_attach = "1-1.2"
    fake.fail_detach = True
    with pytest.raises(DriverError, match="cleanup failed"):
        consumer(fake, journal).attach("prod-a", MEDIA)
    assert json.loads(journal.read_text())["imports"]
    fake.fail_detach = False
    consumer(fake, journal).detach()
    assert not fake.imports
    assert json.loads(journal.read_text())["imports"] == []


def test_journal_precedes_import_and_restart_reconciles(tmp_path: Path) -> None:
    journal = tmp_path / "owned.json"
    fake = UsbipTranscript()
    fake.before_attach = journal
    consumer(fake, journal).attach("prod-a", MEDIA)
    fake.imports[9] = ("foreign", 3240, "9-1")
    restarted = consumer(fake, journal)
    assert restarted.poll().state is NodeState.PENDING
    restarted.attach("prod-a", MEDIA)
    assert len(fake.commands("attach")) == 2
    restarted.detach()
    assert fake.imports == {9: ("foreign", 3240, "9-1")}
    assert journal.stat().st_mode & 0o777 == 0o600


def test_restart_can_detach_without_new_attach(tmp_path: Path) -> None:
    fake = UsbipTranscript()
    journal = tmp_path / "owned.json"
    consumer(fake, journal).attach("prod-a", ONE)
    consumer(fake, journal).detach()
    assert not fake.imports


def test_crash_intent_can_recover_import_with_reused_foreign_port(
    tmp_path: Path,
) -> None:
    fake = UsbipTranscript()
    journal = tmp_path / "owned.json"
    consumer(fake, journal).attach("prod-a", ONE)
    # Kernel import disappeared, another peripheral reused the local port.
    fake.imports = {0: ("foreign", 3240, "2-1")}
    consumer(fake, journal).detach()
    assert fake.imports == {0: ("foreign", 3240, "2-1")}


def test_unusable_journal_never_mutates_ports(tmp_path: Path) -> None:
    journal = tmp_path / "owned.json"
    journal.write_text("not json")
    journal.chmod(0o600)
    fake = UsbipTranscript()
    fake.imports[0] = ("foreign", 3240, "2-1")
    driver = consumer(fake, journal)
    with pytest.raises(DriverError, match="unusable"):
        driver.detach()
    assert driver.poll().state is NodeState.ERROR
    assert not fake.calls


def test_journal_save_failure_prevents_import(tmp_path: Path) -> None:
    parent = tmp_path / "not-a-directory"
    parent.write_text("x")
    fake = UsbipTranscript()
    driver = consumer(fake, parent / "owned")
    with pytest.raises(DriverError):
        driver.attach("prod-a", ONE)
    assert not fake.commands("attach")


def test_constructor_does_not_probe_journal(tmp_path: Path) -> None:
    journal = tmp_path / "owned"
    journal.write_text("broken")
    assert consumer(UsbipTranscript(), journal).describe() is None


@pytest.mark.parametrize(
    "host", ["--help", "-r", "x\ny", "a/b", "x;id", "a..b", "a.-bad", "[bad]", ""]
)
def test_unsafe_host_rejected_before_commands(host: str) -> None:
    fake = UsbipTranscript()
    with pytest.raises(DriverError, match="bad host"):
        consumer(fake).attach("prod-a", {**ONE, "host": host})
    assert not fake.calls


@pytest.mark.parametrize(
    "busids",
    [[], ["--help"], ["1-1", "1-1"], ["../1-1"], ["1-1\n"], ["usb1"], [True], "1-1"],
)
def test_unsafe_busids_rejected_before_commands(busids: object) -> None:
    fake = UsbipTranscript()
    with pytest.raises(DriverError, match="bad busids"):
        consumer(fake).attach("prod-a", {**ONE, "busids": busids})
    assert not fake.calls


@pytest.mark.parametrize("port", [0, 65536, True, "3240", -1])
def test_invalid_port_rejected(port: object) -> None:
    with pytest.raises(DriverError, match="bad port"):
        consumer(UsbipTranscript()).attach("prod-a", {**ONE, "port": port})


@pytest.mark.parametrize("host", ["::1", "[::1]", "HOST.EXAMPLE."])
def test_ipv6_and_hostname_port_identity(host: str) -> None:
    fake = UsbipTranscript()
    driver = consumer(fake)
    driver.attach("prod-a", {**ONE, "host": host, "port": 4321})
    assert driver.poll().state is NodeState.ACTIVE
    assert fake.commands("attach")[0][1:3] == ("--tcp-port", "4321")
    driver.detach()
    assert not fake.imports


def test_same_host_busid_wrong_tcp_port_is_not_owned() -> None:
    fake = UsbipTranscript()
    fake.imports[9] = ("192.168.7.10", 4321, "1-1")
    driver = consumer(fake)
    driver.attach("prod-a", ONE)
    driver.detach()
    assert fake.imports == {9: ("192.168.7.10", 4321, "1-1")}


@pytest.mark.parametrize(
    "output",
    [
        "garbage",
        "Imported USB devices\n====================\ngarbage",
    ],
)
def test_malformed_port_output_fails_closed(output: str) -> None:
    fake = UsbipTranscript()
    fake.output = output
    with pytest.raises(DriverError, match="unparseable"):
        consumer(fake).attach("prod-a", ONE)
    assert not fake.commands("attach")


def test_old_no_colon_port_output_and_lost_import_retry() -> None:
    fake = UsbipTranscript()
    fake.colon = ""
    driver = consumer(fake)
    driver.attach("prod-a", MEDIA)
    fake.imports.pop(0)
    assert driver.poll().state is NodeState.ERROR
    driver.attach("prod-a", MEDIA)
    assert driver.poll().state is NodeState.ACTIVE


def test_producer_bind_idempotence_and_actual_readiness() -> None:
    fake = UsbipTranscript()
    clock = FakeClock()
    driver = producer(fake, clock)
    assert driver.describe() == MEDIA
    assert driver.poll().state is NodeState.ERROR  # physically present is insufficient
    driver.bind()
    driver.bind()
    assert len(fake.commands("bind")) == 2
    assert driver.poll().state is NodeState.ACTIVE
    fake.daemon_ready = False
    # Unchanged sysfs exports: the daemon list is re-probed on its interval.
    assert driver.poll().state is NodeState.ACTIVE
    clock.now += DAEMON_PROBE_INTERVAL
    assert driver.poll().state is NodeState.ERROR
    fake.daemon_ready = True
    fake.hidden_exports.add("1-1")
    clock.now += DAEMON_RETRY_INTERVAL
    assert driver.poll().state is NodeState.ERROR
    fake.bindings["1-1"] = 2  # current usbipd deliberately omits in-use devices
    clock.now += DAEMON_RETRY_INTERVAL
    assert driver.poll().state is NodeState.ACTIVE
    driver.unbind()
    driver.unbind()
    assert not any(fake.bindings.values())
    assert isinstance(driver, Driver)
    with pytest.raises(DriverError, match="consumer-side only"):
        driver.attach("x", {})
    driver.detach()


def test_bind_rollback_preserves_prebound_export() -> None:
    fake = UsbipTranscript()
    fake.bindings["1-1"] = 2
    fake.fail_bind = "1-1.2"
    with pytest.raises(DriverError, match="bind failed"):
        producer(fake).bind()
    assert fake.bindings["1-1"] == 2
    assert not fake.commands("unbind")


def test_bind_rollback_removes_earlier_new_binding() -> None:
    fake = UsbipTranscript()
    fake.fail_bind = "1-1.2"
    with pytest.raises(DriverError, match="bind failed"):
        producer(fake).bind()
    assert not any(fake.bindings.values())
    assert fake.commands("unbind") == [("usbip", "unbind", "-b", "1-1")]


def test_sysfs_export_probe(tmp_path: Path) -> None:
    fake = UsbipTranscript()
    driver = UsbipBindDriver(
        fake, host="localhost", busids=["1-1"], sysfs_root=tmp_path
    )
    assert driver.poll().state is NodeState.ERROR
    device = tmp_path / "1-1"
    device.mkdir()
    (device / "driver").symlink_to(tmp_path / "usb-storage")
    assert driver.poll().state is NodeState.ERROR
    (device / "driver").unlink()
    (device / "driver").symlink_to(tmp_path / "usbip-host")
    (device / "usbip_status").write_text("1\n")
    fake.bindings["1-1"] = 1
    assert driver.poll().state is NodeState.ACTIVE
    (device / "usbip_status").write_text("3\n")
    assert driver.poll().state is NodeState.ERROR
    (device / "usbip_status").write_text("broken")
    assert driver.poll().state is NodeState.ERROR


def test_producer_honors_custom_daemon_port() -> None:
    fake = UsbipTranscript()
    fake.bindings = {"1-1": 1}
    driver = UsbipBindDriver(
        fake, host="localhost", port=4321, busids=["1-1"], export_status=fake.status
    )
    assert driver.poll().state is NodeState.ACTIVE
    assert fake.calls == [("usbip", "--tcp-port", "4321", "list", "-r", "127.0.0.1")]


def test_missing_tool_and_command_failure_are_status_errors() -> None:
    fake = FakeRunner()
    fake.script(["usbip"], DriverError("command not found: usbip"))
    assert UsbipAttachDriver(fake).poll().state is NodeState.ERROR
    assert (
        UsbipBindDriver(
            fake, host="localhost", busids=["1-1"], export_status=lambda _: 1
        )
        .poll()
        .state
        is NodeState.ERROR
    )


def test_error_state_import_is_not_active() -> None:
    fake = UsbipTranscript()
    driver = consumer(fake)
    driver.attach("prod-a", ONE)
    fake.output = (
        "Port 00: <Port Error> at High Speed(480Mbps)\n"
        "  7-1 -> usbip://192.168.7.10:3240/1-1\n"
    )
    assert driver.poll().state is NodeState.ERROR


@pytest.mark.parametrize(
    "data",
    [
        '{"schema":1,"schema":1,"imports":[]}',
        '{"schema":true,"imports":[]}',
        '{"schema":1.0,"imports":[]}',
        '{"schema":2,"imports":[]}',
    ],
)
def test_invalid_ownership_schema_rejected(tmp_path: Path, data: str) -> None:
    journal = tmp_path / "owned"
    journal.write_text(data)
    journal.chmod(0o600)
    fake = UsbipTranscript()
    with pytest.raises(DriverError, match="unusable"):
        consumer(fake, journal).detach()
    assert not fake.calls


def test_ownership_symlink_is_not_followed(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.write_text('{"schema":1,"imports":[]}')
    real.chmod(0o600)
    journal = tmp_path / "owned"
    journal.symlink_to(real)
    fake = UsbipTranscript()
    with pytest.raises(DriverError, match="ownership read"):
        consumer(fake, journal).detach()
    assert not fake.calls


@pytest.mark.parametrize("timeout", [True, 0.0, -1.0, float("nan"), float("inf")])
def test_bad_timeout_rejected(timeout: float) -> None:
    with pytest.raises(DriverError, match="timeout"):
        UsbipAttachDriver(FakeRunner(), timeout=timeout)


def test_malformed_large_number_in_port_output_is_contained() -> None:
    fake = UsbipTranscript()
    fake.output = "Port " + "9" * 5000 + ": <Port in Use>\n"
    assert consumer(fake).poll().state is NodeState.ERROR


def test_invalid_ipv6_scope_rejected() -> None:
    fake = UsbipTranscript()
    with pytest.raises(DriverError, match="bad host"):
        consumer(fake).attach("prod-a", {**ONE, "host": "fe80::1%bad\nscope"})
    assert not fake.calls


def test_failed_explicit_detach_stays_error_until_cleanup_succeeds() -> None:
    fake = UsbipTranscript()
    driver = consumer(fake)
    driver.attach("prod-a", ONE)
    fake.fail_detach = True
    with pytest.raises(DriverError, match="detach failed"):
        driver.detach()
    assert driver.poll().state is NodeState.ERROR
    fake.fail_detach = False
    driver.detach()
    assert driver.poll().state is NodeState.IDLE
    assert not fake.imports


def test_absent_physical_device_is_already_unbound(tmp_path: Path) -> None:
    fake = UsbipTranscript()
    driver = UsbipBindDriver(
        fake, host="localhost", busids=["1-1"], sysfs_root=tmp_path
    )
    driver.unbind()
    assert not fake.calls
    assert driver.poll().state is NodeState.ERROR


def test_deep_invalid_journal_is_contained(tmp_path: Path) -> None:
    journal = tmp_path / "owned"
    journal.write_text("[" * 1500 + "0" + "]" * 1500)
    journal.chmod(0o600)
    assert consumer(UsbipTranscript(), journal).poll().state is NodeState.ERROR


def test_started_producer_reconciles_unplug_replug() -> None:
    fake = UsbipTranscript()
    driver = producer(fake)
    driver.bind()
    fake.bindings["1-1"] = 0
    assert driver.poll().state is NodeState.ACTIVE
    assert len(fake.commands("bind")) == 3
    driver.unbind()
    assert driver.poll().state is NodeState.ERROR
    assert len(fake.commands("bind")) == 3  # explicit shutdown never rebinds


def test_started_producer_retries_initial_bind_failure() -> None:
    fake = UsbipTranscript()
    fake.fail_bind = "1-1.2"
    driver = producer(fake)
    with pytest.raises(DriverError):
        driver.bind()
    fake.fail_bind = None
    assert driver.poll().state is NodeState.ACTIVE


def test_prebound_active_export_is_not_unbound_at_shutdown() -> None:
    fake = UsbipTranscript()
    fake.bindings["1-1"] = 2
    driver = producer(fake)
    driver.bind()
    assert fake.commands("bind") == [("usbip", "bind", "-b", "1-1.2")]
    driver.unbind()
    assert fake.bindings == {"1-1": 2, "1-1.2": 0}
    assert fake.commands("unbind") == [("usbip", "unbind", "-b", "1-1.2")]


def test_restarted_producer_does_not_assume_prebound_ownership() -> None:
    fake = UsbipTranscript()
    producer(fake).bind()
    restarted = producer(fake)
    restarted.bind()
    restarted.unbind()
    assert fake.bindings == {"1-1": 1, "1-1.2": 1}
    assert not fake.commands("unbind")


def test_unrelated_unknown_port_does_not_block_known_routes() -> None:
    fake = UsbipTranscript()
    foreign = ("other", 3240, "9-1")
    fake.imports[9] = foreign
    fake.unknown_ports.add(9)
    driver = consumer(fake)
    driver.attach("prod-a", ONE)
    status = driver.poll()
    assert status.state is NodeState.ACTIVE
    assert "RECOVERY_REQUIRED for unrelated USB/IP ports [9]" in status.detail
    driver.detach()
    assert fake.imports == {9: foreign}
    assert driver.poll().state is NodeState.IDLE
    assert all(c[-1] != "9" for c in fake.commands("detach"))


def test_record_failure_preserves_recovery_barrier_without_unsafe_detach(
    tmp_path: Path,
) -> None:
    fake = UsbipTranscript()
    fake.record_failure = "1-1"
    journal = tmp_path / "owned.json"
    with pytest.raises(DriverError, match="RECOVERY_REQUIRED"):
        consumer(fake, journal).attach("prod-a", ONE)
    assert len(fake.imports) == 1  # kernel attached before the CLI record failed
    assert not fake.commands("detach")
    saved = json.loads(journal.read_text())
    assert saved["schema"] == 3
    assert saved["recovery"][0]["local_ports"] == [0]
    restarted = consumer(fake, journal)
    assert restarted.poll().state is NodeState.ERROR
    with pytest.raises(DriverError, match="RECOVERY_REQUIRED"):
        restarted.detach()
    with pytest.raises(DriverError, match="RECOVERY_REQUIRED"):
        restarted.attach("other", {"host": "other", "port": 3240, "busids": ["2-1"]})
    assert len(fake.commands("attach")) == 1  # no overlapping replacement
    # Another independent driver's known route can operate without touching it.
    other = consumer(fake)
    other.attach("other", {"host": "other", "port": 3240, "busids": ["2-1"]})
    other.detach()
    assert list(fake.imports) == [0]
    # An operator identifies and detaches port 0 outside the driver.
    fake.imports.pop(0)
    restarted.detach()
    assert restarted.poll().state is NodeState.IDLE
    assert json.loads(journal.read_text())["recovery"] == []


def test_crash_before_recovery_observation_is_detected_from_durable_intent(
    tmp_path: Path,
) -> None:
    fake = UsbipTranscript()
    fake.record_failure = "1-1"
    fake.crash_after_import = True
    journal = tmp_path / "owned.json"
    with pytest.raises(SystemExit, match="before connection record"):
        consumer(fake, journal).attach("prod-a", ONE)
    assert json.loads(journal.read_text())["recovery"] == []
    restarted = consumer(fake, journal)
    assert restarted.poll().state is NodeState.ERROR
    assert json.loads(journal.read_text())["recovery"][0]["local_ports"] == [0]
    assert not fake.commands("detach")


def test_repaired_connection_record_allows_owned_cleanup(tmp_path: Path) -> None:
    fake = UsbipTranscript()
    fake.record_failure = "1-1"
    journal = tmp_path / "owned.json"
    with pytest.raises(DriverError):
        consumer(fake, journal).attach("prod-a", ONE)
    fake.unknown_ports.clear()  # operator restores correct connection metadata
    consumer(fake, journal).detach()
    assert not fake.imports
    assert fake.commands("detach") == [("usbip", "detach", "-p", "0")]


def test_partial_record_failure_still_cleans_known_new_imports(tmp_path: Path) -> None:
    fake = UsbipTranscript()
    fake.record_failure = "1-1.2"
    journal = tmp_path / "owned.json"
    with pytest.raises(DriverError, match="RECOVERY_REQUIRED"):
        consumer(fake, journal).attach("prod-a", MEDIA)
    assert list(fake.imports.values()) == [("192.168.7.10", 3240, "1-1.2")]
    assert fake.commands("detach") == [("usbip", "detach", "-p", "0")]


def test_preexisting_unknown_port_does_not_become_owned_ambiguity() -> None:
    fake = UsbipTranscript()
    fake.imports[9] = ("foreign", 3240, "9-1")
    fake.unknown_ports.add(9)
    fake.fail_attach = "1-1"
    driver = consumer(fake)
    with pytest.raises(DriverError, match="device busy") as error:
        driver.attach("prod-a", ONE)
    assert "RECOVERY_REQUIRED" not in str(error.value)
    driver.detach()
    assert not fake.commands("detach")


def test_connection_record_capacity_boundary_before_effects(tmp_path: Path) -> None:
    fake = UsbipTranscript()
    journal = tmp_path / "owned.json"
    host = "a" * 63 + "." + "b" * 26
    assert len(f"{host} 3240 1-1\n") == 100
    with pytest.raises(DriverError, match="99-byte connection record"):
        consumer(fake, journal).attach("prod-a", {**ONE, "host": host})
    assert not fake.calls
    assert not journal.exists()
    host = "a" * 63 + "." + "b" * 25
    assert len(f"{host} 3240 1-1\n") == 99
    consumer(fake, journal).attach("prod-a", {**ONE, "host": host})
    assert len(fake.commands("attach")) == 1


@pytest.mark.parametrize("timeout", [10**1000, "1"])
def test_timeout_type_errors_are_driver_errors(timeout: float) -> None:
    with pytest.raises(DriverError, match="timeout"):
        UsbipAttachDriver(FakeRunner(), timeout=timeout)


def test_recovery_port_reused_by_foreign_device_is_not_detached(tmp_path: Path) -> None:
    fake = UsbipTranscript()
    fake.record_failure = "1-1"
    journal = tmp_path / "owned.json"
    with pytest.raises(DriverError, match="RECOVERY_REQUIRED"):
        consumer(fake, journal).attach("prod-a", ONE)
    fake.unknown_ports.clear()
    fake.imports[0] = ("foreign", 3240, "9-1")
    restarted = consumer(fake, journal)
    restarted.detach()
    assert fake.imports == {0: ("foreign", 3240, "9-1")}
    assert not fake.commands("detach")
    assert json.loads(journal.read_text())["imports"] == []


# -- ADR 0024: recovery attribution baseline ---------------------------------


def test_vanished_import_beside_preexisting_unknown_port_can_reattach(
    tmp_path: Path,
) -> None:
    """Field wedge: an owned import vanishes (unplug, TCP reset) while an
    unrelated record-less port exists. The old port is not attributed."""
    fake = UsbipTranscript()
    fake.imports[9] = ("other", 3240, "9-1")
    fake.unknown_ports.add(9)
    journal = tmp_path / "owned.json"
    driver = consumer(fake, journal)
    driver.attach("prod-a", ONE)
    assert driver.poll().state is NodeState.ACTIVE
    saved = json.loads(journal.read_text())
    assert saved["imports"][0]["unknown_before"] == [9]
    owned_port = next(p for p, r in fake.imports.items() if r[2] == "1-1")
    fake.imports.pop(owned_port)
    status = driver.poll()
    assert status.state is NodeState.ERROR
    assert "RECOVERY_REQUIRED" not in status.detail  # retryable, not a wedge
    # The core's retry re-attaches, also after a restart from the journal.
    restarted = consumer(fake, journal)
    restarted.attach("prod-a", ONE)
    assert restarted.poll().state is NodeState.ACTIVE
    assert fake.imports[9] == ("other", 3240, "9-1")
    assert all(c[-1] != "9" for c in fake.commands("detach"))


def test_recordless_port_appearing_after_last_sighting_still_blocks(
    tmp_path: Path,
) -> None:
    # A record-less port newer than the owned import's last sighting may be
    # that import (e.g. its connection record was removed): stay conservative.
    fake = UsbipTranscript()
    journal = tmp_path / "owned.json"
    driver = consumer(fake, journal)
    driver.attach("prod-a", ONE)
    assert driver.poll().state is NodeState.ACTIVE
    owned_port = next(iter(fake.imports))
    fake.unknown_ports.add(owned_port)  # record lost, import still present
    status = driver.poll()
    assert status.state is NodeState.ERROR
    assert "RECOVERY_REQUIRED" in status.detail
    with pytest.raises(DriverError, match="RECOVERY_REQUIRED"):
        driver.attach("prod-a", ONE)
    assert not fake.commands("detach")


def test_legacy_journal_without_baseline_keeps_conservative_attribution(
    tmp_path: Path,
) -> None:
    fake = UsbipTranscript()
    fake.imports[9] = ("other", 3240, "9-1")
    fake.unknown_ports.add(9)
    journal = tmp_path / "owned.json"
    journal.write_text(
        json.dumps(
            {
                "schema": 2,
                "imports": [{"host": "192.168.7.10", "port": 3240, "busid": "1-1"}],
                "recovery": [],
            }
        )
    )
    journal.chmod(0o600)
    driver = consumer(fake, journal)
    assert "RECOVERY_REQUIRED" in driver.poll().detail
    assert json.loads(journal.read_text())["schema"] == 3


def test_baseline_is_persisted_only_when_it_changes(tmp_path: Path) -> None:
    fake = UsbipTranscript()
    journal = tmp_path / "owned.json"
    driver = consumer(fake, journal)
    driver.attach("prod-a", ONE)
    driver.poll()
    first = journal.stat().st_ino
    for _ in range(5):
        driver.poll()
    assert journal.stat().st_ino == first  # no rewrite per poll
    fake.imports[9] = ("other", 3240, "9-1")
    fake.unknown_ports.add(9)
    driver.poll()
    assert journal.stat().st_ino != first
    assert json.loads(journal.read_text())["imports"][0]["unknown_before"] == [9]


@pytest.mark.parametrize(
    "record",
    [
        {"host": "h", "port": 3240, "busid": "1-1", "unknown_before": "x"},
        {"host": "h", "port": 3240, "busid": "1-1", "unknown_before": [-1]},
        {"host": "h", "port": 3240, "busid": "1-1", "extra": []},
    ],
)
def test_invalid_baseline_records_are_rejected(
    tmp_path: Path, record: dict[str, object]
) -> None:
    journal = tmp_path / "owned.json"
    journal.write_text(json.dumps({"schema": 3, "imports": [record], "recovery": []}))
    journal.chmod(0o600)
    fake = UsbipTranscript()
    with pytest.raises(DriverError, match="unusable"):
        consumer(fake, journal).detach()


# -- ADR 0023: wire detail keeps the cause ----------------------------------


def test_command_failure_detail_leads_with_cause() -> None:
    fake = UsbipTranscript()
    fake.fail_attach = "1-1"
    with pytest.raises(DriverError) as error:
        consumer(fake).attach("prod-a", ONE)
    detail = str(error.value)
    assert detail.startswith("usbip attach failed rc=1: device busy")
    assert detail.encode()[:128].decode(errors="ignore").count("device busy") == 1
    assert "[usbip --tcp-port 3240 attach -r 192.168.7.10 -b 1-1]" in detail


# -- ADR 0024: readiness probe schedule (finding 8) ---------------------------


def test_producer_daemon_probe_schedule() -> None:
    fake = UsbipTranscript()
    fake.bindings = {"1-1": 1, "1-1.2": 1}
    clock = FakeClock()
    driver = producer(fake, clock)

    def probes() -> int:
        return len(fake.commands("list"))

    for second in range(60):
        clock.now = float(second)
        assert driver.poll().state is NodeState.ACTIVE
    # One probe at start and one per interval, instead of one per poll.
    assert probes() == 1 + int(59 // DAEMON_PROBE_INTERVAL)
    before = probes()
    fake.bindings["1-1"] = 2  # a consumer imported it: sysfs export state changed
    driver.poll()
    assert probes() == before + 1
    fake.daemon_ready = False
    clock.now += DAEMON_PROBE_INTERVAL
    assert driver.poll().state is NodeState.ERROR
    failed_at = probes()
    clock.now += DAEMON_RETRY_INTERVAL / 2
    driver.poll()
    assert probes() == failed_at  # no hammering a dead daemon every poll
    fake.daemon_ready = True
    clock.now += DAEMON_RETRY_INTERVAL
    assert driver.poll().state is NodeState.ACTIVE


# -- ADR 0024: external bindings and data-plane admission ----------------------


def test_external_bindings_never_run_usbip_bind_or_unbind() -> None:
    fake = UsbipTranscript()
    driver = producer(fake, bindings="external")
    driver.bind()
    status = driver.poll()
    assert status.state is NodeState.ERROR
    assert "yoke-usbip-bind@BUSID.service" in status.detail
    fake.bindings = {"1-1": 1, "1-1.2": 1}  # udev-provisioned as root
    assert driver.poll().state is NodeState.ACTIVE
    driver.unbind()
    assert not fake.commands("bind") and not fake.commands("unbind")
    assert fake.bindings == {"1-1": 1, "1-1.2": 1}


def test_unknown_bindings_mode_is_rejected() -> None:
    with pytest.raises(DriverError, match="agent or external"):
        producer(UsbipTranscript(), bindings="sometimes")


def admission(tmp_path: Path) -> NftablesAdmission:
    (tmp_path / "run-yoke").mkdir(exist_ok=True)
    (tmp_path / "run-admission").mkdir(exist_ok=True)
    return NftablesAdmission(
        port=3240,
        request_path=tmp_path / "run-yoke" / "usbip-admission.json",
        status_path=tmp_path / "run-admission" / "status.json",
    )


def helper(tmp_path: Path, runner: FakeRunner | None = None) -> int:
    nft = runner or FakeRunner([(("nft",), CommandResult(0, "", ""))])
    return apply_admission(
        request=tmp_path / "run-yoke" / "usbip-admission.json",
        status=tmp_path / "run-admission" / "status.json",
        port=3240,
        runner=nft,
    )


def test_admission_request_status_and_readiness(tmp_path: Path) -> None:
    fake = UsbipTranscript()
    fake.bindings = {"1-1": 1, "1-1.2": 1}
    backend = admission(tmp_path)
    driver = producer(fake, admission=backend)
    assert driver.poll().state is NodeState.PENDING  # nothing requested yet
    driver.admit((("desk-usb", "10.20.30.3"),))
    request = json.loads((tmp_path / "run-yoke" / "usbip-admission.json").read_text())
    assert request == {
        "schema": 1,
        "generation": 1,
        "port": 3240,
        "allow": ["10.20.30.3"],
        "peers": {"desk-usb": "10.20.30.3"},
    }
    status = driver.poll()
    assert status.state is NodeState.PENDING
    assert "not reported" in status.detail
    assert helper(tmp_path) == 0
    status = driver.poll()
    assert status.state is NodeState.ACTIVE
    assert "admission 1 consumer address(es)" in status.detail
    # A newer request is pending until the helper applies that exact list.
    driver.admit((("desk-usb", "10.20.30.4"),))
    assert driver.poll().state is NodeState.PENDING
    assert helper(tmp_path) == 0
    assert driver.poll().state is NodeState.ACTIVE
    # Orderly stop closes the port to new sessions.
    driver.unbind()
    assert (
        json.loads((tmp_path / "run-yoke" / "usbip-admission.json").read_text())[
            "allow"
        ]
        == []
    )


def test_admission_never_widens_for_unusable_addresses(tmp_path: Path) -> None:
    backend = admission(tmp_path)
    backend.apply(
        (
            ("a", "127.0.0.1"),
            ("b", "fe80::1"),
            ("c", "239.1.1.1"),
            ("d", "not-an-ip"),
            ("e", "10.0.0.7"),
        )
    )
    request = json.loads((tmp_path / "run-yoke" / "usbip-admission.json").read_text())
    assert request["allow"] == ["10.0.0.7"]
    assert request["peers"] == {"e": "10.0.0.7"}


def test_admission_helper_failure_is_producer_error(tmp_path: Path) -> None:
    fake = UsbipTranscript()
    fake.bindings = {"1-1": 1, "1-1.2": 1}
    backend = admission(tmp_path)
    driver = producer(fake, admission=backend)
    driver.admit((("desk-usb", "10.20.30.3"),))
    failing = FakeRunner([(("nft",), CommandResult(1, "", "Operation not permitted"))])
    assert helper(tmp_path, failing) == 1
    status = driver.poll()
    assert status.state is NodeState.ERROR
    assert "Operation not permitted" in status.detail


def test_admission_requires_ipv4_producer_host() -> None:
    with pytest.raises(DriverError, match="IPv4"):
        UsbipBindDriver(
            UsbipTranscript(),
            host="usb-box.local",
            busids=["1-1"],
            admission=NftablesAdmission(port=3240),
        )


def test_rendered_admission_ruleset_is_fixed_and_minimal() -> None:
    assert render_admission_ruleset(3240, ["10.0.0.9", "10.0.0.3", "10.0.0.9"]) == (
        "add table inet yoke_usbip\n"
        "delete table inet yoke_usbip\n"
        "table inet yoke_usbip {\n"
        "\tset consumers4 {\n"
        "\t\ttype ipv4_addr\n"
        "\t\telements = { 10.0.0.3, 10.0.0.9 }\n"
        "\t}\n"
        "\tchain input {\n"
        "\t\ttype filter hook input priority filter; policy accept;\n"
        "\t\ttcp dport 3240 ct state established,related accept\n"
        '\t\ttcp dport 3240 iifname "lo" accept\n'
        "\t\ttcp dport 3240 ip saddr @consumers4 accept\n"
        "\t\ttcp dport 3240 drop\n"
        "\t}\n"
        "}\n"
    )
    assert "elements" not in render_admission_ruleset(3240, [])
    with pytest.raises(ValueError):
        render_admission_ruleset(3240, ["10.0.0.1; flush ruleset"])


@pytest.mark.parametrize(
    "request_data",
    [
        {"schema": 1, "generation": 1, "port": 22, "allow": [], "peers": {}},
        {"schema": 2, "generation": 1, "port": 3240, "allow": [], "peers": {}},
        {"schema": 1, "generation": -1, "port": 3240, "allow": [], "peers": {}},
        {"schema": 1, "generation": 1, "port": 3240, "allow": ["::1"], "peers": {}},
        {
            "schema": 1,
            "generation": 1,
            "port": 3240,
            "allow": ["10.0.0.1", "10.0.0.1"],
            "peers": {},
        },
        {
            "schema": 1,
            "generation": 1,
            "port": 3240,
            "allow": [f"10.0.0.{i}" for i in range(1, 18)],
            "peers": {},
        },
        {
            "schema": 1,
            "generation": 1,
            "port": 3240,
            "allow": [],
            "peers": {"x": "10.0.0.1"},
        },
        {"schema": 1, "generation": 1, "port": 3240, "allow": []},
    ],
)
def test_helper_fails_closed_on_invalid_request(
    tmp_path: Path, request_data: dict[str, object]
) -> None:
    admission(tmp_path)
    request = tmp_path / "run-yoke" / "usbip-admission.json"
    request.write_text(json.dumps(request_data))
    runner = FakeRunner([(("nft",), CommandResult(0, "", ""))])
    assert helper(tmp_path, runner) == 1
    status = json.loads((tmp_path / "run-admission" / "status.json").read_text())
    assert status["applied"] is False
    assert status["allow"] == []
    ruleset = (tmp_path / "run-admission" / "status.json.nft").read_text()
    assert "elements" not in ruleset  # the closed table was still loaded
    assert runner.calls[0][:2] == ("nft", "-f")


def test_helper_without_request_installs_closed_table(tmp_path: Path) -> None:
    admission(tmp_path)
    assert helper(tmp_path) == 0
    status = json.loads((tmp_path / "run-admission" / "status.json").read_text())
    assert status == {
        "schema": 1,
        "generation": 0,
        "port": 3240,
        "allow": [],
        "applied": True,
    }
    assert (tmp_path / "run-admission" / "status.json").stat().st_mode & 0o777 == 0o644


def test_helper_refuses_symlinked_request(tmp_path: Path) -> None:
    admission(tmp_path)
    target = tmp_path / "elsewhere.json"
    target.write_text("{}")
    (tmp_path / "run-yoke" / "usbip-admission.json").symlink_to(target)
    assert helper(tmp_path) == 1


def test_unapplied_admission_request_is_rewritten_to_retrigger_helper(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    (tmp_path / "run-yoke").mkdir()
    (tmp_path / "run-admission").mkdir()
    request = tmp_path / "run-yoke" / "usbip-admission.json"
    backend = NftablesAdmission(
        port=3240,
        request_path=request,
        status_path=tmp_path / "run-admission" / "status.json",
        clock=clock,
    )
    backend.apply((("desk-usb", "10.20.30.3"),))
    assert helper(tmp_path) == 0
    backend.apply((("desk-usb", "10.20.30.4"),))  # helper trigger "lost"
    assert backend.status()[0] is NodeState.PENDING
    assert json.loads(request.read_text())["generation"] == 2
    clock.now += 5.0
    assert backend.status()[0] is NodeState.PENDING
    rewritten = json.loads(request.read_text())
    assert rewritten["generation"] == 3 and rewritten["allow"] == ["10.20.30.4"]
    assert helper(tmp_path) == 0
    assert backend.status() == (None, "; admission 1 consumer address(es)")
