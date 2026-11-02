"""The benchmark exposes reproducible results without timing-based gates."""

from __future__ import annotations

import json
from pathlib import Path
from runpy import run_path

import pytest


@pytest.mark.parametrize("workload", ["source", "stages", "pipeline", "jitter"])
def test_benchmark_json_is_reproducible(
    workload: str, capsys: pytest.CaptureFixture[str]
) -> None:
    benchmark = run_path(str(Path(__file__).parents[1] / "benchmarks" / "media.py"))
    args = ["--frames", "4", "--repeat", "1", "--workload", workload, "--memory"]
    assert benchmark["main"](args) == 0
    first = json.loads(capsys.readouterr().out)
    assert benchmark["main"](args) == 0
    second = json.loads(capsys.readouterr().out)
    assert first["schema_version"] == 1
    assert first["environment"]["python"]
    assert first["workload"]["frames"] == 4
    assert first["result"] == second["result"]
    assert len(first["result"]["fingerprint"]) == 64
    assert len(first["samples"]) == 1
    assert isinstance(first["tracemalloc_peak_bytes"], int)
    if workload in ("stages", "pipeline"):
        assert first["result"]["sink"]["frames_ok"] == 4
        assert first["result"]["sink"]["frames_bad"] == 0
    if workload == "stages":
        assert first["result"]["sink"]["mean_latency"] > 0.0
    else:
        assert first["result"]["sink"]["last_latency"] is None
        assert first["result"]["sink"]["mean_latency"] is None
