#!/usr/bin/env python3
"""Explicit software-only firmware verification; installs build tools in .scratch.

Requires git, bash, cmake, ninja, cc, OpenSSL, and Python 3.12+. Default mode
downloads pinned developer dependencies. --offline reuses installed tools and
performs no installation. Neither mode contacts or flashes a device.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRATCH = ROOT / ".scratch/esp32-ota"
IDF = SCRATCH / "esp-idf"
IDF_TAG = "v5.5.6"
IDF_COMMIT = "880a203ae603d08a94d06aa01e0d6761958e5e9e"
MINIMUM_TESTS = 132
# ESP32-S3 internal SRAM instruction bus; flash-mapped code is 0x42xxxxxx.
IRAM = range(0x40370000, 0x403E0000)
ISR_SYMBOLS = ("ir_tx_fill", "ir_tx_extend", "__wrap_esp_panic_handler")


def run(command: list[str], env: dict[str, str], *, capture: bool = False) -> str:
    result = subprocess.run(
        command, cwd=ROOT, env=env, check=True, text=True, capture_output=capture
    )
    return result.stdout if capture else ""


def environment(jobs: int) -> dict[str, str]:
    env = dict(os.environ)
    for key in ("PYTHONHOME", "VIRTUAL_ENV", "IDF_PYTHON_ENV_PATH", "PYTEST_ADDOPTS"):
        env.pop(key, None)
    # export.sh must detect the same Python that prepared the SDK environment.
    env["PATH"] = os.pathsep.join(
        [str(Path(sys.executable).parent), str(Path.home() / ".local/bin"), env["PATH"]]
    )
    values = {
        "IDF_PATH": IDF,
        "IDF_TOOLS_PATH": SCRATCH / "tools",
        "TMPDIR": SCRATCH / "tmp",
        "XDG_CACHE_HOME": SCRATCH / "cache",
        "PIP_CACHE_DIR": SCRATCH / "cache/pip",
        "PYTHONPYCACHEPREFIX": SCRATCH / "cache/pycache",
        "CCACHE_DIR": SCRATCH / "cache/ccache",
    }
    for key, value in values.items():
        env[key] = str(value)
        value.mkdir(parents=True, exist_ok=True)
    env["IDF_PY_BUILD_JOBS"] = str(jobs)
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    return env


def check_sdk(env: dict[str, str]) -> None:
    actual = run(["git", "-C", str(IDF), "rev-parse", "HEAD"], env, capture=True)
    if actual.strip() != IDF_COMMIT:
        raise RuntimeError(f"SDK must be {IDF_TAG} commit {IDF_COMMIT}")
    changes = run(
        ["git", "-C", str(IDF), "status", "--porcelain", "--untracked-files=no"],
        env,
        capture=True,
    )
    if changes.strip():
        raise RuntimeError("SDK or its submodules have tracked modifications")
    modules = run(
        ["git", "-C", str(IDF), "submodule", "status", "--recursive"], env, capture=True
    )
    if not modules or any(line[:1] != " " for line in modules.splitlines()):
        raise RuntimeError("SDK submodules must match the pinned superproject")


def prepare_sdk(env: dict[str, str], offline: bool) -> None:
    if not (IDF / ".git").exists():
        if offline:
            raise RuntimeError("offline mode requires the pinned SDK already installed")
        # environment() creates IDF's parent tree, not repository contents.
        run(
            [
                "git",
                "clone",
                "--branch",
                IDF_TAG,
                "--depth",
                "1",
                "--recursive",
                "--shallow-submodules",
                "https://github.com/espressif/esp-idf.git",
                str(IDF),
            ],
            env,
        )
    check_sdk(env)
    if not offline:
        print(
            "[firmware] Installing/reusing pinned ESP32-S3 developer tools", flush=True
        )
        installer = str(IDF / "tools/idf_tools.py")
        run([sys.executable, installer, "install", "--targets=esp32s3"], env)
        run([sys.executable, installer, "install-python-env", "--features=core"], env)


def test_requirements() -> list[str]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    requirements = project["project"]["optional-dependencies"]["dev"]
    selected = [
        value
        for value in requirements
        if value.startswith(("pytest==", "pytest-asyncio=="))
    ]
    if len(selected) != 2:
        raise RuntimeError("Expected pinned pytest and pytest-asyncio dev requirements")
    return selected


def prepare_test_python(
    env: dict[str, str], offline: bool, requested: Path | None
) -> Path:
    python = requested.absolute() if requested else SCRATCH / "verify-venv/bin/python"
    if not python.exists():
        if requested or offline:
            raise RuntimeError(f"test Python is unavailable: {python}")
        run([sys.executable, "-m", "venv", str(python.parent.parent)], env)
    requirements = test_requirements()
    code = (
        "import importlib.metadata,sys; "
        "sys.exit(any(importlib.metadata.version(item.split('==')[0]) != "
        "item.split('==')[1] for item in sys.argv[1:]))"
    )
    check = subprocess.run(
        [str(python), "-c", code, *requirements], env=env, capture_output=True, cwd=ROOT
    )
    if check.returncode:
        if offline:
            raise RuntimeError(
                "offline mode requires the pinned pytest dev dependencies"
            )
        if requested:
            raise RuntimeError(
                "provided test Python lacks pinned dependencies; not modifying it"
            )
        run([str(python), "-m", "pip", "install", *requirements], env)
    return python


def require_complete_report(report: Path) -> int:
    cases = ET.parse(report).getroot().findall(".//testcase")
    if len(cases) < MINIMUM_TESTS:
        raise RuntimeError(
            f"Expected at least {MINIMUM_TESTS} firmware tests; got {len(cases)}"
        )
    if any(case.find("skipped") is not None for case in cases):
        raise RuntimeError(
            "Firmware verification requires every test: skips are failures"
        )
    if any(
        case.find("failure") is not None or case.find("error") is not None
        for case in cases
    ):
        raise RuntimeError("Firmware verification report contains failed tests")
    return len(cases)


def require_iram(link_map: Path) -> None:
    """The RMT encoder runs from the TX ISR while OTA flash writes disable
    the cache: its code must be linked into internal IRAM."""
    addresses = {}
    for line in link_map.read_text().splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0].startswith("0x") and fields[1] in ISR_SYMBOLS:
            addresses[fields[1]] = int(fields[0], 16)
    for symbol in ISR_SYMBOLS:
        if symbol not in addresses:
            raise RuntimeError(f"{symbol} missing from the link map")
        if addresses[symbol] not in IRAM:
            raise RuntimeError(f"{symbol} is not linked into IRAM")


def verify(env: dict[str, str], python: Path, jobs: int) -> None:
    print(
        "[firmware] Building pinned host mbedTLS and running every firmware test",
        flush=True,
    )
    host = SCRATCH / "host-mbedtls"
    run(
        [
            "cmake",
            "-S",
            str(IDF / "components/mbedtls/mbedtls"),
            "-B",
            str(host),
            "-DENABLE_TESTING=OFF",
            "-DENABLE_PROGRAMS=OFF",
        ],
        env,
    )
    run(["cmake", "--build", str(host), "--parallel", str(jobs)], env)
    report = SCRATCH / "verify/junit.xml"
    report.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            str(python),
            "-m",
            "pytest",
            "tests/firmware_ota",
            "tests/firmware_ir",
            "-ra",
            f"--basetemp={SCRATCH / 'verify-pytest'}",
            f"--junitxml={report}",
        ],
        env,
    )
    count = require_complete_report(report)
    print(
        f"[firmware] {count} tests passed with no skips; compiling ESP32-S3", flush=True
    )
    build = SCRATCH / "build"
    # ESP-IDF applies sdkconfig.defaults only to symbols missing from an
    # existing sdkconfig; regenerate it so the committed defaults are built.
    (SCRATCH / "sdkconfig").unlink(missing_ok=True)
    run(
        [
            "bash",
            "-c",
            'set -e; source "$1"; shift; exec idf.py "$@"',
            "yoke-firmware",
            str(IDF / "export.sh"),
            "-C",
            str(ROOT / "firmware/esp32"),
            "-B",
            str(build),
            "-D",
            f"SDKCONFIG={SCRATCH / 'sdkconfig'}",
            "build",
        ],
        env,
    )
    commands = json.loads((build / "compile_commands.json").read_text())
    tls = [
        Path(command["file"]).resolve()
        for command in commands
        if Path(command["file"]).name in ("esp_tls_mbedtls.c", "yoke_esp_tls_mbedtls.c")
    ]
    if tls != [build / "yoke_esp_tls_mbedtls.c"]:
        raise RuntimeError(
            "Target must compile exactly the generated SDK deadline adapter"
        )
    check_sdk(env)
    require_iram(build / "yoke-ir-updater.map")
    image = (build / "yoke-ir-updater.bin").read_bytes()
    if not 288 <= len(image) <= 6 * 1024 * 1024:
        raise RuntimeError("App image does not fit the dual-OTA partition contract")
    print(f"[firmware] PASS: {count} tests, no skips; target app {len(image)} bytes")
    print(f"[firmware] app SHA-256 {hashlib.sha256(image).hexdigest()}")
    print("[firmware] Hardware acceptance NOT RUN; no device contacted or flashed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--offline", action="store_true", help="reuse tools; no downloads/install"
    )
    parser.add_argument(
        "--test-python", type=Path, help="reuse existing pinned dev Python unchanged"
    )
    parser.add_argument("--jobs", type=int, default=min(os.cpu_count() or 2, 8))
    args = parser.parse_args()
    if args.jobs <= 0:
        parser.error("--jobs must be positive")
    os.umask(0o077)
    env = environment(args.jobs)
    for tool in ("git", "bash", "cmake", "ninja", "cc", "openssl"):
        if not shutil.which(tool, path=env["PATH"]):
            parser.error(f"required developer tool is unavailable: {tool}")
    try:
        prepare_sdk(env, args.offline)
        python = prepare_test_python(env, args.offline, args.test_python)
        verify(env, python, args.jobs)
    except (
        OSError,
        RuntimeError,
        subprocess.CalledProcessError,
        ET.ParseError,
    ) as error:
        parser.exit(1, f"Firmware verification failed: {error}\n")


if __name__ == "__main__":
    main()
