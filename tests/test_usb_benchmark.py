"""The USB benchmark runs offline and labels its numbers as simulated."""

from __future__ import annotations

import json
from pathlib import Path
from runpy import run_path

import pytest


def test_quick_benchmark_reports_simulated_evidence(
    capsys: pytest.CaptureFixture[str],
) -> None:
    benchmark = run_path(
        str(Path(__file__).parents[1] / "benchmarks" / "usb_datapath.py")
    )
    assert benchmark["main"](["--quick"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["schema_version"] == 1
    assert (
        "simulated" in document["evidence"] and "not hardware" in document["evidence"]
    )
    scenarios = {(r["scenario"], r["policy"]) for r in document["results"]}
    for name in ("enumeration", "interrupt_in", "bulk_in", "bulk_out", "msc_read"):
        assert (name, "baseline") in scenarios and (name, "optimized") in scenarios
    enumerations = [r for r in document["results"] if r["scenario"] == "enumeration"]
    assert all(r["deadline_violations"] == 0 for r in enumerations)
    optimized = [r for r in enumerations if r["policy"] == "optimized"]
    baseline = [r for r in enumerations if r["policy"] == "baseline"]
    # Structural, not timing: the cache removes forwarded descriptor reads.
    assert max(r["forwarded_requests"] for r in optimized) < min(
        r["forwarded_requests"] for r in baseline
    )
    assert benchmark["main"](["--quick", "--markdown"]) == 0
    assert "| Scenario |" in capsys.readouterr().out
