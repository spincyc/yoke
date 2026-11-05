"""Build the USB box's pure C modules for the host, unchanged.

Not a conftest: tests/firmware_ir imports its own conftest by name.

Every binary is compiled with -Wall -Wextra -Werror and AddressSanitizer
plus UndefinedBehaviorSanitizer. The native cores come from native/yusb and
native/ywp (YOKE_NATIVE_YUSB and YOKE_NATIVE_YWP override the locations, as
they do for the firmware build). HOST SIMULATION ONLY: no TinyUSB, USB Host
Library, DWC2 controller or cable is involved.

Run as a script, it builds the bridge model without sanitizers for `make
firmware-usb-bench`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMPONENTS = ROOT / "firmware/esp32-usb/components"
HERE = Path(__file__).resolve().parent
NATIVE_YUSB = Path(os.environ.get("YOKE_NATIVE_YUSB", ROOT / "native/yusb"))
NATIVE_YWP = Path(os.environ.get("YOKE_NATIVE_YWP", ROOT / "native/ywp"))
LZ4 = NATIVE_YUSB.parent / "third_party/lz4"
FLAGS = [
    "-std=gnu17",
    "-O1",
    # Match Ubuntu's default hardening so warn_unused_result fires everywhere.
    "-U_FORTIFY_SOURCE",
    "-D_FORTIFY_SOURCE=3",
    "-g",
    "-Wall",
    "-Wextra",
    "-Werror",
    "-Wshadow",
    "-fsanitize=address,undefined",
    "-fno-sanitize-recover=all",
    "-fno-omit-frame-pointer",
]
BENCH_FLAGS = ["-std=gnu17", "-O2", "-Wall", "-Wextra", "-Werror"]
# The firmware's bounds for the cores, from their one source.
BOUNDS = [
    f"-D{line}"
    for line in (ROOT / "firmware/esp32-usb/yusb_bounds.txt").read_text().splitlines()
    if line[:1].isupper()
]
INCLUDES = [
    HERE / "idf",  # ESP-IDF stand-ins for the target glue
    *(
        COMPONENTS / path
        for path in (
            "yoke_usbd",
            "yoke_usbd/include",
            "yoke_usbdesc/include",
            "yoke_usbh/include",
            "yoke_box",
        )
    ),
    NATIVE_YUSB / "include",
    NATIVE_YWP / "include",
    LZ4,
]
USBD = ("yoke_usbd/usbd_core.c", "yoke_usbd/usbd_plan.c", "yoke_usbdesc/usb_desc.c")
PROGRAMS: dict[str, tuple[str, ...]] = {
    "host-usbd": ("host_usbd.c", *USBD, "yoke_box/selftest_cdc.c"),
    "host-plan": ("host_plan.c", "yoke_usbd/usbd_plan.c", "yoke_usbdesc/usb_desc.c"),
    "host-selftest-host": (
        "host_selftest_host.c",
        "yoke_box/selftest_host.c",
        "yoke_usbdesc/usb_desc.c",
    ),
    "host-box": (
        "host_box.c",
        "yoke_box/box_config.c",
        "yoke_box/box_limit.c",
        "yoke_box/box_media.c",
        "yoke_box/box_pump.c",
        "yoke_box/box_time.c",
        "native-usb:src/target.c",
        "native-ywp:src/ywp_json.c",
        "native-ywp:src/ywp_num.c",
        "native-ywp:src/ywp_types.c",
    ),
    "host-usbh": (
        "host_usbh.c",
        "here:idf/idf_stubs.c",
        "yoke_usbh/usbh_esp.c",
        "yoke_usbdesc/usb_desc.c",
    ),
    "host-shell": (
        "host_shell.c",
        "here:idf/idf_stubs.c",
        "yoke_box/box_ywp.c",
        "yoke_box/box_net.c",
        "yoke_box/box_device.c",
        "yoke_box/box_host.c",
        "yoke_box/box_limit.c",
        "yoke_box/box_media.c",
        "yoke_box/box_pump.c",
        "yoke_usbdesc/usb_desc.c",
        "native-usb:src/*.c",
        "native-ywp:src/*.c",
        "lz4:lz4.c",
    ),
    "host-bridge": (
        "host_bridge.c",
        *USBD,
        "yoke_box/box_gadget.c",
        "yoke_box/box_host.c",
        "yoke_box/box_pump.c",
        "native-usb:src/*.c",
        "lz4:lz4.c",
    ),
}


def sources(names: tuple[str, ...]) -> list[Path]:
    roots = {
        "native-usb": NATIVE_YUSB,
        "native-ywp": NATIVE_YWP,
        "lz4": LZ4,
        "here": HERE,
    }
    paths = [HERE / names[0]]
    for name in names[1:]:
        root, _, rest = name.rpartition(":")
        base = roots[root] if root else COMPONENTS
        paths += sorted(base.glob(rest)) if "*" in rest else [base / rest]
    return paths


def build(target: Path, names: tuple[str, ...], flags: list[str] = FLAGS) -> Path:
    compiler = shutil.which("cc")
    if compiler is None:
        pytest.skip("host C compiler unavailable; target build is a separate check")
    if not (NATIVE_YUSB / "include").is_dir() or not (NATIVE_YWP / "include").is_dir():
        pytest.skip("native cores unavailable (set YOKE_NATIVE_YUSB/YOKE_NATIVE_YWP)")
    result = subprocess.run(
        [
            compiler,
            *flags,
            *BOUNDS,
            f"-I{HERE}",
            *(f"-I{path}" for path in INCLUDES),
            *(str(path) for path in sources(names)),
            "-o",
            str(target),
            "-lm",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # Show the compiler's own diagnostics; CalledProcessError hides them.
        raise RuntimeError(f"building {target.name} failed:\n{result.stderr[-8000:]}")
    return target


def build_all(out: Path) -> dict[str, Path]:
    return {name: build(out / name, names) for name, names in PROGRAMS.items()}


if __name__ == "__main__":
    if shutil.which("cc") is None or not (NATIVE_YUSB / "include").is_dir():
        sys.exit("a host C compiler and native/yusb (YOKE_NATIVE_YUSB) are required")
    bench = Path(sys.argv[1])
    bench.parent.mkdir(parents=True, exist_ok=True)
    build(bench, PROGRAMS["host-bridge"], BENCH_FLAGS)
