"""Offline parity tests for CLI and TUI control actions (ADR 0031)."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from tests.test_cli import (
    AUTHOR,
    KEY,
    PSK,
    FakeNet,
    ack,
    ann_consumer,
    ann_producer,
)
from yoke.cli import (
    main,
    run_inspect,
    run_reapply,
    run_snoop,
    run_status,
    run_unpair_producer,
)
from yoke.core.frames import decode, encode
from yoke.core.types import FrameType
from yoke.telemetry import tui
from yoke.telemetry.collector import DebugContext, TelemetryCollector
from yoke.telemetry.demo import demo_debug_context, demo_snapshot
from yoke.telemetry.tui import (
    ActionRequest,
    ActionResult,
    ControlCallbacks,
    ViewState,
    render,
)

# -- CLI: inspect ------------------------------------------------------------


def test_inspect_consumer_and_producer_json_and_text() -> None:
    intent_raw = encode(
        FrameType.INT,
        "cli-other",
        10,
        1_755_000_000.0,
        {
            "op": "pair",
            "consumer": "tv-den",
            "producer": "cam-1",
            "version": [3, "op-prev"],
        },
        PSK,
    )
    net = FakeNet(
        [
            (1000.1, ann_producer(origin="cam-1", kind="hdmi", state="active")),
            (
                1000.2,
                ann_consumer(
                    origin="tv-den",
                    producer="cam-1",
                    version=(3, "op-prev"),
                    state="active",
                ),
            ),
            (1000.3, intent_raw),
            (1000.4, ack(consumer="tv-den", version=(3, "op-prev"), seq=2)),
        ]
    )
    code, out, err = run_inspect(
        net.recv, net.clock, PSK, "tv-den", 2.5, json_mode=True
    )
    assert code == 0
    assert err is None
    doc = json.loads(out)
    assert doc["schema_version"] == 1
    assert doc["authentication"] == "site-psk"
    assert doc["node"]["id"] == "tv-den"
    assert doc["node"]["pairing"] == {"producer": "cam-1", "version": [3, "op-prev"]}
    assert doc["node"]["last_intent"]["acked"] is True
    assert doc["node"]["last_intent"]["applied"] is True

    net2 = FakeNet(
        [
            (1000.1, ann_producer(origin="cam-1", kind="hdmi", state="active")),
            (
                1000.2,
                ann_consumer(
                    origin="tv-den",
                    producer="cam-1",
                    version=(3, "op-prev"),
                    state="active",
                ),
            ),
        ]
    )
    code2, text2, err2 = run_inspect(net2.recv, net2.clock, PSK, "cam-1", 2.5)
    assert code2 == 0
    assert err2 is None
    assert "ID:      cam-1" in text2
    assert "Paired:  tv-den" in text2
    assert "Media:   " in text2
    assert "239.10.0.1" in text2


def test_inspect_missing_and_invalid_node() -> None:
    net = FakeNet([])
    code, out, err = run_inspect(
        net.recv, net.clock, PSK, "ghost-1", 2.5, json_mode=True
    )
    assert code == 1
    assert err is not None and "did not announce" in err
    assert json.loads(out)["node"] is None

    code_bad, _out_bad, err_bad = run_inspect(net.recv, net.clock, PSK, "BAD ID", 2.5)
    assert code_bad == 2
    assert err_bad is not None and "invalid node id" in err_bad


# -- CLI: status --sort ------------------------------------------------------


def test_status_sort_matches_tui_sort_vocabulary() -> None:
    net = FakeNet(
        [
            (
                1000.1,
                ann_producer(origin="z-usb", kind="usb", name="Alpha", state="pending"),
            ),
            (
                1000.5,
                ann_consumer(origin="a-hdmi", kind="hdmi", name="Zeta", state="active"),
            ),
        ]
    )
    code, raw = run_status(
        net.recv, net.clock, PSK, 2.5, json_mode=True, sort_key="name"
    )
    assert code == 0
    assert [n["id"] for n in json.loads(raw)["nodes"]] == ["z-usb", "a-hdmi"]

    net2 = FakeNet(
        [
            (
                1000.1,
                ann_producer(origin="z-usb", kind="usb", name="Alpha", state="pending"),
            ),
            (
                1000.5,
                ann_consumer(origin="a-hdmi", kind="hdmi", name="Zeta", state="active"),
            ),
        ]
    )
    code2, raw2 = run_status(
        net2.recv, net2.clock, PSK, 2.5, json_mode=True, sort_key="age"
    )
    assert code2 == 0
    # a-hdmi seen later -> smaller age -> first.
    assert [n["id"] for n in json.loads(raw2)["nodes"]] == ["a-hdmi", "z-usb"]


# -- CLI: reapply ------------------------------------------------------------


def test_reapply_paired_and_unpaired_consumer() -> None:
    net = FakeNet(
        [
            (
                1000.2,
                ann_consumer(
                    origin="tv-den",
                    producer="cam-1",
                    version=(7, "prev-op"),
                    state="error",
                ),
            ),
            (1000.5, ack(consumer="tv-den", version=(8, AUTHOR), seq=2)),
        ]
    )
    producer, outcome = run_reapply(net.io(), "tv-den")
    assert producer == "cam-1"
    assert outcome.exit_code == 0
    assert outcome.committed is True
    assert outcome.sent_version == (8, AUTHOR)
    body = decode(net.sends[0][1], PSK).body
    assert body == {
        "op": "pair",
        "consumer": "tv-den",
        "producer": "cam-1",
        "version": [8, AUTHOR],
    }

    net_unpaired = FakeNet(
        [
            (
                1000.2,
                ann_consumer(origin="tv-den", producer=None, version=(2, "prev-op")),
            ),
            (1000.5, ack(consumer="tv-den", version=(3, AUTHOR), seq=2)),
        ]
    )
    producer2, outcome2 = run_reapply(net_unpaired.io(), "tv-den")
    assert producer2 is None
    assert outcome2.exit_code == 0
    body2 = decode(net_unpaired.sends[0][1], PSK).body
    assert body2 == {"op": "unpair", "consumer": "tv-den", "version": [3, AUTHOR]}


def test_reapply_refuses_producer_and_missing_consumer() -> None:
    net_prod = FakeNet([(1000.1, ann_producer(origin="cam-1"))])
    _prod, outcome = run_reapply(net_prod.io(), "cam-1")
    assert outcome.exit_code == 2
    assert net_prod.sends == []

    net_empty = FakeNet([])
    _prod2, outcome2 = run_reapply(net_empty.io(), "tv-missing")
    assert outcome2.exit_code == 1
    assert net_empty.sends == []


# -- CLI: unpair --producer --------------------------------------------------


def test_unpair_producer_detaches_all_matching_consumers() -> None:
    authors = iter(["op-first", "op-second"])
    net = FakeNet(
        [
            (1000.1, ann_producer(origin="cam-1")),
            (1000.2, ann_consumer(origin="tv-a", producer="cam-1", version=(2, "old"))),
            (1000.3, ann_consumer(origin="tv-b", producer="cam-1", version=(5, "old"))),
            (1000.4, ann_consumer(origin="tv-c", producer="cam-2", version=(1, "old"))),
            (1002.6, ack(consumer="tv-a", version=(3, "op-first"), seq=2)),
            (1002.8, ack(consumer="tv-b", version=(6, "op-second"), seq=2)),
        ]
    )
    io = net.io()
    io = type(io)(**{**io.__dict__, "operation_author": lambda: next(authors)})
    batch = run_unpair_producer(io, "cam-1")
    assert batch.exit_code == 0
    assert [cid for cid, _ in batch.results] == ["tv-a", "tv-b"]
    sent = [decode(raw, PSK).body for _, raw in net.sends]
    assert sent == [
        {"op": "unpair", "consumer": "tv-a", "version": [3, "op-first"]},
        {"op": "unpair", "consumer": "tv-b", "version": [6, "op-second"]},
    ]


def test_unpair_producer_none_attached_and_validation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    net = FakeNet([(1000.1, ann_producer(origin="cam-1"))])
    batch = run_unpair_producer(net.io(), "cam-1")
    assert batch.exit_code == 0
    assert batch.results == ()
    assert "no consumers" in batch.out[0]

    psk_file = tmp_path / "psk"
    psk_file.write_bytes(KEY)
    psk_file.chmod(0o600)
    assert main(["--psk-file", str(psk_file), "unpair"]) == 2
    assert "specify either CONSUMER or --producer" in capsys.readouterr().err
    assert (
        main(["--psk-file", str(psk_file), "unpair", "tv-a", "--producer", "cam-1"])
        == 2
    )
    assert "not both" in capsys.readouterr().err


# -- CLI: bounded snoop ------------------------------------------------------


def test_snoop_count_stops_after_n_records_and_validates_args(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    items = iter(
        [
            (ann_producer(origin="cam-1", seq=1), 1000.0),
            (ann_consumer(origin="tv-den", seq=1), 1000.1),
            (ann_producer(origin="cam-1", seq=2), 1000.2),
        ]
    )
    out: list[str] = []
    err: list[str] = []
    code = run_snoop(
        lambda: next(items, None),
        PSK,
        True,
        out.append,
        err.append,
        clock=lambda: 1000.5,
        max_records=2,
    )
    assert code == 0
    assert len(out) == 2
    topo = json.loads(err[-1])
    assert "cam-1" in topo["producers"]
    assert "tv-den" in topo["consumers"]

    def forbidden(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("socket opened for bad snoop args")

    monkeypatch.setattr("yoke.cli.open_multicast_socket", forbidden)
    assert main(["snoop", "--count", "0"]) == 2
    assert "--count must be a positive integer" in capsys.readouterr().err
    assert main(["snoop", "--window", "-1"]) == 2
    assert "finite and positive" in capsys.readouterr().err


# -- TUI: inspect, log, pair, unpair, reapply & RO/RW guard ------------------


def test_tui_inspect_and_log_overlays_render_and_close() -> None:
    snapshot = demo_snapshot(12.0)
    debug = demo_debug_context(snapshot)
    state = ViewState(writable=True)
    state.observe(snapshot)

    assert state.handle_key("i", snapshot)
    assert state.modal == "inspect"
    lines = render(snapshot, width=100, height=30, state=state, debug=debug)
    text = "\n".join(lines)
    assert "INSPECT /" in text
    assert "Events:" in text
    assert all(len(line) == 100 for line in lines)
    assert all(" " <= ch <= "~" for line in lines for ch in line)
    assert state.handle_key("escape", snapshot)
    assert state.modal == ""

    assert state.handle_key("l", snapshot)
    assert state.modal == "log"
    log_text = "\n".join(
        render(snapshot, width=100, height=30, state=state, debug=debug)
    )
    assert "CONTROL LOG (CLI: yoke snoop)" in log_text
    assert "Verdicts:" in log_text
    assert state.handle_key("l", snapshot)
    assert state.modal == ""


def test_tui_readonly_blocks_all_write_keys() -> None:
    snapshot = demo_snapshot(5.0)
    state = ViewState(writable=False, readonly_reason="read-only mode (--read-only)")
    state.observe(snapshot)
    for key in ("p", "u", "r"):
        assert state.handle_key(key, snapshot)
        assert state.modal == ""
        assert state.pending_action is None
        assert state.action_ok is False
        assert state.action_status == "read-only mode (--read-only)"
    header = render(snapshot, width=100, height=24, state=state)[0]
    assert "[RO]" in header


def test_tui_pair_unpair_reapply_state_machine() -> None:
    snapshot = demo_snapshot(5.0)
    state = ViewState(writable=True)
    state.observe(snapshot)

    # Select usb-workstation (consumer) -> pair picker shows only usb-desk (same kind).
    state.selected_node_id = "usb-workstation"
    assert state.handle_key("p", snapshot)
    assert state.modal == "pair_pick"
    assert state.picker_candidates == ("usb-desk",)
    picker_text = "\n".join(render(snapshot, width=100, height=28, state=state))
    assert "PAIR ROUTE (CLI: yoke pair)" in picker_text
    assert "usb-desk" in picker_text
    assert state.handle_key("enter", snapshot)
    assert state.modal == ""
    assert state.pending_action == ActionRequest(
        op="pair", target="usb-workstation", producer="usb-desk"
    )
    state.pending_action = None

    # Unpair consumer requires confirmation.
    assert state.handle_key("u", snapshot)
    assert state.modal == "confirm"
    assert "Unpair usb-workstation" in state.confirm_prompt
    confirm_text = "\n".join(render(snapshot, width=100, height=28, state=state))
    assert "CONFIRM CONTROL INTENT" in confirm_text
    assert state.handle_key("n", snapshot)
    assert state.modal == ""
    assert state.pending_action is None

    assert state.handle_key("u", snapshot)
    assert state.handle_key("y", snapshot)
    assert state.pending_action == ActionRequest(op="unpair", target="usb-workstation")
    state.pending_action = None

    # Reapply on consumer.
    assert state.handle_key("r", snapshot)
    assert state.modal == "confirm"
    assert state.handle_key("enter", snapshot)
    assert state.pending_action == ActionRequest(op="reapply", target="usb-workstation")
    state.pending_action = None

    # Select producer camera-east: unpair targets its consumers; reapply refused.
    state.selected_node_id = "camera-east"
    assert state.handle_key("r", snapshot)
    assert state.modal == ""
    assert state.action_ok is False
    assert "targets a consumer" in (state.action_status or "")

    assert state.handle_key("u", snapshot)
    assert state.modal == "confirm"
    assert state.handle_key("y", snapshot)
    assert state.pending_action == ActionRequest(
        op="unpair-producer", target="camera-east"
    )
    state.pending_action = None

    # Pair picker from producer shows compatible same-kind consumers.
    assert state.handle_key("p", snapshot)
    assert state.modal == "pair_pick"
    assert set(state.picker_candidates) == {
        "display-den",
        "display-spare",
        "display-studio",
    }
    assert state.handle_key("escape", snapshot)
    assert state.modal == ""


def test_tui_interactive_screen_executes_background_control_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_telemetry_tui import _Screen, _terminal

    clock = [0.0]
    monkeypatch.setattr(tui.time, "monotonic", lambda: clock[0])
    executed: list[ActionRequest] = []

    def fake_execute(req: ActionRequest) -> ActionResult:
        executed.append(req)
        return ActionResult(True, f"{req.label()} committed")

    # Select display-den (2nd ordered device: camera-east is 1st? No, sorted by name:
    # "Den display" is 1st -> display-den! Press 'u', 'y', wait tick, then 'q').
    screen = _Screen([ord("u"), ord("y"), -1, -1, ord("q")], clock)
    # Pause briefly on getch so the worker thread posts its result reliably.
    orig_getch = screen.getch

    def slow_getch() -> int:
        time.sleep(0.02)
        return orig_getch()

    screen.getch = slow_getch  # type: ignore[method-assign]
    controls = ControlCallbacks(
        writable=True, readonly_reason=None, execute=fake_execute
    )
    assert (
        tui._run_screen(
            screen,  # type: ignore[arg-type]
            _terminal(),
            lambda: demo_snapshot(clock[0]),
            1.0,
            get_debug=lambda: demo_debug_context(demo_snapshot(clock[0])),
            controls=controls,
        )
        == 0
    )
    assert executed == [ActionRequest(op="unpair", target="display-den")]
    assert any("OK: unpair display-den committed" in frame for frame in screen.frames)
    assert any("[RW]" in frame for frame in screen.frames)


def test_collector_debug_context_captures_log_and_inspection() -> None:
    collector = TelemetryCollector(PSK, now=10.0, wall_time=1_755_000_000.0)
    collector.observe(ann_producer(origin="cam-1", seq=1), 10.0, wall=1_755_000_000.0)
    collector.observe(b"not-valid", 11.0, wall=1_755_000_001.0)
    ctx = collector.debug_context(12.0)
    assert isinstance(ctx, DebugContext)
    assert "cam-1" in ctx.nodes
    assert ctx.nodes["cam-1"]["node"]["media"] == {"group": "239.10.0.1", "port": 5004}
    assert len(ctx.log) == 2
    assert "cam-1" in ctx.log[0]
    assert "MALFORMED" in ctx.log[1]


def test_top_cli_read_only_and_writable_wiring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[ControlCallbacks | None] = []

    def fake_run_top(
        get_snapshot: Any,
        interval: float = 1.0,
        *,
        get_debug: Any = None,
        controls: ControlCallbacks | None = None,
    ) -> int:
        captured.append(controls)
        return 0

    monkeypatch.setattr("yoke.telemetry.tui.run_top", fake_run_top)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    class _DummySock:
        def settimeout(self, _t: float) -> None:
            pass

        def recvfrom(self, _n: int) -> tuple[bytes, object]:
            time.sleep(0.01)
            raise TimeoutError

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        "yoke.telemetry.runtime.open_multicast_socket", lambda *_a, **_k: _DummySock()
    )

    psk_file = tmp_path / "psk"
    psk_file.write_bytes(KEY)
    psk_file.chmod(0o600)

    # With PSK -> writable by default.
    assert main(["--psk-file", str(psk_file), "top"]) == 0
    assert captured[-1] is not None and captured[-1].writable is True

    # With PSK + --read-only -> read-only.
    assert main(["--psk-file", str(psk_file), "top", "--read-only"]) == 0
    assert captured[-1] is not None and captured[-1].writable is False

    # With --open -> writable in lab mode.
    assert main(["--open", "top"]) == 0
    assert captured[-1] is not None and captured[-1].writable is True

    # With --demo -> read-only.
    assert main(["top", "--demo"]) == 0
    assert captured[-1] is not None and captured[-1].writable is False
