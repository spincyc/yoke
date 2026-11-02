"""Tests for ``yoke.media.cli`` — the media debug/observability CLI.

Everything here is offline and deterministic: ``main`` is driven with argv
and asserted on exit code + captured output, and the pure render/parse
helpers are exercised directly. No sockets are ever opened; the real
``run``/``--live`` socket paths are covered by the loopback lane, and here
only their arg-parsing, config-building and plan-rendering are tested (via
``--dry-run``).
"""

from __future__ import annotations

import json

import pytest

from yoke.media.assembly import MediaConfig, build_receiver, build_sender
from yoke.media.cli import (
    LINK_DELAY,
    Faults,
    SimLink,
    build_config,
    build_parser,
    drive,
    main,
    render_run_plan,
    render_simulate_text,
    resolve_run_plan,
)
from yoke.media.types import EventKind, MediaUnit

# Small frames keep the model payloads tiny while exercising every stage.
BASE = ["--width", "320", "--height", "240"]


def _parse(argv: list[str]) -> object:
    return build_parser().parse_args(argv)


# -- drive() core: determinism ----------------------------------------------


def test_drive_clean_delivers_every_frame() -> None:
    from yoke.media.assembly import MediaConfig

    result = drive(MediaConfig(width=320, height=240), Faults(), frames=10)
    assert result.frames_sourced == 10
    assert result.frames_ok == 10
    assert result.frames_bad == 0
    assert result.frames_lost == 0
    assert result.packets_dropped == 0
    assert result.integrity_ok is True


def test_drive_is_deterministic_for_a_fixed_seed() -> None:
    from yoke.media.assembly import MediaConfig

    cfg = MediaConfig(width=320, height=240, seed=7)
    faults = Faults(drop=0.2)
    a = drive(cfg, faults, frames=16)
    b = drive(cfg, faults, frames=16)
    assert (a.frames_ok, a.frames_lost, a.packets_dropped) == (
        b.frames_ok,
        b.frames_lost,
        b.packets_dropped,
    )
    assert render_simulate_text(a) == render_simulate_text(b)


# -- simulate ----------------------------------------------------------------


def test_simulate_clean_exit_zero_and_report(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(["simulate", *BASE, "--frames", "12"])
    out = capsys.readouterr().out
    assert code == 0
    assert "ok=12" in out
    assert "lost=0" in out
    assert "verdict:  OK" in out


def test_simulate_json_parses_to_snapshot_schema(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(["simulate", *BASE, "--frames", "8", "--json"])
    out = capsys.readouterr().out
    assert code == 0
    obj = json.loads(out)
    # snapshot schema (metrics.MetricsCollector.snapshot)
    assert {"stages", "streams", "totals"} <= set(obj)
    assert obj["summary"]["frames_ok"] > 0
    assert obj["summary"]["frames_lost"] == 0


def test_simulate_with_drop_reports_loss_but_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(["simulate", *BASE, "--frames", "16", "--drop", "0.1", "--seed", "1"])
    out = capsys.readouterr().out
    # A report never fails on loss — it reports it.
    assert code == 0
    assert "DEGRADED" in out
    assert "drop=0.1" in out
    # deterministic: same argv, same bytes.
    main(["simulate", *BASE, "--frames", "16", "--drop", "0.1", "--seed", "1"])
    assert capsys.readouterr().out == out


def test_simulate_fec_xor_reports_recoveries(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(
        [
            "simulate",
            *BASE,
            "--fec",
            "xor",
            "--drop",
            "0.02",
            "--seed",
            "0",
            "--frames",
            "24",
            "--mtu",
            "1500",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    result = drive(
        build_config(_parse(["simulate", *BASE, "--fec", "xor", "--mtu", "1500"])),  # type: ignore[arg-type]
        Faults(drop=0.02),
        frames=24,
    )
    assert result.fec_recovered > 0
    assert "recovered=" in out
    assert "fec:      recovered=0" not in out


# -- probe -------------------------------------------------------------------


def test_probe_clean_passes(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["probe", *BASE, "--frames", "10"])
    out = capsys.readouterr().out
    assert code == 0
    assert "integrity: PASS" in out


def test_probe_heavy_drop_fails_the_gate(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(["probe", *BASE, "--frames", "10", "--drop", "0.5", "--seed", "1"])
    out = capsys.readouterr().out
    assert code != 0
    assert "integrity: FAIL" in out


# -- trace -------------------------------------------------------------------


def test_trace_emits_parseable_json_lines_over_many_stages(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(["trace", *BASE, "--frames", "3"])
    out = capsys.readouterr().out
    assert code == 0
    lines = out.splitlines()
    assert len(lines) > 3  # many events per frame
    objs = [json.loads(line) for line in lines]
    for obj in objs:
        assert {"stage", "kind", "seq", "pts", "now", "detail"} <= set(obj)
    stages = {obj["stage"] for obj in objs}
    # A frame's journey crosses the source, the sender lanes, and the sink.
    assert {"source", "codec", "packetize", "sink"} <= stages
    # ordered by time (non-decreasing "now")
    nows = [obj["now"] for obj in objs]
    assert nows == sorted(nows)


# -- snoop (offline) ---------------------------------------------------------


def test_snoop_offline_topology_has_streams_stages_wire(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(["snoop", *BASE, "--frames", "8", "--drop", "0.1", "--seed", "1"])
    out = capsys.readouterr().out
    assert code == 0
    topo = json.loads(out)
    assert "streams" in topo
    assert "stages" in topo
    assert any("wire" in entry for entry in topo["streams"].values())


# -- run (dry-run plans, no sockets) -----------------------------------------


def test_run_sender_multicast_plan(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["run", "--role", "sender", "--group", "239.1.2.3", "--dry-run"])
    out = capsys.readouterr().out
    assert code == 0
    assert "plan:      sender" in out
    assert "transport: multicast" in out
    assert "dest:      239.1.2.3:5004" in out
    assert "interface=0.0.0.0 ttl=1 loopback=on" in out


def test_run_sender_unicast_plan_respects_frames(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(
        [
            "run",
            "--role",
            "sender",
            "--peer",
            "10.0.0.9",
            "--unicast",
            "--frames",
            "120",
            "--dry-run",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "transport: unicast" in out
    assert "frames=120" in out


def test_run_receiver_multicast_plan_binds_group(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(
        [
            "run",
            "--role",
            "receiver",
            "--group",
            "239.1.2.3",
            "--port",
            "6000",
            "--snoop",
            "--duration",
            "5",
            "--dry-run",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "plan:      receiver" in out
    assert "bind:      239.1.2.3:6000" in out
    assert "duration=5s" in out
    assert "snoop:     on" in out


def test_run_receiver_unicast_binds_wildcard() -> None:
    args = _parse(["run", "--role", "receiver", "--unicast", "--dry-run"])
    plan, problem = resolve_run_plan(args, build_config(args))  # type: ignore[arg-type]
    assert problem is None
    assert plan is not None
    assert plan.transport == "unicast"
    assert plan.endpoint[0] == "0.0.0.0"
    assert "bind:      0.0.0.0" in render_run_plan(plan)


def test_run_sender_missing_peer_exits_2(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(["run", "--role", "sender", "--unicast", "--dry-run"])
    err = capsys.readouterr().err
    assert code == 2
    assert "requires --peer" in err


# -- bad args ----------------------------------------------------------------


def test_unknown_encoding_exits_2() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["simulate", "--encoding", "bogus"])
    assert exc.value.code == 2


def test_unknown_fec_exits_2() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["simulate", "--fec", "bogus"])
    assert exc.value.code == 2


def test_missing_subcommand_exits_2() -> None:
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


# -- render helpers ----------------------------------------------------------


def test_render_run_plan_is_multiline_and_labeled() -> None:
    args = _parse(["run", "--role", "sender", "--dry-run"])
    plan, _ = resolve_run_plan(args, build_config(args))  # type: ignore[arg-type]
    assert plan is not None
    text = render_run_plan(plan)
    assert text.splitlines()[0].startswith("plan:")
    assert "media:" in text


def test_multicast_sender_rejects_peer_without_unicast(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["run", "--role", "sender", "--peer", "10.0.0.9", "--dry-run"]) == 2
    assert "--peer requires --unicast" in capsys.readouterr().err


def test_multicast_options_are_explicit_in_plan(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        main(
            [
                "run",
                "--role",
                "sender",
                "--interface",
                "127.0.0.1",
                "--ttl",
                "0",
                "--no-loopback",
                "--dry-run",
            ]
        )
        == 0
    )
    assert "interface=127.0.0.1 ttl=0 loopback=off" in capsys.readouterr().out


@pytest.mark.parametrize(
    "options",
    [
        ["--fps", "0"],
        ["--width", "-1"],
        ["--height", "0"],
        ["--mtu", "12"],
        ["--mtu", "65536"],
        ["--port", "0"],
        ["--port", "65536"],
        ["--ttl", "256"],
        ["--ttl", "-1"],
        ["--group", "127.0.0.1"],
        ["--group", "ff02::1"],
        ["--interface", "239.1.1.1"],
        ["--interface", "eth0"],
        ["--frames", "0"],
        ["--duration", "nan"],
        ["--duration", "inf"],
        ["--duration", "-1"],
    ],
)
def test_run_rejects_invalid_values_before_opening_socket(
    options: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["run", "--role", "sender", "--dry-run", *options]) == 2
    assert capsys.readouterr().err


@pytest.mark.parametrize(
    "options",
    [
        ["--drop", "nan"],
        ["--drop", "1.1"],
        ["--dup", "-0.1"],
        ["--reorder", "inf"],
        ["--jitter", "nan"],
        ["--jitter", "-1"],
        ["--corrupt", "1.1"],
        ["--frames", "-1"],
    ],
)
def test_offline_rejects_invalid_faults(
    options: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["simulate", *BASE, *options]) == 2
    assert capsys.readouterr().err


def test_multicast_feedback_is_off_by_default() -> None:
    args = build_parser().parse_args(["run", "--role", "receiver", "--dry-run"])
    plan, problem = resolve_run_plan(args, build_config(args))
    assert problem is None and plan is not None
    assert plan.feedback_dest is None


def test_designated_receiver_feedback_is_explicit_unicast() -> None:
    args = build_parser().parse_args(
        [
            "run",
            "--role",
            "receiver",
            "--feedback-host",
            "127.0.0.1",
            "--feedback-port",
            "12345",
            "--dry-run",
        ]
    )
    plan, problem = resolve_run_plan(args, build_config(args))
    assert problem is None and plan is not None
    assert plan.feedback_dest == ("127.0.0.1", 12345)


@pytest.mark.parametrize(
    "options",
    [
        ["--feedback-host", "127.0.0.1"],
        ["--feedback-port", "12345"],
        ["--feedback-host", "239.1.1.1", "--feedback-port", "12345"],
        ["--feedback-host", "0.0.0.0", "--feedback-port", "12345"],
        ["--feedback-host", "127.0.0.1", "--feedback-port", "0"],
    ],
)
def test_invalid_feedback_options_exit_usage(
    options: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["run", "--role", "receiver", "--dry-run", *options]) == 2
    assert capsys.readouterr().err


def test_live_snoop_accepts_bounded_duration() -> None:
    args = build_parser().parse_args(
        [
            "snoop",
            "--live",
            "--duration",
            "0.1",
            "--interface",
            "127.0.0.1",
        ]
    )
    build_config(args)
    assert args.duration == 0.1


@pytest.mark.parametrize(
    "argv",
    [
        ["run", "--role", "sender", "--fec", "xor"],
        ["run", "--role", "receiver", "--fec", "xor"],
        ["snoop", "--live", "--fec", "xor"],
    ],
)
def test_live_xor_rejected_before_socket_creation(
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("live XOR opened a socket")

    monkeypatch.setattr("yoke.media.cli.open_receiver_socket", forbidden)
    monkeypatch.setattr("yoke.media.cli.open_sender_socket", forbidden)
    assert main(argv) == 2
    assert "XOR FEC is offline-only" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["run", "--role", "sender", "--frames", "2", "--duration", "1"],
        ["run", "--role", "receiver", "--frames", "2"],
        ["run", "--role", "receiver", "--peer", "127.0.0.1"],
        ["run", "--role", "sender", "--snoop"],
    ],
)
def test_run_rejects_inapplicable_options(
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([*argv, "--dry-run"]) == 2
    assert capsys.readouterr().err


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
    assert "verdict:  DEGRADED" in render_simulate_text(result)


def test_simulate_total_loss_reports_degraded_but_remains_a_report(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["simulate", *BASE, "--frames", "3", "--drop", "1"]) == 0
    text = capsys.readouterr().out
    assert "sourced=3 ok=0 bad=0 lost=3" in text
    assert "verdict:  DEGRADED" in text
    assert main(["probe", *BASE, "--frames", "3", "--drop", "1"]) == 1
    assert "integrity: FAIL (3 frame(s) never arrived)" in capsys.readouterr().out


def test_empty_drive_cannot_report_success() -> None:
    result = drive(MediaConfig(), Faults(), frames=0)
    assert not result.integrity_ok
    assert "verdict:  DEGRADED" in render_simulate_text(result)


def test_real_run_rejects_small_path_mtu_before_opening_socket(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("invalid path MTU opened a socket")

    monkeypatch.setattr("yoke.media.cli.open_sender_socket", forbidden)
    assert main(["run", "--role", "sender", "--mtu", "40"]) == 2
    assert "path mtu must be in [98, 65535]" in capsys.readouterr().err


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
