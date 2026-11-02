"""The sans-I/O media pipeline: chain pure stages, thread units through.

A :class:`Pipeline` owns an ordered list of :class:`Stage`. Pushing a
unit runs it through each stage; a stage may fan out (one frame → many
slices → many packets), hold (jitter buffer), or drop. ``tick`` drives
timers (jitter playout, periodic stats); ``finish`` drains a finite stream.
Nothing here touches sockets or the clock — the shell (``yoke.media.runner``)
and the tests supply time and I/O. The forward output is independent of who reads
the tap events,
which is what makes the pipeline snoop-verifiable without interference.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from yoke.media.types import (
    MediaUnit,
    NetworkReport,
    PipelineOutput,
    StageOutcome,
)


@runtime_checkable
class Stage(Protocol):
    """One pure transform in the pipeline.

    Implementations set ``name`` (used in taps) and implement
    ``process``; ``tick``, ``flush`` and ``on_feedback`` default to no-ops via
    :class:`StageBase`. A stage MUST NOT let its ``emit`` output depend on
    anything but its inputs and internal state — never on whether an
    observer is attached — so observation stays non-interfering.
    """

    name: str

    def process(self, unit: MediaUnit, now: float) -> StageOutcome: ...

    def tick(self, now: float) -> StageOutcome: ...

    def flush(self, now: float) -> StageOutcome: ...

    def on_feedback(self, report: NetworkReport, now: float) -> StageOutcome: ...

    def next_deadline(self) -> float | None: ...


class StageBase:
    """Convenience base giving no-op timer/finalizer/feedback behavior."""

    name: str = "stage"

    def process(self, unit: MediaUnit, now: float) -> StageOutcome:
        raise NotImplementedError

    def tick(self, now: float) -> StageOutcome:
        return StageOutcome()

    def flush(self, now: float) -> StageOutcome:
        return StageOutcome()

    def on_feedback(self, report: NetworkReport, now: float) -> StageOutcome:
        return StageOutcome()

    def next_deadline(self) -> float | None:
        return None


class Pipeline:
    """An ordered chain of stages."""

    def __init__(self, stages: list[Stage]) -> None:
        self._stages = stages
        self._finished = False

    @property
    def stages(self) -> list[Stage]:
        return list(self._stages)

    @property
    def finished(self) -> bool:
        """Whether the pipeline has received its terminal end-of-input signal."""
        return self._finished

    def push(self, unit: MediaUnit, now: float) -> PipelineOutput:
        """Run one unit through every stage, fanning out as stages emit.

        Units are threaded stage-by-stage: stage i's emitted units become
        stage i+1's inputs. Events and feedback from every stage are
        collected. Units still 'in flight' at the last stage are the
        pipeline's emitted output.
        """
        if self._finished:
            raise RuntimeError("cannot push into a finished pipeline")
        out = PipelineOutput()
        frontier = [unit]
        for stage in self._stages:
            if not frontier:
                break
            next_frontier: list[MediaUnit] = []
            for item in frontier:
                outcome = stage.process(item, now)
                out.events.extend(outcome.events)
                out.feedback.extend(outcome.feedback)
                next_frontier.extend(outcome.emit)
            frontier = next_frontier
        out.emitted.extend(frontier)
        return out

    def tick(self, now: float) -> PipelineOutput:
        """Advance timers. Each stage's ``tick`` may emit held units
        (e.g. the jitter buffer releasing a frame at its playout time);
        those emitted units are threaded through the *subsequent* stages.
        """
        out = PipelineOutput()
        if self._finished:
            return out
        for index, stage in enumerate(self._stages):
            outcome = stage.tick(now)
            out.events.extend(outcome.events)
            out.feedback.extend(outcome.feedback)
            frontier = outcome.emit
            if not frontier:
                continue
            for downstream in self._stages[index + 1 :]:
                if not frontier:
                    break
                next_frontier: list[MediaUnit] = []
                for item in frontier:
                    dout = downstream.process(item, now)
                    out.events.extend(dout.events)
                    out.feedback.extend(dout.feedback)
                    next_frontier.extend(dout.emit)
                frontier = next_frontier
            out.emitted.extend(frontier)
        return out

    def feed_back(self, report: NetworkReport, now: float) -> PipelineOutput:
        """Deliver a receiver report to every stage that consumes feedback
        (the rate controller). Emitted units are threaded downstream."""
        out = PipelineOutput()
        if self._finished:
            return out
        for index, stage in enumerate(self._stages):
            outcome = stage.on_feedback(report, now)
            out.events.extend(outcome.events)
            out.feedback.extend(outcome.feedback)
            frontier = outcome.emit
            if not frontier:
                continue
            for downstream in self._stages[index + 1 :]:
                if not frontier:
                    break
                next_frontier: list[MediaUnit] = []
                for item in frontier:
                    dout = downstream.process(item, now)
                    out.events.extend(dout.events)
                    out.feedback.extend(dout.feedback)
                    next_frontier.extend(dout.emit)
                frontier = next_frontier
            out.emitted.extend(frontier)
        return out

    def finish(self, now: float) -> PipelineOutput:
        """End input and drain every stage once, in pipeline order.

        A stage's flush emissions pass through downstream ``process``
        methods before those stages are themselves flushed. All events and
        feedback use the caller's actual finalization time. Subsequent
        finish/tick/feedback calls are empty; further input is rejected.
        """
        out = PipelineOutput()
        if self._finished:
            return out
        self._finished = True
        for index, stage in enumerate(self._stages):
            outcome = stage.flush(now)
            out.events.extend(outcome.events)
            out.feedback.extend(outcome.feedback)
            frontier = outcome.emit
            if not frontier:
                continue
            for downstream in self._stages[index + 1 :]:
                if not frontier:
                    break
                next_frontier: list[MediaUnit] = []
                for item in frontier:
                    result = downstream.process(item, now)
                    out.events.extend(result.events)
                    out.feedback.extend(result.feedback)
                    next_frontier.extend(result.emit)
                frontier = next_frontier
            out.emitted.extend(frontier)
        return out

    def next_deadline(self) -> float | None:
        if self._finished:
            return None
        deadlines = [d for s in self._stages if (d := s.next_deadline()) is not None]
        return min(deadlines) if deadlines else None
