"""Self-tests for the media network simulator, using a trivial identity
stage so the harness is verified before real stages depend on it."""

from __future__ import annotations

from tests.media_simnet import FaultPolicy, MediaLink
from yoke.media.pipeline import Pipeline, StageBase
from yoke.media.types import (
    EventKind,
    MediaUnit,
    StageEvent,
    StageOutcome,
    UnitKind,
    digest_of,
)


class IdentityStage(StageBase):
    name = "identity"

    def process(self, unit: MediaUnit, now: float) -> StageOutcome:
        return StageOutcome(
            emit=[unit],
            events=[StageEvent(self.name, EventKind.OUT, unit.seq, unit.pts, now)],
        )


def packet(seq: int) -> MediaUnit:
    payload = seq.to_bytes(2, "big") + b"body"
    return MediaUnit(UnitKind.PACKET, seq, seq * 100, payload, digest_of(payload))


def make_link(**kw: object) -> MediaLink:
    sender = Pipeline([IdentityStage()])
    receiver = Pipeline([IdentityStage()])
    return MediaLink(sender, receiver, **kw)  # type: ignore[arg-type]


def test_clean_link_delivers_all_in_order() -> None:
    link = make_link(seed=1, policy=FaultPolicy(delay=0.001))
    for i in range(10):
        link.send([packet(i)])
    link.advance(0.1)
    seqs = [u.seq for u in link.delivered]
    assert seqs == list(range(10))
    assert link.dropped == []


def test_drop_removes_units_deterministically() -> None:
    link = make_link(seed=3, policy=FaultPolicy(drop=0.5, delay=0.001))
    for i in range(200):
        link.send([packet(i)])
    link.advance(1.0)
    assert 0 < len(link.delivered) < 200
    # deterministic for the same seed
    link2 = make_link(seed=3, policy=FaultPolicy(drop=0.5, delay=0.001))
    for i in range(200):
        link2.send([packet(i)])
    link2.advance(1.0)
    assert [u.seq for u in link.delivered] == [u.seq for u in link2.delivered]


def test_duplicate_produces_two_copies() -> None:
    link = make_link(seed=5, policy=FaultPolicy(dup=1.0, delay=0.001))
    link.send([packet(7)])
    link.advance(0.05)
    assert [u.seq for u in link.delivered] == [7, 7]


def test_reorder_can_swap_delivery_order() -> None:
    link = make_link(
        seed=9,
        policy=FaultPolicy(delay=0.001, reorder=1.0, reorder_spike=0.02),
    )
    for i in range(8):
        link.send([packet(i)])
    link.advance(0.2)
    # all delivered, but ordering perturbed relative to send order
    assert sorted(u.seq for u in link.delivered) == list(range(8))


def test_corrupt_flips_a_bit_and_marks_meta() -> None:
    link = make_link(seed=11, policy=FaultPolicy(corrupt=1.0, delay=0.001))
    link.send([packet(4)])
    link.advance(0.05)
    got = link.delivered[0]
    assert got.meta.get("corrupted") is True
    assert got.payload != packet(4).payload


def test_advance_is_monotonic_and_quiesces() -> None:
    link = make_link(seed=2, policy=FaultPolicy(delay=0.003))
    for i in range(5):
        link.send([packet(i)])
    link.advance(0.5)
    assert link.quiescent()
    assert link.now == 0.5
