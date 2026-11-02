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
    verifier = runpy.run_path(str(ROOT / "firmware/esp32/verify.py"))
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
