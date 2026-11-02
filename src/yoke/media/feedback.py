"""Pure report adaptation at the receiver's pipeline/transport boundary."""

from __future__ import annotations

from dataclasses import replace

from yoke.media.pipeline import Pipeline
from yoke.media.stages.jitter import JitterBuffer
from yoke.media.types import NetworkReport


def receiver_report(report: NetworkReport, pipeline: Pipeline) -> NetworkReport:
    """Use packet inter-arrival variation, independent of source clock offset.

    The sink can measure residual frame gaps without capture timestamps.
    Packet jitter is already measured by the canonical jitter stage, so it
    must not depend on the sink's optional source-to-sink latency samples.
    Custom pipelines without that stage retain their own report estimate.
    """
    for stage in pipeline.stages:
        if isinstance(stage, JitterBuffer):
            return replace(report, jitter=stage.jitter_estimate())
    return report
