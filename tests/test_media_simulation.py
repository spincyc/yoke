"""Virtual media timing must progress or fail within an explicit budget."""

from __future__ import annotations

import pytest

from yoke.media.cli import main
from yoke.media.pipeline import Pipeline, StageBase
from yoke.media.simulation import Faults, SimLink


def _link(*, step_budget: int = 100000, receiver: Pipeline | None = None) -> SimLink:
    return SimLink(
        Pipeline([]),
        receiver or Pipeline([]),
        seed=0,
        faults=Faults(),
        step_budget=step_budget,
    )


@pytest.mark.parametrize(
    "interval", [0, -1, float("nan"), float("inf"), float("-inf"), 1e-100, 2]
)
def test_advance_rejects_unsupported_tick_interval(interval: float) -> None:
    link = _link()
    with pytest.raises(ValueError, match=r"tick interval.*\[1e-06, 1\]"):
        link.advance(1, tick_interval=interval)
    assert link.now == 0


@pytest.mark.parametrize("until", [-1, 3601, 1e16, float("nan"), float("inf")])
def test_advance_rejects_unsupported_horizon(until: float) -> None:
    with pytest.raises(ValueError, match=r"advance time.*\[0, 3600\]"):
        _link().advance(until)


def test_large_current_time_cannot_stall_float_tick_progress() -> None:
    link = _link()
    link.now = 1e16
    with pytest.raises(ValueError, match=r"current time.*\[0, 3600\]"):
        link.advance(1e16)
    with pytest.raises(ValueError, match="current time"):
        link.drain()


def test_virtual_time_cannot_run_backwards() -> None:
    link = _link()
    link.advance(0.5)
    with pytest.raises(ValueError, match="must not precede"):
        link.advance(0.4)


def test_small_tick_is_bounded_by_cumulative_work_budget() -> None:
    link = _link(step_budget=16)
    with pytest.raises(ValueError, match=r"work budget exhausted \(16 steps\)"):
        link.advance(1, tick_interval=0.000001)
    assert link.now < 0.0001
    # Calling again cannot evade the cumulative budget.
    with pytest.raises(ValueError, match="work budget exhausted"):
        link.advance(1)


def test_nonprogressing_receiver_deadline_is_bounded() -> None:
    class StalledStage(StageBase):
        name = "stalled"

        def next_deadline(self) -> float:
            return 0.0

    link = _link(step_budget=8, receiver=Pipeline([StalledStage()]))
    with pytest.raises(ValueError, match="work budget exhausted"):
        link.drain()


@pytest.mark.parametrize(
    "kwargs", [{"jitter": 1e100}, {"delay": 61}, {"reorder_spike": float("inf")}]
)
def test_unsupported_fault_delay_is_rejected(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError, match=r"\[0, 60\]"):
        Faults(**kwargs)


def test_huge_cli_jitter_fails_before_simulation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("unsupported jitter reached simulation")

    monkeypatch.setattr("yoke.media.cli.drive", forbidden)
    assert (
        main(
            [
                "simulate",
                "--frames",
                "1",
                "--width",
                "32",
                "--height",
                "32",
                "--jitter",
                "1e100",
            ]
        )
        == 2
    )
    assert "jitter must be finite and in [0, 60] seconds" in capsys.readouterr().err
