"""Closed-loop rate controller: slide the codec target within the band.

Implements the dynamic sliding-scale law from
doc/research/prototype/COMPRESSION.md. The controller consumes receiver
:class:`~yoke.media.types.NetworkReport` feedback and picks a compression
*ratio* for the codec, always inside the visually-lossless band
``[RATIO_MIN, RATIO_MAX]``:

* **Quiet fabric** -> relax toward the transparent resting point
  (``RATIO_TRANSPARENT``): the least compression that still funds FEC
  headroom. We never relax to bit-exact (uncompressed has zero loss
  resilience), so the quiet floor is the transparent ratio, not
  ``RATIO_MIN``.
* **Congestion / loss / jitter rises** -> slide *up* toward
  ``RATIO_MAX``, shrinking the stream to ride out contention — still
  line-based, still sub-ms, still visually lossless.
* **Never exceed ``RATIO_MAX``** — that is the frame-based cliff, which
  the transparent link must never touch.

The instantaneous target is smoothed with an EMA so a single noisy report
does not make the dial oscillate; the hard clamp is applied last.

Coupling to the codec
----------------------
The controller and :class:`~yoke.media.stages.codec.CodecEncodeStage`
share a small mutable :class:`RateState` (chosen over meta-stamping: the
feedback loop is then independent of the unit flow — the controller writes
the state whenever a report arrives, the encode stage reads it whenever it
encodes, with no ordering coupling between the two). The controller also
stamps ``meta["target_ratio"]`` on units it passes through, so a pipeline
that prefers the meta path works too.
"""

from __future__ import annotations

from dataclasses import dataclass

from yoke.media.pipeline import StageBase
from yoke.media.types import (
    RATIO_MAX,
    RATIO_MIN,
    RATIO_TRANSPARENT,
    EventKind,
    MediaUnit,
    NetworkReport,
    StageEvent,
    StageOutcome,
)

# EMA smoothing factor for the target (0 = frozen, 1 = no smoothing). A
# third weight on each new observation damps oscillation while still
# converging within a handful of sustained reports.
EMA_ALPHA = 0.3
# Emit an ADJUST tap only when the target moves at least this much, so
# sub-perceptual EMA settling steps do not spam observers.
ADJUST_EPS = 0.05
# Congestion dominates; loss and jitter refine it. Weights sum to 1 so the
# pressure signal lands in [0, 1].
W_CONGESTION = 0.5
W_LOSS = 0.35
W_JITTER = 0.15
# Normalisers: the observation that counts as "fully saturated" pressure.
LOSS_FULL = 0.10  # 10% packet loss == full loss pressure
JITTER_FULL = 0.03  # 30 ms delay variation == full jitter pressure


@dataclass
class RateState:
    """Mutable target shared between the controller and the encode stage.

    The rate controller writes :attr:`target`; the codec encode stage
    reads it. It is the only shared mutable state in the lane and is pure
    data (sans-I/O, trivially seedable in tests)."""

    target: float = RATIO_TRANSPARENT


def _clamp(ratio: float) -> float:
    return min(RATIO_MAX, max(RATIO_MIN, ratio))


class RateController(StageBase):
    """Slides the codec target within the visually-lossless band."""

    name = "ratecontrol"

    def __init__(self, state: RateState | None = None) -> None:
        self._state = state
        self._target = RATIO_TRANSPARENT
        self._ema = RATIO_TRANSPARENT
        if state is not None:
            state.target = self._target

    def target_ratio(self) -> float:
        return self._target

    @staticmethod
    def _pressure(report: NetworkReport) -> float:
        """Congestion/loss/jitter -> a single pressure signal in [0, 1]."""
        congestion = min(1.0, max(0.0, report.congestion))
        loss = min(1.0, max(0.0, report.loss) / LOSS_FULL)
        jitter = min(1.0, max(0.0, report.jitter) / JITTER_FULL)
        pressure = W_CONGESTION * congestion + W_LOSS * loss + W_JITTER * jitter
        return min(1.0, pressure)

    def on_feedback(self, report: NetworkReport, now: float) -> StageOutcome:
        pressure = self._pressure(report)
        # Quiet -> transparent resting point; full pressure -> RATIO_MAX.
        instant = RATIO_TRANSPARENT + pressure * (RATIO_MAX - RATIO_TRANSPARENT)
        self._ema = EMA_ALPHA * instant + (1.0 - EMA_ALPHA) * self._ema
        new_target = _clamp(self._ema)
        prev = self._target
        self._target = new_target
        if self._state is not None:
            self._state.target = new_target
        events: list[StageEvent] = []
        if abs(new_target - prev) >= ADJUST_EPS:
            events.append(
                StageEvent(
                    stage=self.name,
                    kind=EventKind.ADJUST,
                    seq=0,
                    pts=0,
                    now=now,
                    detail={
                        "target": new_target,
                        "prev": prev,
                        "pressure": pressure,
                        "loss": report.loss,
                        "congestion": report.congestion,
                        "jitter": report.jitter,
                    },
                )
            )
        return StageOutcome(events=events)

    def process(self, unit: MediaUnit, now: float) -> StageOutcome:
        """Pass the unit through unchanged, stamping the current target."""
        stamped = unit.with_payload(unit.payload, target_ratio=self._target)
        return StageOutcome(emit=[stamped])
