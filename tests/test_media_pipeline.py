"""Offline end-to-end integration tests for the whole media pipeline.

Drives the canonical MEDIA.md pipeline as a closed loop with zero I/O and a
virtual clock:

    source.frame(now)
      -> sender.pipeline.push  (ratecontrol -> codec -> packetize -> [fec])
        -> MediaLink            (drop/dup/reorder/delay/corrupt on a seed)
          -> receiver.pipeline  ([fec-decode] -> jitter -> depacketize ->
                                 codec-decode -> sink)
            -> IntegritySink metrics + StageEvent taps

Every run is deterministic from a single seed. Health is asserted two ways:
the receiver's own :meth:`IntegritySink.metrics`, and an independent
:class:`~yoke.media.metrics.MetricsCollector` fed only from the collected
``StageEvent`` taps (the white-box observer). The simnet carries
``MediaUnit`` objects in-process (not wire bytes), so it faithfully carries
XOR-parity units that never cross a real wire -- both ``fec="xor"`` and
``fec="dup"`` are exercised here.

Two behaviours surfaced that are documented inline with ``# NOTE:`` and in
the module epilogue rather than worked around; neither is a delivery bug
(the stream reassembles correctly in every scenario).
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass

import pytest

from tests.media_simnet import FaultPolicy, MediaLink
from yoke.media.assembly import (
    MediaConfig,
    Receiver,
    Sender,
    build_receiver,
    build_sender,
)
from yoke.media.metrics import MetricsCollector, StageRoles
from yoke.media.types import (
    RATIO_MAX,
    RATIO_MIN,
    RATIO_TRANSPARENT,
    Body,
    EventKind,
    MediaUnit,
    NetworkReport,
    StageEvent,
)

# Frame cadence for the virtual clock (60 fps). The exact value only sets
# the spacing of capture times; every result is seeded and reproducible.
DT = 1.0 / 60.0
# Extra virtual time to advance past the last frame so the jitter buffer
# releases every still-held packet before we read the metrics.
DRAIN = 0.5

FrameHook = Callable[[int, float, Sender, MediaLink], None]
SendFilter = Callable[[MediaUnit], bool]


@dataclass
class RunResult:
    """Everything a scenario needs to assert on after a full run."""

    metrics: Body  # receiver IntegritySink.metrics()
    events: list[StageEvent]  # every tap (sender push + receiver + feedback)
    snapshot: Body  # MetricsCollector.snapshot() fed from ``events``
    collector: MetricsCollector
    sender: Sender
    receiver: Receiver
    link: MediaLink


def _wire_out_role(config: MediaConfig) -> str:
    """The last sender stage whose OUT marks a packet launched onto the wire.

    With FEC the tail stage is ``fec``; without it, ``packetize``. The
    collector's default :class:`StageRoles` assumes ``fec`` is always present,
    so no-FEC runs must say ``packetize`` or ``packets_sent`` stays zero.
    """
    return "fec" if config.fec in ("xor", "dup") else "packetize"


def run_pipeline(
    config: MediaConfig,
    policy: FaultPolicy,
    frames: int,
    *,
    seed: int = 0,
    on_frame: FrameHook | None = None,
    send_filter: SendFilter | None = None,
    drain: float = DRAIN,
) -> RunResult:
    """Run ``frames`` frames through a matched sender/receiver over the link.

    The source is driven via ``sender.source.frame(now)``; each emitted
    FRAME is pushed through the sender pipeline; the emitted PACKET/PARITY
    units are handed to the :class:`MediaLink`, which delivers them into the
    receiver pipeline and ticks it (releasing the jitter buffer). ``on_frame``
    (if given) runs after each frame is delivered -- used to feed feedback.
    ``send_filter`` selects which emitted units reach the wire (used to drop
    a whole ST 2022-7 path). Returns sink metrics, every event, and a
    :class:`MetricsCollector` snapshot folded from those events.
    """
    sender = build_sender(config)
    receiver = build_receiver(config)
    link = MediaLink(sender.pipeline, receiver.pipeline, seed=seed, policy=policy)
    collector = MetricsCollector(StageRoles(wire_out=_wire_out_role(config)))
    events: list[StageEvent] = []

    for i in range(frames):
        now = i * DT
        frame_out = sender.source.frame(now)
        events.extend(frame_out.events)
        for frame in frame_out.emit:
            push_out = sender.pipeline.push(frame, now)
            events.extend(push_out.events)
            units = push_out.emitted
            if send_filter is not None:
                units = [u for u in units if send_filter(u)]
            link.send(units)
        link.advance(now + DT)
        if on_frame is not None:
            on_frame(i, now, sender, link)

    link.advance(frames * DT + drain)
    # Receiver-side taps (and any feedback ADJUST taps) live on the link; add
    # them once at the end so nothing is double-counted.
    events.extend(link.events)
    for event in events:
        collector.observe(event)

    return RunResult(
        metrics=receiver.sink.metrics(),
        events=events,
        snapshot=collector.snapshot(),
        collector=collector,
        sender=sender,
        receiver=receiver,
        link=link,
    )


# -- event helpers -----------------------------------------------------------


def _count(events: list[StageEvent], *, stage: str, kind: EventKind) -> int:
    return sum(1 for e in events if e.stage == stage and e.kind is kind)


def _recover_count(events: list[StageEvent]) -> int:
    return sum(1 for e in events if e.kind is EventKind.RECOVER)


def _dedup_drops(events: list[StageEvent]) -> int:
    return sum(
        1
        for e in events
        if e.kind is EventKind.DROP and e.detail.get("why") == "duplicate"
    )


def _integrity(events: list[StageEvent]) -> list[StageEvent]:
    return [e for e in events if e.stage == "sink" and e.kind is EventKind.INTEGRITY]


def _codec_frame_bytes(events: list[StageEvent]) -> dict[int, int]:
    """Modelled on-wire ``frame_bytes`` per frame seq, from codec OUT taps."""
    by_seq: dict[int, int] = {}
    for e in events:
        if e.stage == "codec" and e.kind is EventKind.OUT:
            fb = e.detail.get("frame_bytes")
            if isinstance(fb, int) and e.seq not in by_seq:
                by_seq[e.seq] = fb
    return by_seq


def _monotonic(seqs: list[int]) -> bool:
    return all(b >= a for a, b in itertools.pairwise(seqs))


# -- scenario 1: clean link, raw/none -> lossless, bit-exact -----------------


def test_clean_raw_none_is_lossless_and_bit_exact() -> None:
    n = 40
    result = run_pipeline(
        MediaConfig(encoding="raw", fec="none"), FaultPolicy(), n, seed=1
    )
    metrics = result.metrics
    # Every frame delivered (at most one could still be in flight; here the
    # drain is long enough that all N arrive).
    assert metrics["frames_ok"] == n
    assert metrics["frames_bad"] == 0
    assert metrics["lost"] == 0
    assert metrics["duplicated"] == 0
    # Bit-exact: every glass-to-glass integrity check passed on the digest.
    checks = _integrity(result.events)
    assert len(checks) == n
    assert all(e.detail["ok"] is True and e.detail["reason"] == "ok" for e in checks)
    # White-box observer agrees with the sink.
    totals = result.snapshot["totals"]
    assert totals["frames_sourced"] == n
    assert totals["frames_ok"] == n
    assert totals["frames_bad"] == 0
    # NOTE: metrics["mean_latency"] is 0.0 here even though frames plainly
    # took time to arrive: the Packetizer builds a fresh RTP meta dict and
    # does not carry the source's meta["capture_now"] onto PACKET units, so
    # it never reaches the sink. This is by design (capture_now is not an
    # on-wire RTP field). The white-box collector recovers true latency via
    # the source-OUT/pts join, so its stream latency IS populated:
    latency = result.snapshot["streams"]["-"]["latency"]
    assert latency is not None and latency["mean"] > 0.0


# -- scenario 2: loss without FEC -> incomplete frames reported, none faked --


def test_loss_without_fec_reports_loss_and_never_fakes_ok() -> None:
    n = 60
    result = run_pipeline(
        MediaConfig(encoding="raw", fec="none"),
        FaultPolicy(drop=0.05, delay=0.001),
        n,
        seed=1,
    )
    metrics = result.metrics
    # Some frames were lost (a dropped packet leaves a frame unreassembled;
    # it never reaches the sink and shows as a hole in the seq range).
    assert metrics["lost"] > 0
    assert metrics["frames_ok"] < n
    # No corruption on this link, so nothing is verified-but-wrong: a frame
    # that arrives is bit-exact, and an incomplete one is simply absent.
    assert metrics["frames_bad"] == 0
    # The sink stays self-consistent: every arrival is counted exactly once.
    checks = _integrity(result.events)
    assert len(checks) == metrics["frames_ok"] + metrics["frames_bad"]
    assert all(e.detail["ok"] is True for e in checks)  # no bad-but-claimed-ok


# -- scenario 3: XOR FEC demonstrably recovers loss --------------------------


def test_xor_fec_recovers_single_losses_and_beats_no_fec() -> None:
    n = 60
    seed = 7
    policy = FaultPolicy(drop=0.07, delay=0.001)
    with_fec = run_pipeline(
        MediaConfig(encoding="raw", fec="xor", fec_group=8), policy, n, seed=seed
    )
    without_fec = run_pipeline(
        MediaConfig(encoding="raw", fec="none"), policy, n, seed=seed
    )
    recovered = _recover_count(with_fec.events)
    # FEC RECOVER events actually fired (one lost packet per group rebuilt).
    assert recovered > 0
    assert with_fec.collector.stage_report("fec-decode")["recover"] == recovered
    # Recovery demonstrably helps: more frames delivered ok than the no-FEC
    # run at the same seed/drop. (seed=7, drop=0.07: 51 vs 40, recover=12.)
    assert with_fec.metrics["frames_ok"] > without_fec.metrics["frames_ok"]
    # Recovered packets are byte/metadata-identical, so no frame verifies bad.
    assert with_fec.metrics["frames_bad"] == 0


# -- scenario 4: duplicate-path FEC (ST 2022-7) is seamless ------------------


def test_dup_fec_dedups_and_survives_a_full_path_loss() -> None:
    n = 40
    config = MediaConfig(encoding="raw", fec="dup")
    # Both paths healthy: every packet arrives twice; the merger forwards the
    # first copy and dedups the second. Delivery is seamless.
    both = run_pipeline(config, FaultPolicy(delay=0.001), n, seed=1)
    assert both.metrics["frames_ok"] == n
    assert both.metrics["lost"] == 0
    assert both.metrics["frames_bad"] == 0
    # 4 slices/frame -> 4 packets/frame -> 4 deduped duplicates/frame.
    assert _dedup_drops(both.events) == 4 * n

    # A whole path is lost: drop every path-"B" copy before it hits the wire.
    # ST 2022-7 says this loses nothing -- the surviving path carries it all.
    only_a = run_pipeline(
        config,
        FaultPolicy(delay=0.001),
        n,
        seed=1,
        send_filter=lambda u: u.meta.get("path") == "A",
    )
    assert only_a.metrics["frames_ok"] == n
    assert only_a.metrics["lost"] == 0
    assert only_a.metrics["frames_bad"] == 0
    # Only one copy of each packet now arrives, so there is nothing to dedup.
    assert _dedup_drops(only_a.events) == 0


# -- scenario 5: reorder + jitter -> jitter buffer resequences ---------------


def test_reorder_and_jitter_are_resequenced_in_order() -> None:
    n = 50
    result = run_pipeline(
        MediaConfig(encoding="raw", fec="none"),
        FaultPolicy(delay=0.002, jitter=0.003, reorder=0.6, reorder_spike=0.004),
        n,
        seed=4,
    )
    metrics = result.metrics
    # Nothing permanently lost or corrupted despite the shuffled arrivals.
    assert metrics["frames_bad"] == 0
    assert metrics["lost"] == 0
    assert metrics["frames_ok"] == n
    # Prove reordering really happened AND that the buffer fixed it: packets
    # were HELD out of sequence but RELEASED strictly in sequence.
    holds = [
        e.seq for e in result.events if e.stage == "jitter" and e.kind is EventKind.HOLD
    ]
    outs = [
        e.seq for e in result.events if e.stage == "jitter" and e.kind is EventKind.OUT
    ]
    assert not _monotonic(holds)  # arrivals were out of order
    assert _monotonic(outs)  # releases are back in order
    # The sink therefore observes zero reordering downstream of the buffer.
    assert metrics["reordered"] == 0


# -- scenario 6: rate adaptation under rising congestion ---------------------


def test_rate_control_slides_target_up_and_shrinks_frame_bytes() -> None:
    n = 40
    config = MediaConfig(encoding="jpegxs", fec="none")  # RatioCodec

    def ramp(i: int, now: float, sender: Sender, link: MediaLink) -> None:
        # Congestion (and loss/jitter) climb toward saturation over the run.
        c = min(1.0, i / 20.0)
        link.deliver_feedback(
            NetworkReport(now=now, loss=0.10 * c, jitter=0.03 * c, congestion=c)
        )

    result = run_pipeline(config, FaultPolicy(delay=0.001), n, seed=0, on_frame=ramp)
    target = result.sender.rate_state.target
    # The target slid up from the transparent resting point toward the cap,
    # and never crosses RATIO_MAX (the frame-based cliff) nor RATIO_MIN.
    assert target > RATIO_TRANSPARENT
    assert RATIO_MIN <= target <= RATIO_MAX
    # An ADJUST tap fired as the controller moved the dial.
    assert _count(result.events, stage="ratecontrol", kind=EventKind.ADJUST) > 0
    # Modelled on-wire size shrinks as the codec compresses harder.
    frame_bytes = _codec_frame_bytes(result.events)
    assert frame_bytes[n - 1] < frame_bytes[0]


# -- scenario 7: corruption -> digest mismatches flagged bad, never faked ----


def test_corruption_is_detected_as_bad_frames() -> None:
    n = 50
    result = run_pipeline(
        MediaConfig(encoding="raw", fec="none"),
        FaultPolicy(corrupt=0.05, delay=0.001),
        n,
        seed=5,
    )
    metrics = result.metrics
    # Bit flips ride through the in-process meta path into the reassembled
    # frame; the lossless sink catches the digest/seq mismatch and counts the
    # frame bad -- it is never silently accepted as ok. (A flip in the RTP
    # version bits instead drops the packet, losing the frame; both outcomes
    # are honest, neither is a false ok.)
    assert metrics["frames_bad"] > 0
    assert metrics["frames_ok"] < n
    bad_checks = [e for e in _integrity(result.events) if e.detail["ok"] is False]
    assert len(bad_checks) == metrics["frames_bad"]
    assert all(
        e.detail["reason"]
        in {"digest-mismatch", "seq-mismatch", "pts-mismatch", "short-payload"}
        for e in bad_checks
    )
    # No crash, and no corrupt frame was counted ok.
    assert metrics["frames_ok"] + metrics["frames_bad"] == len(
        _integrity(result.events)
    )


# -- scenario 8: seeded property test over several seeds ---------------------


@pytest.mark.parametrize("seed", [0, 1, 2, 3, 4, 5])
def test_property_bounded_loss_xor_stays_consistent(seed: int) -> None:
    n = 60
    result = run_pipeline(
        MediaConfig(encoding="raw", fec="xor", fec_group=8),
        FaultPolicy(drop=0.05, delay=0.001),
        n,
        seed=seed,
    )
    metrics = result.metrics
    checks = _integrity(result.events)
    # No crash, and the sink counters add up: every arrival is exactly one of
    # ok / bad, so their sum is the number of frames delivered to the sink.
    assert metrics["frames_ok"] + metrics["frames_bad"] == len(checks)
    # Loss can never exceed what was sourced.
    totals = result.snapshot["totals"]
    assert totals["frames_sourced"] == n
    assert metrics["lost"] <= totals["frames_sourced"]
    # The independent white-box observer agrees with the sink's own tallies.
    assert totals["frames_ok"] == metrics["frames_ok"]
    assert totals["frames_bad"] == metrics["frames_bad"]
    # Bounded loss with FEC: bit-exact path, so nothing verifies bad, and the
    # mesh stayed live (some frames delivered).
    assert metrics["frames_bad"] == 0
    assert metrics["frames_ok"] > 0


# NOTE (findings, not delivery bugs -- reported to the caller):
#
# 1. IntegritySink.metrics()["mean_latency"]/["last_latency"] read 0.0 through
#    the full pipeline. Packetizer builds a fresh RTP meta dict and drops the
#    source's meta["capture_now"], so it never reaches the sink's latency
#    calc. Arguably correct (capture_now is not an RTP field); the white-box
#    MetricsCollector recovers true glass-to-glass latency via its source-OUT
#    /pts join, and that value IS non-zero (asserted in scenario 1).
#
# 2. MetricsCollector's default StageRoles(wire_in="jitter") expects the first
#    receiver stage to emit an IN tap per arrival, but JitterBuffer emits HOLD
#    (and LATE), never IN -- so snapshot packets_recv is always 0 and the
#    white-box "packet_loss" equals packets_sent for the canonical pipeline.
#    These tests therefore assert loss via the sink metrics and via
#    fec_recovered / stage RECOVER counts, not via the collector's packet_loss.
