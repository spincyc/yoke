"""Linux updater transactions, with no root, real device, or network.

The real systemd/Radxa readiness and power-cut behavior remain bench-unverified.
Scripted services model only the documented notify readiness contract.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import zipfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from yoke.update import (
    MAX_OUTPUT,
    RunResult,
    State,
    SubprocessRunner,
    SystemdService,
    UpdateError,
    Updater,
    build_bundle,
    main,
    verify_bundle,
)


def make_wheel(
    path: Path, *, dependency: bool = False, extra: str | None = None
) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    wheel = path / "yoke-0.1.0-py3-none-any.whl"
    metadata = (
        "Metadata-Version: 2.1\nName: yoke\nVersion: 0.1.0\nRequires-Python: >=3.12\n"
        "Provides-Extra: dev\nRequires-Dist: pytest==9.2.0; extra == 'dev'\n"
    )
    if dependency:
        metadata += "Requires-Dist: unwanted-network-dependency\n"
    contents = {
        "yoke/__init__.py": "",
        "yoke/cli.py": (
            "import argparse\n"
            "def main():\n"
            "    parser = argparse.ArgumentParser(description='wheel fixture')\n"
            "    parser.parse_args()\n"
            "    return 0\n"
            "if __name__ == '__main__':\n"
            "    raise SystemExit(main())\n"
        ),
        "yoke-0.1.0.dist-info/METADATA": metadata,
        "yoke-0.1.0.dist-info/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: offline-test\n"
            "Root-Is-Purelib: true\nTag: py3-none-any\n"
        ),
        "yoke-0.1.0.dist-info/entry_points.txt": (
            "[console_scripts]\nyoke = yoke.cli:main\n"
        ),
    }
    if extra is not None:
        contents[extra] = "unexpected"
    contents["yoke-0.1.0.dist-info/RECORD"] = "".join(
        f"{name},,\n" for name in [*contents, "yoke-0.1.0.dist-info/RECORD"]
    )
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, data in contents.items():
            archive.writestr(name, data)
    return wheel


def make_artifact(tmp_path: Path, release: str = "v2") -> tuple[Path, str]:
    wheel = make_wheel(tmp_path / "build")
    artifact = tmp_path / f"{release}.tar"
    result = build_bundle(wheel, release, artifact)
    digest = result["sha256"]
    assert isinstance(digest, str)
    return artifact, digest


def write_tar(path: Path, entries: Sequence[tuple[str, bytes, bytes]]) -> str:
    with tarfile.open(path, "w", format=tarfile.USTAR_FORMAT) as archive:
        for name, data, kind in entries:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.size = len(data)
            if kind == tarfile.SYMTYPE:
                info.linkname = "/etc/passwd"
            archive.addfile(info, io.BytesIO(data))
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass
class FakeInstaller:
    fail: bool = False
    calls: list[Path] = field(default_factory=list)

    def install(self, wheel: Path, destination: Path) -> None:
        self.calls.append(destination)
        (destination / "bin").mkdir(parents=True)
        (destination / "bin/python").symlink_to(sys.executable)
        if self.fail:
            raise UpdateError("scripted offline install failure")


@dataclass
class FakeService:
    root: Path
    fail_start: set[str] = field(default_factory=set)
    fail_health: set[str] = field(default_factory=set)
    fail_stop: int = 0
    calls: list[tuple[str, str]] = field(default_factory=list)

    def _call(self, action: str) -> str:
        current = os.readlink(self.root / "current").split("/")[-1]
        self.calls.append((action, current))
        return current

    def check_contract(self) -> None:
        self._call("contract")

    def stop(self) -> None:
        self._call("stop")
        if self.fail_stop:
            self.fail_stop -= 1
            raise UpdateError("scripted stop failure")

    def start(self) -> None:
        if self._call("start") in self.fail_start:
            raise UpdateError("scripted start failure")

    def healthy(self) -> None:
        if self._call("healthy") in self.fail_health:
            raise UpdateError("scripted readiness failure")


def installed(tmp_path: Path) -> tuple[Updater, FakeInstaller, FakeService]:
    root = tmp_path / "opt/yoke"
    (root / "releases/initial/venv/bin").mkdir(parents=True)
    (root / "releases/initial/venv/bin/python").symlink_to(sys.executable)
    (root / "current").symlink_to("releases/initial")
    tool = Path(__file__).parents[1] / "src/yoke/update.py"
    (root / "updater.py").write_bytes(tool.read_bytes())
    installer = FakeInstaller()
    service = FakeService(root)
    return Updater(root, installer, service), installer, service


def staged(tmp_path: Path) -> tuple[Updater, FakeInstaller, FakeService]:
    updater, installer, service = installed(tmp_path)
    artifact, digest = make_artifact(tmp_path)
    updater.stage(artifact, digest)
    return updater, installer, service


def test_bundle_verified_and_reproducible(tmp_path: Path) -> None:
    artifact, digest = make_artifact(tmp_path)
    bundle = verify_bundle(artifact, digest)
    assert bundle.release == "v2"
    assert bundle.manifest()["state_format"] == 1
    second = tmp_path / "second.tar"
    assert (
        build_bundle(tmp_path / "build" / bundle.filename, "v2", second)["sha256"]
        == digest
    )
    with pytest.raises(FileExistsError):
        build_bundle(tmp_path / "build" / bundle.filename, "v2", second)


def test_digest_failure_precedes_any_install_or_filesystem_mutation(
    tmp_path: Path,
) -> None:
    artifact, _ = make_artifact(tmp_path)
    root = tmp_path / "absent"
    installer, service = FakeInstaller(), FakeService(root)
    updater = Updater(root, installer, service)
    with pytest.raises(UpdateError, match="SHA-256 mismatch"):
        updater.stage(artifact, "0" * 64)
    assert not root.exists()
    assert installer.calls == service.calls == []


@pytest.mark.parametrize("name", ["../escape", "/absolute", "x/y", "manifest.json"])
def test_bad_archive_names_and_duplicate_manifest(tmp_path: Path, name: str) -> None:
    path = tmp_path / "bad.tar"
    digest = write_tar(
        path, [("manifest.json", b"{}", tarfile.REGTYPE), (name, b"x", tarfile.REGTYPE)]
    )
    with pytest.raises(UpdateError):
        verify_bundle(path, digest)
    assert not (tmp_path.parent / "escape").exists()


def test_archive_link_and_hidden_trailing_payload_refused(tmp_path: Path) -> None:
    path = tmp_path / "linked.tar"
    digest = write_tar(path, [("wheel.whl", b"", tarfile.SYMTYPE)])
    with pytest.raises(UpdateError, match="archive member"):
        verify_bundle(path, digest)
    artifact, _ = make_artifact(tmp_path)
    artifact.write_bytes(artifact.read_bytes() + b"hidden")
    with pytest.raises(UpdateError, match="after archive terminator"):
        verify_bundle(artifact, hashlib.sha256(artifact.read_bytes()).hexdigest())


@pytest.mark.parametrize(
    "kind", [tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.GNUTYPE_LONGNAME]
)
def test_hidden_tar_extension_headers_are_refused(tmp_path: Path, kind: bytes) -> None:
    artifact, _ = make_artifact(tmp_path)
    header = tarfile.TarInfo("extension")
    header.type = kind
    payload = b"manifest.json\x00" if kind == tarfile.GNUTYPE_LONGNAME else b""
    header.size = len(payload)
    hidden = header.tobuf(format=tarfile.USTAR_FORMAT)
    hidden += payload + bytes((-len(payload)) % 512)
    artifact.write_bytes(hidden + artifact.read_bytes())
    with pytest.raises(UpdateError, match="archive member"):
        verify_bundle(artifact, hashlib.sha256(artifact.read_bytes()).hexdigest())


@pytest.mark.parametrize(
    "change", ["duplicate", "migration", "command", "wheel-hash", "bool"]
)
def test_manifest_is_strict_and_never_executes_hooks(
    tmp_path: Path, change: str
) -> None:
    artifact, digest = make_artifact(tmp_path)
    bundle = verify_bundle(artifact, digest)
    manifest = bundle.manifest()
    if change == "duplicate":
        encoded = (
            json.dumps(manifest)
            .replace('"state_format": 1', '"state_format": 1, "state_format": 1')
            .encode()
        )
    else:
        if change == "migration":
            manifest["state_format"] = 2
        elif change == "bool":
            manifest["state_format"] = True
        elif change == "command":
            manifest["post_install"] = "touch /unwanted"
        else:
            wheel = manifest["wheel"]
            assert isinstance(wheel, dict)
            wheel["sha256"] = "0" * 64
        encoded = json.dumps(manifest).encode()
    path = tmp_path / "changed.tar"
    digest = write_tar(
        path,
        [
            ("manifest.json", encoded, tarfile.REGTYPE),
            (bundle.filename, bundle.wheel, tarfile.REGTYPE),
        ],
    )
    with pytest.raises(UpdateError):
        verify_bundle(path, digest)


@pytest.mark.parametrize(
    "extra", ["../outside", "yoke/../outside", "surprise.pth", "yoke.data/scripts/root"]
)
def test_wheel_paths_rejected_before_package_install(
    tmp_path: Path, extra: str
) -> None:
    wheel = make_wheel(tmp_path, extra=extra)
    with pytest.raises(UpdateError):
        build_bundle(wheel, "v2", tmp_path / "bad.tar")


def test_dependency_wheel_refused(tmp_path: Path) -> None:
    wheel = make_wheel(tmp_path, dependency=True)
    with pytest.raises(UpdateError, match="dependency-free"):
        build_bundle(wheel, "v2", tmp_path / "bad.tar")


@pytest.mark.parametrize(
    "marker",
    [
        "extra == 'dev' or python_version >= '3'",
        "extra == 'other'",
        "python_version >= '3'",
    ],
)
def test_conditional_runtime_dependency_is_not_mistaken_for_dev_only(
    tmp_path: Path,
    marker: str,
) -> None:
    wheel = make_wheel(tmp_path)
    with zipfile.ZipFile(wheel) as archive:
        contents = {name: archive.read(name) for name in archive.namelist()}
    name = "yoke-0.1.0.dist-info/METADATA"
    contents[name] = contents[name].replace(b"extra == 'dev'", marker.encode())
    with zipfile.ZipFile(wheel, "w") as archive:
        for entry, data in contents.items():
            archive.writestr(entry, data)
    with pytest.raises(UpdateError, match="dependency-free"):
        build_bundle(wheel, "v2", tmp_path / "bad.tar")


def test_invalid_utf8_zip_filename_is_a_json_validation_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    wheel = make_wheel(tmp_path)
    data = bytearray(wheel.read_bytes())
    central = data.index(b"PK\x01\x02")
    for offset, flags_offset, name_offset in ((0, 6, 30), (central, 8, 46)):
        flags = int.from_bytes(
            data[offset + flags_offset : offset + flags_offset + 2], "little"
        )
        data[offset + flags_offset : offset + flags_offset + 2] = (
            flags | 0x800
        ).to_bytes(2, "little")
        data[offset + name_offset] = 0xFF
    wheel.write_bytes(data)
    assert (
        main(
            [
                "bundle",
                "--wheel",
                str(wheel),
                "--release",
                "v2",
                "--output",
                str(tmp_path / "bad.tar"),
            ]
        )
        == 2
    )
    assert "invalid wheel" in json.loads(capsys.readouterr().out)["error"]


def test_stage_keeps_live_service_and_persistent_state_untouched(
    tmp_path: Path,
) -> None:
    updater, installer, service = installed(tmp_path)
    etc, state = tmp_path / "etc", tmp_path / "var/lib"
    etc.mkdir()
    state.mkdir(parents=True)
    files = {
        etc / "agent.toml": b"node_id = 'unchanged'",
        etc / "psk": b"private",
        state / "state.json": b'{"seq":999,"pairing":{"producer":"peer"}}',
        state / "usbip-ownership.json": b"retained driver resource",
    }
    for path, data in files.items():
        path.write_bytes(data)
    artifact, digest = make_artifact(tmp_path)
    result = updater.stage(artifact, digest)
    assert result["current"] == "initial"
    assert result["releases"] == ["initial", "v2"]
    assert installer.calls == [updater.root / "releases/v2/venv"]
    assert service.calls == []
    updater.activate("v2")
    assert all(path.read_bytes() == data for path, data in files.items())
    with pytest.raises(UpdateError, match="already exists"):
        updater.stage(artifact, digest)


def test_dry_run_never_creates_or_changes_installation(tmp_path: Path) -> None:
    artifact, digest = make_artifact(tmp_path)
    root = tmp_path / "absent"
    updater = Updater(root, FakeInstaller(), FakeService(root))
    assert updater.stage(artifact, digest, dry_run=True)["dry_run"] is True
    assert not root.exists()
    live, installer, service = installed(tmp_path)
    live.stage(artifact, digest)
    before = (live.root / "update-state.json").read_bytes()
    assert live.activate("v2", dry_run=True)["previous"] == "initial"
    assert (live.root / "update-state.json").read_bytes() == before
    assert os.readlink(live.root / "current") == "releases/initial"
    assert len(installer.calls) == 1
    assert service.calls == [("contract", "initial")]


def test_install_failure_removes_only_candidate(tmp_path: Path) -> None:
    updater, installer, service = installed(tmp_path)
    installer.fail = True
    artifact, digest = make_artifact(tmp_path)
    with pytest.raises(UpdateError, match="staging failed"):
        updater.stage(artifact, digest)
    assert not (updater.root / "releases/v2").exists()
    assert updater.status()["current"] == "initial"
    assert updater._state().phase == "stage-failed"
    assert service.calls == []


def test_explicit_activation_and_manual_rollback(tmp_path: Path) -> None:
    updater, _, service = staged(tmp_path)
    updater.activate("v2")
    assert updater._state() == State("healthy", "v2", "initial")
    assert service.calls[-3:] == [
        ("stop", "initial"),
        ("start", "v2"),
        ("healthy", "v2"),
    ]
    updater.rollback()
    assert updater._state() == State("rolled-back", "initial", "v2")
    assert updater.status()["current"] == "initial"


@pytest.mark.parametrize("failure", ["start", "health", "stop"])
def test_failed_activation_restores_prior_release(tmp_path: Path, failure: str) -> None:
    updater, _, service = staged(tmp_path)
    if failure == "start":
        service.fail_start.add("v2")
    elif failure == "health":
        service.fail_health.add("v2")
    else:
        service.fail_stop = 1
    updater.activate("v2")
    state = updater._state()
    assert (state.phase, state.active, state.previous, state.candidate) == (
        "rolled-back",
        "initial",
        None,
        "v2",
    )
    assert updater.status()["current"] == "initial"
    assert service.calls[-3:] == [
        ("stop", "v2" if failure != "stop" else "initial"),
        ("start", "initial"),
        ("healthy", "initial"),
    ]
    if failure == "stop":
        assert ("start", "v2") not in service.calls
    with pytest.raises(UpdateError, match="no previous"):
        updater.rollback()


def test_operation_result_is_captured_before_releasing_update_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updater, _, service = staged(tmp_path)
    service.fail_start.add("v2")
    later_artifact, later_digest = make_artifact(tmp_path, "v3")
    other = Updater(updater.root, FakeInstaller(), FakeService(updater.root))
    original_lock = updater._lock

    @contextmanager
    def race_after_unlock() -> Iterator[None]:
        with original_lock():
            yield
        other.stage(later_artifact, later_digest)

    monkeypatch.setattr(updater, "_lock", race_after_unlock)
    result = updater.activate("v2")
    state = result["state"]
    assert isinstance(state, dict) and state["phase"] == "rolled-back"
    assert state["candidate"] == "v2"
    assert updater._state().phase == "staged"
    assert updater._state().candidate == "v3"


def test_rollback_failure_is_visible_and_recoverable(tmp_path: Path) -> None:
    updater, _, service = staged(tmp_path)
    service.fail_start.update({"v2", "initial"})
    updater.activate("v2")
    assert updater._state().phase == "recovery-required"
    assert updater._state().previous == "initial"
    calls = service.calls.copy()
    updater.recover(offline=True)
    assert updater._state().phase == "rolled-back"
    assert updater.status()["current"] == "initial"
    assert service.calls == calls


@pytest.mark.parametrize("switch", [False, True])
def test_power_loss_before_or_after_link_switch_recovers_before_boot(
    tmp_path: Path, switch: bool
) -> None:
    updater, _, service = staged(tmp_path)
    updater._write(State("activating", "initial", "initial", "v2"))
    if switch:
        updater._switch("v2")
    updater.recover(offline=True)
    assert updater._state().phase == "rolled-back"
    assert updater.status()["current"] == "initial"
    assert service.calls == []


def test_recovery_can_restore_even_when_candidate_python_disappeared(
    tmp_path: Path,
) -> None:
    updater, _, service = staged(tmp_path)
    updater._write(State("activating", "initial", "initial", "v2"))
    updater._switch("v2")
    (updater.root / "releases/v2/venv/bin/python").unlink()
    updater.recover(offline=True)
    assert updater.status()["current"] == "initial"
    assert updater._state().phase == "rolled-back"
    assert service.calls == []


def test_missing_rollback_runtime_does_not_claim_recovery(tmp_path: Path) -> None:
    updater, _, _ = staged(tmp_path)
    updater._write(State("activating", "initial", "initial", "v2"))
    updater._switch("v2")
    (updater.root / "releases/initial/venv/bin/python").unlink()
    updater.recover(offline=True)
    assert updater._state().phase == "recovery-required"
    assert updater.status()["current"] == "v2"


@pytest.mark.parametrize("detail", ["💥" * 4096, "\x00" * 4096, "\udcff" * 4096])
def test_failure_detail_cannot_overflow_or_corrupt_recovery_journal(
    tmp_path: Path,
    detail: str,
) -> None:
    updater, _, service = staged(tmp_path)
    updater._write(State("activating", "initial", "initial", "v2", detail))
    assert (updater.root / "update-state.json").stat().st_size <= 16384
    updater.status()
    updater.recover(offline=True)
    assert updater._state().phase == "rolled-back"
    assert updater.status()["current"] == "initial"
    assert service.calls == []


def test_interrupted_stage_is_not_a_published_release(tmp_path: Path) -> None:
    updater, _, _ = installed(tmp_path)
    updater._prepare()
    (updater.root / "releases/v2/venv/bin").mkdir(parents=True)
    (updater.root / "releases/v2/venv/bin/python").symlink_to(sys.executable)
    updater._write(State("staging", "initial", None, "v2"))
    updater.recover(offline=True)
    assert updater.status()["releases"] == ["initial"]
    with pytest.raises(UpdateError):
        updater.activate("v2")


def test_lock_prevents_interleaved_updates(tmp_path: Path) -> None:
    updater, _, _ = installed(tmp_path)
    artifact, digest = make_artifact(tmp_path)
    other = Updater(updater.root, FakeInstaller(), FakeService(updater.root))
    with updater._lock(), pytest.raises(UpdateError, match="another updater"):
        other.stage(artifact, digest)
    assert not (updater.root / "releases/v2").exists()


def test_symlink_root_lock_and_current_are_not_followed(tmp_path: Path) -> None:
    updater, _, _ = installed(tmp_path)
    link = tmp_path / "linked"
    link.symlink_to(updater.root)
    with pytest.raises(UpdateError, match="symbolic links"):
        Updater(link, FakeInstaller(), FakeService(link))
    (updater.root / ".update.lock").symlink_to(tmp_path / "missing-target")
    with pytest.raises(UpdateError, match="update lock"), updater._lock():
        pass
    assert not (tmp_path / "missing-target").exists()
    (updater.root / "current").unlink()
    (updater.root / "current").symlink_to("../../../outside")
    with pytest.raises(UpdateError, match="unsafe target"):
        updater.status()


@dataclass
class ScriptRunner:
    root: Path
    props: str = ""
    recovery_active: bool = True
    recovery_props: str = ""
    calls: list[tuple[tuple[str, ...], float]] = field(default_factory=list)

    def run(self, argv: Sequence[str], *, timeout: float) -> RunResult:
        self.calls.append((tuple(argv), timeout))
        if "show" in argv:
            if "yoke-update-recover.service" in argv:
                return RunResult(
                    0,
                    self.recovery_props
                    or (
                        "Type=oneshot\nRemainAfterExit=yes\nUser=root\n"
                        "ExecStart={ path=/usr/bin/python3 ; "
                        f"argv[]=/usr/bin/python3 {self.root}/updater.py "
                        "recover --offline ; }\n"
                    ),
                )
            return RunResult(
                0,
                self.props
                or (
                    "Type=notify\nNotifyAccess=main\n"
                    "Requires=yoke-update-recover.service\n"
                    "After=network.target yoke-update-recover.service\n"
                    f"ExecStart={{ path={self.root}/current/venv/bin/python ; "
                    f"argv[]={self.root}/current/venv/bin/python -m yoke.cli agent "
                    "--config /etc/yoke/agent.toml ; }\n"
                ),
            )
        if "yoke-update-recover.service" in argv and not self.recovery_active:
            return RunResult(3, "inactive")
        return RunResult(0)


def test_systemd_notify_readiness_contract_and_bounded_commands(tmp_path: Path) -> None:
    (tmp_path / "updater.py").write_text("# trusted fixed script fixture\n")
    runner = ScriptRunner(tmp_path)
    pauses: list[float] = []
    service = SystemdService(tmp_path, runner, pause=pauses.append)
    service.check_contract()
    service.stop()
    service.start()
    service.healthy()
    assert pauses == [1, 1]
    assert all(0 < timeout <= 100 for _, timeout in runner.calls)
    assert (
        sum("is-active" in call and "yoked.service" in call for call, _ in runner.calls)
        == 3
    )


@pytest.mark.parametrize(
    "props",
    [
        "Type=exec\nNotifyAccess=main",
        "Type=notify\nNotifyAccess=all",
        "Type=notify\nNotifyAccess=main\nExecStart={ path=/wrong/python ; }",
    ],
)
def test_wrong_unit_contract_refused(tmp_path: Path, props: str) -> None:
    with pytest.raises(UpdateError, match="notify readiness"):
        SystemdService(tmp_path, ScriptRunner(tmp_path, props)).check_contract()


def test_inactive_boot_recovery_unit_prevents_activation_deadlock(
    tmp_path: Path,
) -> None:
    (tmp_path / "updater.py").write_text("# trusted fixed script fixture\n")
    with pytest.raises(UpdateError, match="command failed"):
        SystemdService(
            tmp_path, ScriptRunner(tmp_path, recovery_active=False)
        ).check_contract()


def test_unit_must_require_and_follow_recovery(tmp_path: Path) -> None:
    runner = ScriptRunner(tmp_path)
    correct = runner.run(["systemctl", "show", "yoked.service"], timeout=5).output
    runner.props = correct.replace("Requires=yoke-update-recover.service", "Requires=")
    with pytest.raises(UpdateError, match="require and start after"):
        SystemdService(tmp_path, runner).check_contract()


def test_recovery_unit_cannot_use_another_installation(tmp_path: Path) -> None:
    runner = ScriptRunner(
        tmp_path,
        recovery_props=(
            "Type=oneshot\nRemainAfterExit=yes\nUser=root\n"
            "ExecStart={ path=/usr/bin/python3 ; "
            "argv[]=/usr/bin/python3 /wrong/updater.py recover --offline ; }\n"
        ),
    )
    with pytest.raises(UpdateError, match="fixed offline updater"):
        SystemdService(tmp_path, runner).check_contract()


def test_subprocess_timeout_output_bounds_and_clean_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = SubprocessRunner()
    monkeypatch.setenv("NOTIFY_SOCKET", "/should/not/inherit")
    monkeypatch.setenv("PYTHONPATH", "/should/not/import")
    result = runner.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import os; print(os.getenv('NOTIFY_SOCKET'), os.getenv('PYTHONPATH'))",
        ],
        timeout=5,
    )
    assert result.output == "None None\n"
    with pytest.raises(UpdateError, match="timed out"):
        runner.run([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.05)
    with pytest.raises(UpdateError, match="output exceeds"):
        runner.run(
            [sys.executable, "-c", f"print('x' * {MAX_OUTPUT + 8192})"], timeout=5
        )


def test_cli_validation_is_machine_readable(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    assert main(["--root", str(tmp_path / "absent"), "activate", "../bad"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["schema_version"] == 1
    assert "invalid release" in result["error"]


def test_standalone_cli_real_offline_install_activate_and_rollback(
    tmp_path: Path,
) -> None:
    """Actual wheel/pip/venv/CLI fixtures; systemd alone is a script seam."""
    updater, _, _ = installed(tmp_path)
    artifact, digest = make_artifact(tmp_path)
    systemctl = tmp_path / "fake-systemctl"
    systemctl.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, sys\n"
        f"root = pathlib.Path({str(updater.root)!r})\n"
        "if 'show' in sys.argv:\n"
        "    if 'yoke-update-recover.service' in sys.argv:\n"
        "        print('Type=oneshot\\nRemainAfterExit=yes\\nUser=root')\n"
        "        print(f'ExecStart={{ path=/usr/bin/python3 ; "
        "argv[]=/usr/bin/python3 {root}/updater.py recover --offline ; }}')\n"
        "        sys.exit(0)\n"
        "    print('Type=notify\\nNotifyAccess=main')\n"
        "    print('Requires=yoke-update-recover.service')\n"
        "    print('After=network.target yoke-update-recover.service')\n"
        "    print(f'ExecStart={{ path={root}/current/venv/bin/python ; "
        "argv[]={root}/current/venv/bin/python -m yoke.cli agent "
        "--config /etc/yoke/agent.toml ; }}')\n"
        "if 'start' in sys.argv and (root / 'fail-start').exists():\n"
        "    current = (root / 'current').readlink().name\n"
        "    if (root / 'fail-start').read_text() == current: sys.exit(1)\n",
        encoding="utf-8",
    )
    systemctl.chmod(0o755)
    tool = updater.root / "updater.py"
    prefix = [
        sys.executable,
        "-I",
        str(tool),
        "--root",
        str(updater.root),
        "--systemctl",
        str(systemctl),
    ]

    def invoke(*args: str, expected: int = 0) -> dict[str, object]:
        proc = subprocess.run(
            [*prefix, *args], capture_output=True, text=True, timeout=45, check=False
        )
        assert proc.returncode == expected, proc.stdout + proc.stderr
        value = json.loads(proc.stdout)
        assert isinstance(value, dict)
        return value

    assert invoke("stage", str(artifact), "--sha256", digest)["current"] == "initial"
    installed_python = updater.root / "releases/v2/venv/bin/python"
    help_result = subprocess.run(
        [str(installed_python), "-I", "-m", "yoke.cli", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert help_result.returncode == 0
    assert "wheel fixture" in help_result.stdout
    script = updater.root / "releases/v2/venv/bin/yoke"
    # pip uses a shell trampoline when the interpreter path exceeds the
    # operating system's shebang limit. Both forms must use the permanent path.
    assert str(installed_python) in script.read_text()
    assert (
        subprocess.run(
            [str(script), "--help"], capture_output=True, timeout=10, check=False
        ).returncode
        == 0
    )
    assert invoke("activate", "v2")["current"] == "v2"
    assert invoke("rollback")["current"] == "initial"
    (updater.root / "fail-start").write_text("v2")
    failed = invoke("activate", "v2", expected=3)
    assert failed["current"] == "initial"
    state = failed["state"]
    assert isinstance(state, dict) and state["phase"] == "rolled-back"
