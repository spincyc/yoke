"""Offline checks of the ZERO 3E privilege recipe (ADR 0024).

The installer is run in its ``--root`` staging mode: no account, firewall,
udev or systemd state on the test host is touched. Kernel, udev, systemd
and nftables behavior on the boxes remains ⚠ bench-unverified; these tests
pin the recipe's structure and privilege boundaries.
"""

from __future__ import annotations

import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from yoke.config import AgentConfig
from yoke.core.types import Kind, NodeConfig, Role
from yoke.drivers.factory import build_driver
from yoke.drivers.usbip import UsbipBindDriver

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "packaging" / "zero3e" / "install.sh"
UNITS = Path("etc/systemd/system")


def install(stage: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(INSTALL), *args, "--root", str(stage)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def device(stage: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return install(
        stage,
        "device",
        "--node-id",
        "usb-dev",
        "--host",
        "10.20.30.2",
        "--busid",
        "1-1",
        "--busid",
        "1-1.2",
        *extra,
    )


def directives(path: Path) -> dict[str, list[str]]:
    """``Section.Key`` -> values, in order (systemd allows repeats)."""
    section = ""
    found: dict[str, list[str]] = {}
    for line in path.read_text().splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if text.startswith("["):
            section = text.strip("[]")
            continue
        key, _, value = text.partition("=")
        found.setdefault(f"{section}.{key}", []).append(value)
    return found


def test_device_install_stages_the_complete_recipe(tmp_path: Path) -> None:
    result = device(tmp_path)
    assert result.returncode == 0, result.stderr
    staged = {p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")}
    for name in (
        "yoked.service",
        "yoke-update-recover.service",
        "yoked.service.d/10-zero3e-device.conf",
        "yoke-usbipd.service",
        "yoke-usbip-bind@.service",
        "yoke-usbip-admission.service",
        "yoke-usbip-admission.path",
    ):
        assert f"{UNITS}/{name}" in staged
    assert "etc/modules-load.d/yoke-usbip-host.conf" in staged
    modules = (tmp_path / "etc/modules-load.d/yoke-usbip-host.conf").read_text()
    assert "usbip-host" in modules.split()
    rules = (tmp_path / "etc/udev/rules.d/70-yoke-usbip-bind.rules").read_text()
    for busid in ("1-1", "1-1.2"):
        assert (
            f'KERNEL=="{busid}", TAG+="systemd", '
            'ENV{SYSTEMD_WANTS}+="yoke-usbip-bind@%k.service"'
        ) in rules
    assert "@" + "PORT@" not in "".join(
        p.read_text() for p in (tmp_path / UNITS).rglob("*") if p.is_file()
    )
    # No gadget worker files: the computer-side gadget unit is separate work.
    assert not [name for name in staged if "gadget" in name]


def test_agent_stays_unprivileged_and_helpers_are_narrow(tmp_path: Path) -> None:
    assert device(tmp_path).returncode == 0
    units = tmp_path / UNITS
    agent = directives(units / "yoked.service")
    dropin = directives(units / "yoked.service.d/10-zero3e-device.conf")
    assert agent["Service.User"] == ["yoke"]
    assert agent["Service.NoNewPrivileges"] == ["true"]
    assert agent["Service.CapabilityBoundingSet"] == [""]
    assert "Service.AmbientCapabilities" not in agent
    assert not [
        k for k in dropin if "Capabilit" in k or k.endswith((".User", ".Group"))
    ]
    assert dropin["Service.RuntimeDirectory"] == ["yoke"]
    # ADR 0023: ordered after time sync, never dependent on it.
    assert "time-sync.target" in agent["Unit.After"][0].split()
    for key in ("Unit.Wants", "Unit.Requires", "Unit.BindsTo"):
        assert all("time-sync" not in value for value in agent.get(key, []))
    helper = directives(units / "yoke-usbip-admission.service")
    assert helper["Service.CapabilityBoundingSet"] == ["CAP_NET_ADMIN"]
    assert helper["Service.ExecStart"] == [
        "/opt/yoke/current/venv/bin/python -m yoke.cli usbip-admission apply "
        "--port 3240"
    ]
    assert "yoke-usbipd.service" in helper["Unit.Before"][0].split()
    daemon = directives(units / "yoke-usbipd.service")
    assert daemon["Unit.Requires"] == ["yoke-usbip-admission.service"]
    assert daemon["Service.ExecStart"] == ["/usr/sbin/usbipd --tcp-port 3240"]
    assert daemon["Service.CapabilityBoundingSet"] == [""]
    bind = directives(units / "yoke-usbip-bind@.service")
    assert bind["Service.ExecStart"] == ["/usr/sbin/usbip bind -b %i"]
    assert bind["Service.CapabilityBoundingSet"] == [""]
    assert bind["Service.PrivateNetwork"] == ["true"]
    trigger = directives(units / "yoke-usbip-admission.path")
    assert trigger["Path.PathChanged"] == ["/run/yoke/usbip-admission.json"]


def test_device_agent_config_builds_the_zero3e_driver(tmp_path: Path) -> None:
    assert device(tmp_path, "--port", "4000").returncode == 0
    config = tmp_path / "etc/yoke/agent.toml"
    assert config.stat().st_mode & 0o777 == 0o640
    data = tomllib.loads(config.read_text())
    assert data["driver"] == {
        "type": "usbip",
        "host": "10.20.30.2",
        "port": 4000,
        "busids": ["1-1", "1-1.2"],
        "bindings": "external",
        "admission": "nftables",
    }
    assert data["psk_file"] == "/etc/yoke/psk" and "open_mode" not in data
    cfg = AgentConfig(
        node=NodeConfig(data["node_id"], Role(data["role"]), Kind(data["kind"])),
        state_file=Path(data["state_file"]),
        psk=b"k" * 32,
        driver=data["driver"],
    )
    driver = build_driver(cfg)
    assert isinstance(driver, UsbipBindDriver)
    assert driver.describe() == {
        "host": "10.20.30.2",
        "port": 4000,
        "busids": ["1-1", "1-1.2"],
    }
    helper = (tmp_path / UNITS / "yoke-usbip-admission.service").read_text()
    assert "--port 4000" in helper


def test_existing_agent_config_is_never_overwritten(tmp_path: Path) -> None:
    config = tmp_path / "etc/yoke/agent.toml"
    config.parent.mkdir(parents=True)
    config.write_text("# operator edits\n")
    result = device(tmp_path)
    assert result.returncode == 0
    assert config.read_text() == "# operator edits\n"
    assert "keeping existing" in result.stdout


@pytest.mark.parametrize(
    "args",
    [
        ["device", "--node-id", "Bad_ID", "--host", "10.0.0.1", "--busid", "1-1"],
        ["device", "--node-id", "ok", "--host", "10.0.0.300", "--busid", "1-1"],
        ["device", "--node-id", "ok", "--host", "box.local", "--busid", "1-1"],
        ["device", "--node-id", "ok", "--host", "10.0.0.1", "--busid", "1-1;rm"],
        ["device", "--node-id", "ok", "--host", "10.0.0.1"],
        [
            "device",
            "--node-id",
            "ok",
            "--host",
            "10.0.0.1",
            "--busid",
            "1-1",
            "--port",
            "0",
        ],
        ["computer", "--busid", "1-1"],
        ["gadget"],
    ],
)
def test_invalid_arguments_stage_nothing(tmp_path: Path, args: list[str]) -> None:
    result = install(tmp_path, *args)
    assert result.returncode == 2
    assert not any(tmp_path.iterdir())


def test_computer_role_installs_only_the_unprivileged_agent(tmp_path: Path) -> None:
    assert install(tmp_path, "computer").returncode == 0
    files = sorted(
        p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file()
    )
    assert files == [
        f"{UNITS}/yoke-update-recover.service",
        f"{UNITS}/yoked.service",
    ]


def test_vhci_lab_writes_record_and_sysfs_access_together(tmp_path: Path) -> None:
    assert install(tmp_path, "vhci-lab").returncode == 0
    dropin = directives(tmp_path / UNITS / "yoked.service.d/20-usbip-vhci.conf")
    assert dropin["Service.ReadWritePaths"] == ["/run/vhci_hcd"]
    assert not [key for key in dropin if "Capabilit" in key or "User" in key]
    tmpfiles = (tmp_path / "etc/tmpfiles.d/yoke-vhci.conf").read_text()
    assert "d /run/vhci_hcd 0700 yoke yoke -" in tmpfiles
    rules = (tmp_path / "etc/udev/rules.d/70-yoke-vhci.rules").read_text()
    assert 'KERNEL=="vhci_hcd.*"' in rules
    assert "chgrp yoke /sys%p/attach /sys%p/detach" in rules


def test_staged_units_pass_systemd_analyze_verify(tmp_path: Path) -> None:
    analyze = shutil.which("systemd-analyze")
    if analyze is None:
        pytest.skip("systemd-analyze is not installed")
    assert device(tmp_path).returncode == 0
    for stub in (
        "opt/yoke/current/venv/bin/python",
        "usr/sbin/usbipd",
        "usr/sbin/usbip",
        "usr/bin/sh",
    ):
        path = tmp_path / stub
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)
    (tmp_path / "bin").symlink_to("usr/bin")
    result = subprocess.run(
        [
            analyze,
            f"--root={tmp_path}",
            "verify",
            "yoked.service",
            "yoke-usbipd.service",
            "yoke-usbip-admission.service",
            "yoke-usbip-admission.path",
            "yoke-usbip-bind@1-1.2.service",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        cwd=tmp_path / UNITS,
    )
    if "--root" in result.stderr and "not supported" in result.stderr:
        pytest.skip("this systemd-analyze cannot verify an alternate root")
    problems = [
        line
        for line in result.stderr.splitlines()
        if "Unknown key" in line
        or "Failed to parse" in line
        or "Unknown section" in line
        or "is not executable" in line
    ]
    assert not problems, result.stderr
