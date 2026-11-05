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
