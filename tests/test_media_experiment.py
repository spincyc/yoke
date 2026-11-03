"""Offline experiment behavior through the reusable media API."""

from __future__ import annotations

import subprocess
import sys

import pytest

from yoke.media.assembly import MediaConfig, build_receiver, build_sender
from yoke.media.experiment import drive
from yoke.media.simulation import LINK_DELAY, Faults, SimLink
from yoke.media.types import EventKind, MediaUnit


def test_experiment_runs_without_command_or_live_io_modules() -> None:
    script = """
import sys
import time

forbidden = {
    "argparse", "socket", "yoke.media.cli", "yoke.media.runner", "yoke.media.sockets"
}

class ExperimentBoundary:
    def find_spec(self, fullname, path=None, target=None):
        if fullname in forbidden:
            raise AssertionError(f"experiment imported {fullname}")

def clock_read():
    raise AssertionError("experiment read a live clock")

sys.meta_path.insert(0, ExperimentBoundary())
time.time = time.monotonic = time.perf_counter = clock_read

from yoke.media.assembly import MediaConfig
from yoke.media.experiment import drive
from yoke.media.simulation import Faults

result = drive(MediaConfig(width=32, height=32), Faults(), frames=2)
assert result.integrity_ok and result.frames_ok == 2
assert forbidden.isdisjoint(sys.modules)
"""
    subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_drive_clean_delivers_every_frame() -> None:
    result = drive(MediaConfig(width=320, height=240), Faults(), frames=10)
    assert result.frames_sourced == 10
    assert result.frames_ok == 10
    assert result.frames_bad == 0
    assert result.frames_lost == 0
    assert result.packets_dropped == 0
    assert result.integrity_ok is True


def test_drive_is_deterministic_for_a_fixed_seed() -> None:
    cfg = MediaConfig(width=320, height=240, seed=7)
    faults = Faults(drop=0.2)
    a = drive(cfg, faults, frames=16)
    b = drive(cfg, faults, frames=16)
    assert a == b


def test_simulation_feedback_changes_subsequent_codec_models_causally() -> None:
    result = drive(
        MediaConfig(width=320, height=240, encoding="jpegxs"),
        Faults(drop=0.1),
        frames=90,
    )
    adjustments = [
        e
        for e in result.events
        if e.kind is EventKind.ADJUST and e.stage == "ratecontrol"
    ]
    assert adjustments
    first_adjust = min(e.now for e in adjustments)
    assert any(
        e.stage == "sink"
        and e.kind is EventKind.INTEGRITY
        and e.now + LINK_DELAY <= first_adjust
        for e in result.events
    )
    codec = [e for e in result.events if e.stage == "codec" and e.kind is EventKind.OUT]
    assert all(e.detail["ratio"] == 2.5 for e in codec if e.now < first_adjust)
    assert any(e.detail["ratio"] > 2.5 for e in codec if e.now > first_adjust)
    assert result.target_ratio is not None and result.target_ratio > 2.5
    assert result.feedback_reports == result.feedback_delivered > 0


def test_simulation_feedback_waits_for_reverse_link_delivery() -> None:
    config = MediaConfig(width=32, height=32, encoding="jpegxs")
    sender, receiver = build_sender(config), build_receiver(config, report_every=1)
    receiver.sink.set_congestion(1)
    link = SimLink(
        sender.pipeline, receiver.pipeline, seed=0, faults=Faults(), feedback_delay=0.05
    )
    frame = sender.source.frame(0).emit[0]
    link.send(sender.pipeline.push(frame, 0).emitted)
    link.advance(0.04)
    assert link.feedback and not link.feedback_delivered
    assert sender.rate_state.target == 2.5
    link.advance(0.1)
    assert sender.rate_state.target > 2.5
    assert all(
        delivery == pytest.approx(report.now + 0.05)
        for report, delivery in link.feedback_delivered
    )


@pytest.mark.parametrize("fec", ["none", "dup", "xor"])
def test_clean_fec_measures_the_same_wire_boundary(fec: str) -> None:
    result = drive(MediaConfig(width=320, height=240, fec=fec), Faults(), frames=30)
    assert result.frames_ok == 30 and result.frames_lost == 0
    assert result.packets_recv == result.packets_sent
    assert result.data_packets_recv == 120
    assert result.snapshot["streams"]["-"]["packet_loss"] == 0
    assert result.snapshot["streams"]["-"]["packet_loss_rate"] == 0
    assert result.snapshot["totals"]["packet_loss"] == 0
    if fec != "none":
        assert result.packets_recv > result.data_packets_recv


def test_network_duplicates_cannot_hide_known_injected_packet_loss() -> None:
    result = drive(MediaConfig(width=320, height=240), Faults(drop=0.2, dup=1), 30)
    assert result.packets_recv > result.packets_sent
    assert result.packets_dropped > 0
    assert result.snapshot["streams"]["-"]["packet_loss"] == result.packets_dropped
    assert result.topology["streams"]["-"]["packet_loss"] == result.packets_dropped
    assert result.snapshot["network"]["packets_duplicated"] == result.packets_duplicated


@pytest.mark.parametrize("missing", [{0}, {2}, {0, 2}, {0, 1, 2}])
def test_known_source_ids_account_for_leading_trailing_and_all_loss(
    missing: set[int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schedule = SimLink._schedule

    def drop_selected(link: SimLink, unit: MediaUnit) -> None:
        if unit.meta["frame_seq"] in missing:
            link.dropped.append(unit)
        else:
            schedule(link, unit)

    monkeypatch.setattr(SimLink, "_schedule", drop_selected)
    result = drive(MediaConfig(width=320, height=240), Faults(), frames=3)
    assert result.frames_sourced == 3
    assert result.frames_ok == 3 - len(missing)
    assert result.frames_lost == len(missing)
    assert result.snapshot["streams"]["-"]["frames_lost"] == len(missing)


def test_empty_drive_cannot_report_success() -> None:
    result = drive(MediaConfig(), Faults(), frames=0)
    assert not result.integrity_ok


def test_offline_feedback_uses_packet_arrival_jitter_without_capture_clock() -> None:
    config = MediaConfig(width=32, height=32, fps=30)
    sender, receiver = build_sender(config), build_receiver(config, report_every=1)
    link = SimLink(
        sender.pipeline,
        receiver.pipeline,
        seed=3,
        faults=Faults(jitter=0.015, delay=0.02),
    )
    for seq in range(30):
        now = seq / config.fps
        frame = sender.source.frame(now).emit[0]
        link.send(sender.pipeline.push(frame, now).emitted)
        link.advance((seq + 1) / config.fps)
    link.drain()
    assert link.feedback
    assert any(report.jitter > 0 for report in link.feedback)
    assert receiver.sink.metrics()["mean_latency"] is None


def test_simulator_drains_network_and_playout_before_counting_loss() -> None:
    result = drive(
        MediaConfig(width=32, height=32, fps=5, playout_delay=0.2),
        Faults(delay=0.5),
        frames=3,
    )
    assert result.frames_ok == 3
    assert result.frames_lost == 0


def test_finite_simulation_emits_partial_xor_group_before_draining() -> None:
    result = drive(MediaConfig(width=32, height=32, fec="xor"), Faults(), frames=1)
    assert result.frames_ok == 1 and result.frames_lost == 0
    # Four data packets plus parity for the otherwise unfinished group of eight.
    assert result.packets_sent == result.packets_recv == 5


def test_finite_simulation_reports_pending_fragment_assembly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schedule = SimLink._schedule

    def drop_last_fragment(link: SimLink, unit: MediaUnit) -> None:
        meta = unit.meta
        if (
            meta["slice_index"] == meta["slice_count"] - 1
            and meta["frag_index"] == meta["frag_count"] - 1
        ):
            link.dropped.append(unit)
        else:
            schedule(link, unit)

    monkeypatch.setattr(SimLink, "_schedule", drop_last_fragment)
    result = drive(MediaConfig(width=32, height=32, mtu=125), Faults(), frames=1)
    assert result.frames_ok == 0 and result.frames_lost == 1
    assert any(
        event.stage == "depacketize"
        and event.kind is EventKind.DROP
        and event.detail.get("incomplete") is True
        for event in result.events
    )
