"""The dedicated CI gate cannot succeed through skipped/partial tests."""

import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "outcome", ["complete", "skipped", "failure", "error", "partial"]
)
def test_verification_requires_complete_success(tmp_path: Path, outcome: str) -> None:
    # Loading this explicit developer command has no install/network side effects.
    verifier = runpy.run_path(str(ROOT / "firmware/verify.py"))
    count = verifier["MINIMUM_TESTS"]
    case = '<testcase name="check"/>'
    cases = case * count
    if outcome in {"skipped", "failure", "error"}:
        cases = case * (count - 1) + f'<testcase name="bad"><{outcome}/></testcase>'
    elif outcome == "partial":
        cases = case * (count - 1)
    report = tmp_path / "report.xml"
    report.write_text(f"<testsuites><testsuite>{cases}</testsuite></testsuites>")
    if outcome == "complete":
        assert verifier["require_complete_report"](report) == count
    else:
        with pytest.raises(RuntimeError):
            verifier["require_complete_report"](report)


@pytest.mark.parametrize(
    ("address", "ok"), [("0x4038523c", True), ("0x42001234", False), (None, False)]
)
def test_isr_encoder_must_link_into_iram(
    tmp_path: Path, address: str | None, ok: bool
) -> None:
    """Finding 8: the RMT encoder must run while flash writes disable cache."""
    verifier = runpy.run_path(str(ROOT / "firmware/verify.py"))
    lines = []
    for symbol in verifier["ISR_SYMBOLS"]:
        if address is not None:
            lines += [
                f" .text.{symbol}",
                f"                {address}       {symbol}",
            ]
    link_map = tmp_path / "app.map"
    link_map.write_text("\n".join(lines) + "\n")
    if ok:
        verifier["require_iram"](link_map)
    else:
        with pytest.raises(RuntimeError):
            verifier["require_iram"](link_map)
