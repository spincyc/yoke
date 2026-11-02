"""Deterministic synthetic media link with a delayed feedback return path.

This module owns virtual-network faults, wire boundary taps, and causal
receiver-report delivery. It reads no clock and performs no I/O. The
reference simulator models network behavior, not codec/hardware throughput.
"""

from __future__ import annotations

import heapq
import itertools
import math
from dataclasses import dataclass, field
from random import Random

from yoke.media.feedback import receiver_report
from yoke.media.pipeline import Pipeline
from yoke.media.types import (
    EventKind,
    MediaUnit,
    NetworkReport,
    PipelineOutput,
    StageEvent,
)

LINK_DELAY = 0.0005
MAX_SIM_TIME = 3600.0
MAX_SIM_DELAY = 60.0
MIN_TICK_INTERVAL = 0.000001
MAX_TICK_INTERVAL = 1.0
MAX_SIM_STEPS = 100000


def _bounded_time(value: float, name: str, minimum: float, maximum: float) -> float:
    message = f"{name} must be finite and in [{minimum:g}, {maximum:g}] seconds"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(message)
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError(message) from exc
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise ValueError(message)
    return number


# -- fault script + offline link --------------------------------------------


@dataclass(frozen=True)
class Faults:
    """Per-unit fault probabilities for the offline sim (mirrors the media
    simnet's policy). Probabilities are in ``[0, 1]``; ``jitter`` is the
    magnitude (seconds) of uniform delay noise."""

    drop: float = 0.0
    dup: float = 0.0
    reorder: float = 0.0
    jitter: float = 0.0
    corrupt: float = 0.0
    delay: float = LINK_DELAY
    reorder_spike: float = 0.01

    def __post_init__(self) -> None:
        for name in ("drop", "dup", "reorder", "corrupt"):
            value = getattr(self, name)
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError(f"{name} must be finite and in [0, 1]")
        for name in ("jitter", "delay", "reorder_spike"):
            _bounded_time(getattr(self, name), name, 0, MAX_SIM_DELAY)

    def is_clean(self) -> bool:
        return not (
            self.drop or self.dup or self.reorder or self.jitter or self.corrupt
        )


@dataclass(order=True)
class _Scheduled:
    at: float
    order: int
    unit: MediaUnit = field(compare=False)


@dataclass(order=True)
class _ScheduledFeedback:
    at: float
    order: int
    report: NetworkReport = field(compare=False)


class SimLink:
    """A deterministic, seeded, one-way lossy link between a sender and a
    receiver :class:`~yoke.media.pipeline.Pipeline`, for offline debugging.

    It is the in-package analogue of ``tests/media_simnet.MediaLink`` (the
    CLI is shippable package code and must not import the test tree): feed
    it the sender's emitted units with :meth:`send`, then :meth:`advance`
    the virtual clock to drive the receiver. Every delivered packet is
    retained (:attr:`delivered_packets`) so a black-box snooper can verify
    the *wire* the receiver actually saw.
    """

    def __init__(
        self,
        sender: Pipeline,
        receiver: Pipeline,
        *,
        seed: int,
        faults: Faults,
        feedback_delay: float = LINK_DELAY,
        step_budget: int = MAX_SIM_STEPS,
    ) -> None:
        _bounded_time(feedback_delay, "feedback delay", 0, MAX_SIM_DELAY)
        if type(step_budget) is not int or not 1 <= step_budget <= MAX_SIM_STEPS:
            raise ValueError(f"step budget must be an integer in [1, {MAX_SIM_STEPS}]")
        self._sender = sender
        self._receiver = receiver
        self._rng = Random(seed)
        self._faults = faults
        self._queue: list[_Scheduled] = []
        self._feedback_queue: list[_ScheduledFeedback] = []
        self._feedback_delay = feedback_delay
        self._counter = itertools.count()
        self.now = 0.0
        self.delivered_packets: list[tuple[MediaUnit, float]] = []
        self.dropped: list[MediaUnit] = []
        self.events: list[StageEvent] = []
        self.feedback: list[NetworkReport] = []
        self.feedback_delivered: list[tuple[NetworkReport, float]] = []
        self.sent = 0
        self.duplicates = 0
        self._steps = 0
        self._step_budget = step_budget
        self._receiver_finished = False

    def send(self, units: list[MediaUnit]) -> None:
        _bounded_time(self.now, "current time", 0, MAX_SIM_TIME)
        for unit in units:
            self.sent += 1
            self.events.append(self._wire_event(unit, EventKind.OUT))
            self._schedule(unit)

    def _wire_event(self, unit: MediaUnit, kind: EventKind) -> StageEvent:
        return StageEvent(
            stage="wire",
            kind=kind,
            seq=unit.seq,
            pts=unit.pts,
            now=self.now,
            detail={"unit_kind": unit.kind.value},
        )

    def _schedule(self, unit: MediaUnit) -> None:
        f = self._faults
        if self._rng.random() < f.drop:
            self.dropped.append(unit)
            return
        copies = 2 if self._rng.random() < f.dup else 1
        self.duplicates += copies - 1
        for _ in range(copies):
            delay = f.delay
            if f.jitter:
                delay += self._rng.uniform(-f.jitter, f.jitter)
            if self._rng.random() < f.reorder:
                delay += f.reorder_spike
            out = self._corrupt(unit) if self._rng.random() < f.corrupt else unit
            arrival = _bounded_time(
                self.now + max(delay, 0.0), "packet arrival time", 0, MAX_SIM_TIME
            )
            heapq.heappush(
                self._queue,
                _Scheduled(arrival, next(self._counter), out),
            )

    def _corrupt(self, unit: MediaUnit) -> MediaUnit:
        if not unit.payload:
            return unit.with_payload(b"\x00", corrupted=True)
        data = bytearray(unit.payload)
        index = self._rng.randrange(len(data))
        data[index] ^= 1 << self._rng.randrange(8)
        return unit.with_payload(bytes(data), corrupted=True)

    def advance(self, until: float, *, tick_interval: float = 0.005) -> None:
        _bounded_time(self.now, "current time", 0, MAX_SIM_TIME)
        _bounded_time(until, "advance time", 0, MAX_SIM_TIME)
        _bounded_time(
            tick_interval, "tick interval", MIN_TICK_INTERVAL, MAX_TICK_INTERVAL
        )
        if until < self.now:
            raise ValueError("advance time must not precede current virtual time")
        next_tick = self.now
        while True:
            due = self._queue[0].at if self._queue else None
            report_due = self._feedback_queue[0].at if self._feedback_queue else None
            candidates = [
                t for t in (due, report_due, next_tick) if t is not None and t <= until
            ]
            if not candidates:
                break
            if self._steps >= self._step_budget:
                raise ValueError(
                    f"simulation work budget exhausted ({self._step_budget} steps); "
                    "reduce frames/delays or use a coarser supported API tick interval"
                )
            self._steps += 1
            step = min(candidates)
            if step < self.now:
                raise ValueError("scheduled time precedes current virtual time")
            self.now = step
            if due is not None and step == due:
                item = heapq.heappop(self._queue)
                self.delivered_packets.append((item.unit, self.now))
                self.events.append(self._wire_event(item.unit, EventKind.IN))
                out = self._receiver.push(item.unit, self.now)
                self._record_receiver_output(out)
            if report_due is not None and step == report_due:
                report = heapq.heappop(self._feedback_queue).report
                self.feedback_delivered.append((report, self.now))
                out = self._sender.feed_back(report, self.now)
                self.events.extend(out.events)
            if step == next_tick:
                if not self._receiver_finished:
                    rout = self._receiver.tick(self.now)
                    self._record_receiver_output(rout)
                following_tick = step + tick_interval
                if following_tick <= step:
                    raise ValueError(
                        "tick interval cannot advance virtual time at this precision"
                    )
                next_tick = following_tick
        self.now = until

    def _record_receiver_output(self, out: PipelineOutput) -> None:
        self.events.extend(out.events)
        for report in out.feedback:
            report = receiver_report(report, self._receiver)
            self.feedback.append(report)
            arrival = _bounded_time(
                self.now + self._feedback_delay,
                "feedback arrival time",
                0,
                MAX_SIM_TIME,
            )
            heapq.heappush(
                self._feedback_queue,
                _ScheduledFeedback(arrival, next(self._counter), report),
            )

    def quiescent(self) -> bool:
        return not self._queue and not self._feedback_queue

    def drain(self) -> None:
        """Finish scheduled delivery and receiver playout on the virtual clock."""
        _bounded_time(self.now, "current time", 0, MAX_SIM_TIME)
        while True:
            deadlines = [
                self._queue[0].at if self._queue else None,
                self._feedback_queue[0].at if self._feedback_queue else None,
                None if self._receiver_finished else self._receiver.next_deadline(),
            ]
            due = [deadline for deadline in deadlines if deadline is not None]
            if not due:
                return
            self.advance(max(self.now, min(due)))

    def finish_receiver(self) -> None:
        """Finalize only after actual forward deliveries, then return reports."""
        _bounded_time(self.now, "current time", 0, MAX_SIM_TIME)
        if self._queue:
            raise ValueError("drain scheduled packets before finishing the receiver")
        self._receiver_finished = True
        self._record_receiver_output(self._receiver.finish(self.now))
        self.drain()
