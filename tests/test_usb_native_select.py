"""Selecting the native gadget worker (ADR 0027): CLI, packaging, options.

The agent's usb-gadget driver supervises either worker through the same
control socket, so selection is local to the computer-side box: the
``--implementation`` switch (or ``$YOKE_GADGET_WORKER``) of the Python
entry point, or the unit drop-in ``install.sh computer --native-worker``
stages. The native binary accepts every option the Python CLI forwards.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from tests.test_packaging import UNITS, directives, install
from tests.usbsim.native import build
from yoke.usb import worker


class ExecedError(Exception):
    pass


def capture_exec(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, list[str]]]:
    calls: list[tuple[str, list[str]]] = []

    def fake_execv(path: str, argv: list[str]) -> Any:
        calls.append((path, list(argv)))
        raise ExecedError

    monkeypatch.setattr(os, "execv", fake_execv)
    return calls


def test_native_implementation_execs_the_c_worker_with_the_same_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = capture_exec(monkeypatch)
    with pytest.raises(ExecedError):
        worker.main(
            [
                "--implementation",
                "native",
                "--native-binary",
                "/opt/w",
                "--udc",
                "fcc00000.usb",
                "--target",
                "10.0.0.2:3240/1-1",
                "--source",
                "10.0.0.3",
                "--out-window",
                "4",
            ]
        )
    path, argv = calls[0]
    assert path == argv[0] == "/opt/w"
    pairs = dict(zip(argv[1::2], argv[2::2], strict=True))
    assert pairs["--udc"] == "fcc00000.usb" and pairs["--out-window"] == "4"
    assert pairs["--target"] == "10.0.0.2:3240/1-1" and pairs["--source"] == "10.0.0.3"
    assert pairs["--socket"] == "/run/yoke-gadget/control.sock"


def test_environment_selects_the_native_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = capture_exec(monkeypatch)
    monkeypatch.setenv("YOKE_GADGET_WORKER", "native")
    with pytest.raises(ExecedError):
        worker.main(["--udc", "u"])
    assert calls[0][0] == worker.NATIVE_BINARY


def test_install_computer_with_native_worker_stages_binary_and_dropin(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "yoke-gadget-worker"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    root = tmp_path / "root"
    root.mkdir()
    assert install(root, "computer", "--native-worker", str(binary)).returncode == 0
    staged = root / "usr/local/libexec/yoke/yoke-gadget-worker"
    assert staged.read_text() == "#!/bin/sh\n" and os.access(staged, os.X_OK)
    # The drop-in only selects the implementation; the Python entry point
    # executes its --native-binary default, which is where it was staged.
    dropin_dir = root / UNITS / "yoke-gadget.service.d"
    dropin = directives(dropin_dir / "20-native-worker.conf")
    assert dropin == {"Service.Environment": ["YOKE_GADGET_WORKER=native"]}
    assert "/" + staged.relative_to(root).as_posix() == worker.NATIVE_BINARY
    unit = directives(root / UNITS / "yoke-gadget.service")
    assert unit["Service.User"] == ["yoke-gadget"]  # the sandbox is unchanged
    # A missing native binary exits EX_CONFIG (78): no restart loop.
    assert unit["Service.RestartPreventExitStatus"] == ["78"]
    assert unit["Service.ExecStart"][0].startswith("/opt/yoke/current/venv/bin/python")
    # Re-running without the option selects the Python worker again.
    result = install(root, "computer")
    assert result.returncode == 0, result.stderr
    assert not dropin_dir.exists()
    assert "Python gadget worker is selected again" in result.stdout
    assert install(root, "computer").returncode == 0  # idempotent
    # And back: the option restores the drop-in.
    assert install(root, "computer", "--native-worker", str(binary)).returncode == 0
    assert (dropin_dir / "20-native-worker.conf").is_file()


def test_native_environment_reaches_the_python_entry_point(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """What the drop-in's Environment= does to the unit's ExecStart."""
    calls = capture_exec(monkeypatch)
    root = tmp_path / "root"
    root.mkdir()
    binary = tmp_path / "w"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    assert install(root, "computer", "--native-worker", str(binary)).returncode == 0
    env = directives(root / UNITS / "yoke-gadget.service.d/20-native-worker.conf")
    name, _, value = env["Service.Environment"][0].partition("=")
    monkeypatch.setenv(name, value)
    unit = (root / UNITS / "yoke-gadget.service").read_text().replace("\\\n", " ")
    exec_start = next(x for x in unit.splitlines() if x.startswith("ExecStart="))
    argv = exec_start.removeprefix("ExecStart=").split()
    assert argv[1:3] == ["-m", "yoke.usb.worker"]
    with pytest.raises(ExecedError):
        worker.main(argv[3:])
    path, forwarded = calls[0]
    assert path == worker.NATIVE_BINARY
    assert forwarded[forwarded.index("--udc") + 1] == "fcc00000.usb"


@pytest.mark.parametrize(
    "args",
    [
        ["device", "--native-worker", "/bin/true"],
        ["computer", "--native-worker", "/nope"],
    ],
)
def test_native_worker_option_is_validated(tmp_path: Path, args: list[str]) -> None:
    assert install(tmp_path, *args).returncode == 2
    assert not any(tmp_path.iterdir())


def test_native_binary_accepts_every_forwarded_option(tmp_path: Path) -> None:
    binary = build("prod", sanitize=False)
    args = worker._parser().parse_args(
        ["--udc", "dummy_udc.0", "--target", "127.0.0.1:9/1-1", "--source", "127.0.0.1"]
    )
    args.native_binary = str(binary)
    command = worker.native_command(args)
    assert subprocess.run([str(binary), "--help"], capture_output=True).returncode == 0
    bad = subprocess.run([str(binary), "--udc", "u", "--bogus"], capture_output=True)
    assert bad.returncode == 2
    process = subprocess.Popen(command, stderr=subprocess.PIPE, text=True)
    time.sleep(0.5)  # retrying a refused connection, as the Python worker would
    process.send_signal(signal.SIGTERM)
    assert process.wait(10) == 0
    assert process.stderr is not None and "native worker on UDC dummy_udc.0" in (
        process.stderr.read()
    )


@pytest.mark.parametrize(
    "level",
    ["DEBUG", "INFO", "WARN", "WARNING", "ERROR", "CRITICAL", "FATAL", "NOTSET"],
)
def test_native_binary_accepts_every_python_log_level(level: str) -> None:
    """logging._nameToLevel, which the Python worker's --log-level takes."""
    assert level in logging.getLevelNamesMapping()
    binary = build("prod", sanitize=False)
    result = subprocess.run(
        [str(binary), "--udc", "u", "--log-level", level, "--target", "x:1/bogus"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    # Options are checked in order: the level passed, the bad target failed.
    assert result.returncode == 2 and "bad --target" in result.stderr, result.stderr


def test_native_binary_log_levels_filter_like_logging(tmp_path: Path) -> None:
    binary = build("prod", sanitize=False)
    bad = subprocess.run(
        [str(binary), "--udc", "u", "--log-level", "LOUD"],
        capture_output=True,
        text=True,
    )
    assert bad.returncode == 2 and "invalid value for --log-level" in bad.stderr
    outputs = {}
    for level in ("INFO", "WARN", "CRITICAL"):
        process = subprocess.Popen(
            [
                str(binary),
                "--udc",
                "u",
                "--log-level",
                level,
                "--target",
                "127.0.0.1:9/1-1",
            ],
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(0.5)  # the startup line (INFO) and a failed attempt (WARNING)
        process.send_signal(signal.SIGTERM)
        assert process.wait(10) == 0
        assert process.stderr is not None
        outputs[level] = process.stderr.read()
    assert "INFO yoke.usb.worker: native worker on UDC u" in outputs["INFO"]
    assert "INFO " not in outputs["WARN"]
    assert "WARNING yoke.usb.worker: session attempt failed" in outputs["WARN"]
    assert outputs["CRITICAL"] == ""
