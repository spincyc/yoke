"""USB box host tests: the TinyUSB backend's descriptor rules and state
machine, both self-tests and the role configuration, plus the build
configuration the bench depends on. See doc/design/USB-TIER1.md for what
these cannot prove (DWC2 timing, PHY, VBUS, cables).
"""

from __future__ import annotations

import csv
import json
import re
import subprocess
from itertools import pairwise
from pathlib import Path

import pytest
from usb_build import ROOT, build_all

APP = ROOT / "firmware/esp32-usb"


@pytest.fixture(scope="module")
def usb_programs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    return build_all(tmp_path_factory.mktemp("firmware-usb"))


def run(program: Path, mode: str) -> None:
    result = subprocess.run([str(program), mode], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "mode",
    [
        "enumerate",  # FIFOs per address, alternate 0 live, SET_CONFIGURATION relayed
        "deferred-in",  # IN reply held off (NAK) until the sink answers
        "deferred-status",  # no-data status held off until acknowledged
        "out-data",  # OUT data, then TinyUSB's own status stage
        "superseded",  # a late reply never answers a newer request
        "abandoned",  # an armed reply the host gave up on ends INTERRUPTED
        "timeout",  # unanswered request stalls, re-arming SETUP reception
        "echo",  # bulk OUT -> IN through the self-test
        "reset-order",  # SHUTDOWN completions before the RESET event; stale drops
        "chunks",  # long transfers in whole packets per DWC2 packet counter; ZLPs
        "cancel",  # abort fence: INTERRUPTED, or the real result if it won
        "halt",  # host CLEAR_FEATURE(HALT) ends a started operation
        "control-acked",  # SET_CONFIGURATION delivered acked; post-reset stall
        "contract",  # misuse is reported, never acted on
        "high-speed",  # ESP32-P4 limits with 512-byte bulk packets
    ],
)
def test_device_backend(usb_programs: dict[str, Path], mode: str) -> None:
    run(usb_programs["host-usbd"], mode)


@pytest.mark.parametrize(
    "mode",
    [
        "keyboard",
        "alternates",
        "refusals",
        "speeds-and-in-numbers",  # S3 IN 1..4, P4 IN 1..7; P4 presents high speed only
        "other-speed",  # qualifier and other-speed buffers of a high-speed port
        "strings",
        "lines",
    ],
)
def test_descriptor_rules(usb_programs: dict[str, Path], mode: str) -> None:
    run(usb_programs["host-plan"], mode)


@pytest.mark.parametrize(
    "mode",
    [
        "config",  # YWP/1 identity, PSK >= 32 bytes, explicit open mode, name rule
        "auth-limit",  # per-source throttle of authentication failures
        "clock-rules",  # admin step cap; sync kept across software resets only
        "media",  # producer media out, consumer media in (IPv4 only)
        "pump",  # socket <-> core transport, close and refusal
        "pump-backpressure",  # a peer that stops reading; a full core is not watched
    ],
)
def test_box_config_media_and_transport(
    usb_programs: dict[str, Path], mode: str
) -> None:
    run(usb_programs["host-box"], mode)


@pytest.mark.parametrize(
    "mode",
    [
        "enumerate",
        "set-config-race",
        "bulk-in",
        "bulk-out",
        "report",
        "no-ram",
        "parked-reads",  # 16 parked reads; control and unlinks still served
    ],
)
def test_bridge_data_path(usb_programs: dict[str, Path], mode: str) -> None:
    """Both boxes' cores and glue over TCP loopback (host_bridge.c): import,
    descriptor fetch, presentation, SET_CONFIGURATION (with a class request
    right behind it), a relayed vendor STALL, bulk both ways, interrupt
    reports in order, and a box whose RAM cannot hold the endpoints refusing
    the configuration cleanly."""
    result = subprocess.run(
        [str(usb_programs["host-bridge"]), mode, "1"], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert all(value > 0 for value in json.loads(result.stdout).values())


@pytest.mark.parametrize("mode", ["keyboard", "quiet-device"])
def test_host_selftest(usb_programs: dict[str, Path], mode: str) -> None:
    run(usb_programs["host-selftest-host"], mode)


@pytest.mark.parametrize(
    "mode",
    [
        "release-after-completion",  # completions after the library's own bookkeeping
        "clear-halt",  # CLEAR_FEATURE(HALT) leaves fresh pipes (DATA0)
        "unplug-replug",  # freed after the dead URB; a quick replug attaches
    ],
)
def test_usb_host_backend(usb_programs: dict[str, Path], mode: str) -> None:
    """usbh_esp.c over a model of the ESP-IDF USB Host Library."""
    run(usb_programs["host-usbh"], mode)


@pytest.mark.parametrize(
    "mode",
    [
        "start-order",  # no socket before the box's first address
        "clock-gate",  # the YWP/1 node and a rebase wait for the wall clock
        "socket-reopen",  # a failed control socket is retried; the node runs
        "receive-bound",  # a forged trickle is parsed; a flood: batches, then throttled
        "watch-wakes",  # the network task wakes only when a watch changes
    ],
)
def test_bridge_shell(usb_programs: dict[str, Path], mode: str) -> None:
    """box_ywp.c, box_net.c and box_device.c over host stand-ins (idf/)."""
    run(usb_programs["host-shell"], mode)


def partitions(path: Path) -> dict[str, tuple[int, int]]:
    with path.open() as stream:
        rows = list(csv.reader(line for line in stream if line[0] != "#"))
    return {row[0].strip(): (int(row[3], 0), int(row[4], 0)) for row in rows if row}


def test_commissioning_geometry_matches_the_ir_endpoint() -> None:
    """The trust image, NVS and both 6 MiB OTA slots sit where the IR
    endpoint has them, so commissioning (provision.py, write_flash 0x19000)
    is identical; the table fills the 16 MiB flash without overlap."""
    usb = partitions(APP / "partitions.csv")
    ir = partitions(ROOT / "firmware/esp32/partitions.csv")
    for name in (
        "nvs",
        "otatrust",
        "otadata",
        "phy_init",
        "ota_0",
        "ota_1",
        "coredump",
    ):
        assert usb[name] == ir[name]
    spans = sorted(usb.values())
    assert all(a + b <= c for (a, b), (c, _) in pairwise(spans))
    assert spans[-1][0] + spans[-1][1] == 16 * 1024 * 1024


def test_build_configuration() -> None:
    """UART console (the OTG controller owns the native USB PHY), watchdog
    panics, rollback, no RTC heap for DMA, its own project name (an IR
    image cannot be uploaded to a USB box) and esp_tinyusb pinned."""
    config = (APP / "sdkconfig.defaults").read_text().splitlines()
    for line in (
        "CONFIG_ESP_CONSOLE_UART_DEFAULT=y",
        "CONFIG_ESP_CONSOLE_SECONDARY_NONE=y",
        "CONFIG_ESP_TASK_WDT_PANIC=y",
        "CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y",
        "# CONFIG_ESP_SYSTEM_ALLOW_RTC_FAST_MEM_AS_HEAP is not set",
        "# CONFIG_ESP_TLS_SERVER_MIN_AUTH_MODE_OPTIONAL is not set",
    ):
        assert line in config
    assert not any(line.startswith("CONFIG_IDF_TARGET") for line in config)
    project = (APP / "CMakeLists.txt").read_text()
    assert "project(yoke-usb-box)" in project
    assert "yoke_ota_tls_deadline_override()" in project
    manifest = (APP / "components/yoke_usbd/idf_component.yml").read_text()
    assert re.search(r'espressif/esp_tinyusb: "==3\.0\.0"', manifest)
    for target in ("esp32s3", "esp32p4"):
        lock = (APP / f"dependencies.lock.{target}").read_text()
        assert "version: 3.0.0" in lock and f"target: {target}" in lock
        assert "component_hash: 0ff29f6113c4922691b3d7eee2f29af5" in lock
