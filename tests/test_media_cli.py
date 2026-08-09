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

from yoke.media.cli import (
    Faults,
    build_config,
    build_parser,
    drive,
    main,
    render_run_plan,
    render_simulate_text,
    resolve_run_plan,
)

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
    code = main(["run", "--role", "sender", "--peer", "10.0.0.9", "--dry-run"])
    out = capsys.readouterr().out
    assert code == 0
    assert "plan:      sender" in out
    assert "transport: multicast" in out
    assert "dest:      10.0.0.9:5004" in out


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
    code = main(["run", "--role", "sender", "--dry-run"])
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
    args = _parse(["run", "--role", "sender", "--peer", "h", "--dry-run"])
    plan, _ = resolve_run_plan(args, build_config(args))  # type: ignore[arg-type]
    assert plan is not None
    text = render_run_plan(plan)
    assert text.splitlines()[0].startswith("plan:")
    assert "media:" in text
