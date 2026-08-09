"""Deterministic media-network simulator for offline pipeline tests.

Carries a sender pipeline's emitted units to a receiver pipeline through a
virtual link that can drop, duplicate, reorder, delay, and corrupt — all
from a single seed, with a virtual clock. This is the media-plane analogue
of ``tests/simnet.py`` for the control plane: it lets FEC recovery, jitter
buffering, and rate adaptation be reproduced exactly with no sockets.

Not a test module itself (no ``test_`` prefix); imported by the media
integration tests.
"""

from __future__ import annotations

import heapq
import itertools
from collections.abc import Callable
from dataclasses import dataclass, field
from random import Random

from yoke.media.pipeline import Pipeline
from yoke.media.types import (
    MediaUnit,
    NetworkReport,
    StageEvent,
)


@dataclass(frozen=True)
class FaultPolicy:
    """Per-link fault probabilities and timing, applied per unit.

    ``drop``/``dup``/``corrupt`` are probabilities in [0, 1]. ``delay`` is
    the base one-way delay (s); ``jitter`` adds uniform noise in
    ``[-jitter, +jitter]`` (clamped ≥ 0). ``reorder`` is the probability a
    unit gets an extra delay spike (moving it behind later units)."""

    drop: float = 0.0
    dup: float = 0.0
    corrupt: float = 0.0
    delay: float = 0.0005
    jitter: float = 0.0
    reorder: float = 0.0
    reorder_spike: float = 0.01


@dataclass(order=True)
class _Scheduled:
    at: float
    order: int
    unit: MediaUnit = field(compare=False)


class MediaLink:
    """A one-way lossy link from a sender pipeline to a receiver pipeline.

    Feed the sender's emitted units in with :meth:`send`; the link applies
    faults and schedules delivery on a virtual clock. :meth:`advance` runs
    the receiver up to a time, delivering due units and ticking both
    pipelines. Feedback reports the receiver produces are collected and can
    be delivered back to the sender via :meth:`deliver_feedback`.
    """

    def __init__(
        self,
        sender: Pipeline,
        receiver: Pipeline,
        *,
        seed: int = 0,
        policy: FaultPolicy | None = None,
    ) -> None:
        self._sender = sender
        self._receiver = receiver
        self._rng = Random(seed)
        self._policy = policy or FaultPolicy()
        self._queue: list[_Scheduled] = []
        self._counter = itertools.count()
        self.now = 0.0
        self.delivered: list[MediaUnit] = []
        self.dropped: list[MediaUnit] = []
        self.events: list[StageEvent] = []
        self.feedback: list[NetworkReport] = []

    def set_policy(self, policy: FaultPolicy) -> None:
        self._policy = policy

    def _corrupt(self, unit: MediaUnit) -> MediaUnit:
        if not unit.payload:
            return unit.with_payload(b"\x00", corrupted=True)
        data = bytearray(unit.payload)
        index = self._rng.randrange(len(data))
        data[index] ^= 1 << self._rng.randrange(8)
        return unit.with_payload(bytes(data), corrupted=True)

    def _schedule(self, unit: MediaUnit) -> None:
        p = self._policy
        if self._rng.random() < p.drop:
            self.dropped.append(unit)
            return
        copies = 2 if self._rng.random() < p.dup else 1
        for _ in range(copies):
            delay = p.delay
            if p.jitter:
                delay += self._rng.uniform(-p.jitter, p.jitter)
            if self._rng.random() < p.reorder:
                delay += p.reorder_spike
            delay = max(delay, 0.0)
            out = self._corrupt(unit) if self._rng.random() < p.corrupt else unit
            heapq.heappush(
                self._queue,
                _Scheduled(self.now + delay, next(self._counter), out),
            )

    def send(self, units: list[MediaUnit]) -> None:
        """Enqueue units the sender emitted onto the wire (with faults)."""
        for unit in units:
            self._schedule(unit)

    def deliver_feedback(self, report: NetworkReport) -> None:
        """Deliver a receiver report back to the sender's rate controller."""
        out = self._sender.feed_back(report, self.now)
        self.events.extend(out.events)

    def advance(self, until: float, *, tick_interval: float = 0.005) -> None:
        """Run the virtual clock to ``until``, delivering due units to the
        receiver and ticking both pipelines on a fixed cadence."""
        next_tick = self.now
        while True:
            due = self._queue[0].at if self._queue else None
            candidates = [t for t in (due, next_tick) if t is not None and t <= until]
            if not candidates:
                break
            step = min(candidates)
            self.now = step
            if due is not None and step == due:
                item = heapq.heappop(self._queue)
                out = self._receiver.push(item.unit, self.now)
                self.delivered.extend(out.emitted)
                self.events.extend(out.events)
                self.feedback.extend(out.feedback)
            if step == next_tick:
                self._tick_both()
                next_tick = step + tick_interval
        self.now = until

    def _tick_both(self) -> None:
        rout = self._receiver.tick(self.now)
        self.delivered.extend(rout.emitted)
        self.events.extend(rout.events)
        self.feedback.extend(rout.feedback)

    def quiescent(self) -> bool:
        return not self._queue


DriveFn = Callable[[float], list[MediaUnit]]
