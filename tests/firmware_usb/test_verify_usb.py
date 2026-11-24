"""The firmware gate refuses a USB box image that would serve esp_tinyusb's
static descriptors instead of the mirrored ones."""

import runpy
from pathlib import Path

import pytest
from usb_build import ROOT


@pytest.mark.parametrize("case", ["mirrored", "unwrapped", "both"])
def test_mirror_callbacks_must_be_linked(tmp_path: Path, case: str) -> None:
    verifier = runpy.run_path(str(ROOT / "firmware/verify.py"))
    lines = []
    for symbol in verifier["MIRRORED"]:
        if case != "unwrapped":
            lines.append(f"                0x42014500                __wrap_{symbol}")
        if case != "mirrored":
            lines.append(f"                0x42014600                {symbol}")
        # Discarded input sections name the originals without an address.
        lines.append(
            f" .text.{symbol}\n                0x00000000       0x1e lib.a(x.o)"
        )
    link_map = tmp_path / "app.map"
    link_map.write_text("\n".join(lines) + "\n")
    if case == "mirrored":
        verifier["require_mirror_callbacks"](link_map)
    else:
        with pytest.raises(RuntimeError):
            verifier["require_mirror_callbacks"](link_map)


def test_verify_builds_s3_device_role_image() -> None:
    """firmware/verify.py must also build-verify the ESP32-S3 device-role image."""
    source = (ROOT / "firmware/verify.py").read_text()
    assert "build-usb-esp32s3-device" in source
    assert "sdkconfig-usb-esp32s3-device" in source
    assert "SDKCONFIG_DEFAULTS=sdkconfig.defaults;sdkconfig.role-device" in source
    assert 'images["USB box (esp32s3 device)"] = require_app(' in source
