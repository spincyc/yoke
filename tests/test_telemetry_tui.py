"""The terminal is a deterministic view; every interaction also runs offline."""

from __future__ import annotations

import copy
import sys
from dataclasses import replace
from types import ModuleType
from typing import Any

import pytest

from yoke.telemetry import tui
from yoke.telemetry.demo import demo_snapshot
from yoke.telemetry.model import Snapshot
from yoke.telemetry.tui import (
    HISTORY_LENGTH,
    SORT_KEYS,
    ViewState,
    render,
    run_top,
    source_progress,
)


@pytest.mark.parametrize(
    ("width", "height"),
    [(0, 0), (1, 1), (20, 8), (40, 12), (60, 18), (80, 24), (120, 40), (160, 50)],
)
def test_canvas_is_exact_size_and_printable_ascii(width: int, height: int) -> None:
    snapshot = demo_snapshot(42)
    state = ViewState()
    state.observe(snapshot)
    lines = render(snapshot, width=width, height=height, state=state)
    assert len(lines) == height
    assert all(len(line) == width for line in lines)
    assert all(" " <= char <= "~" for line in lines for char in line)
    if width >= 40 and height >= 12:
        assert "COLLECTOR HOST" in "\n".join(lines)
        assert "DEMO" in lines[0]
        assert "ENDPOINTS" in "\n".join(lines)


def test_untrusted_text_cannot_inject_terminal_controls_or_wide_cells() -> None:
    snapshot = demo_snapshot(1)
    device = replace(
        snapshot.devices[0],
        name="\x1b[2J\n\r\t\x00\x7f\u202e\u0301\u754c\U0001f600",
        driver_detail="\x1b]0;surprise\x07\nbackend",
    )
    host = copy.deepcopy(snapshot.host)
    assert host is not None
    host["node_id"] = "\x1b[31mhost\n"
    snapshot = replace(snapshot, devices=(device,), host=host)
    state = ViewState(source_error="bad\x1b[H\nsource")
    lines = render(snapshot, width=120, height=30, state=state)
    text = "\n".join(lines)
    assert "?" in text
    assert all(len(line) == 120 for line in lines)
    assert all(" " <= char <= "~" for line in lines for char in line)


def test_missing_host_measurements_stay_unknown() -> None:
    snapshot = replace(demo_snapshot(1), host=None, devices=())
    state = ViewState()
    state.observe(snapshot)
    text = "\n".join(render(snapshot, width=100, height=24, state=state))
    assert "unknown" in text
    assert "0.0%" not in text
    assert "0B/s" not in text
    assert "Waiting for endpoint announcements" in text
    assert list(state.cpu_history) == [None]
    assert list(state.rx_history) == [None]


def test_partial_network_rates_are_unknown_instead_of_undercounted() -> None:
    snapshot = demo_snapshot(2)
    host = copy.deepcopy(snapshot.host)
    assert host is not None
    unavailable = {**host["network"][0], "interface": "demo1"}
    unavailable["rx_bytes_per_second"] = None
    host["network"].append(unavailable)
    snapshot = replace(snapshot, host=host)
    state = ViewState()
    state.observe(snapshot)
    assert list(state.rx_history) == [None]
    assert next(iter(state.tx_history)) is not None


def test_render_does_not_mutate_state_or_append_histories() -> None:
    state = ViewState()
    snapshot = demo_snapshot(2)
    state.observe(snapshot)
    before = copy.deepcopy(state)
    render(snapshot, width=80, height=24, state=state)
    render(snapshot, width=120, height=30, state=state)
    assert state == before


def test_histories_are_bounded_and_deduplicate_cached_samples() -> None:
    state = ViewState()
    for tick in range(HISTORY_LENGTH + 60):
        state.observe(demo_snapshot(tick))
        state.observe(demo_snapshot(tick))
    for history in (
        state.cpu_history,
        state.memory_history,
        state.rx_history,
        state.tx_history,
    ):
        assert len(history) == HISTORY_LENGTH
        assert history.maxlen == HISTORY_LENGTH


@pytest.mark.parametrize("cadence", [0.2, 1.0, 60.0])
def test_source_freshness_uses_advertised_cadence(cadence: float) -> None:
    snapshot = replace(demo_snapshot(600), sample_interval_seconds=cadence)
    progress = source_progress(None, snapshot, now=1000)
    tolerance = max(3 * cadence, 3)
    for elapsed in range(1, int(tolerance) + 1):
        assert source_progress(progress, snapshot, now=1000 + elapsed) is progress
    with pytest.raises(ValueError, match=r"not advanced.*freshness unknown"):
        source_progress(progress, snapshot, now=1000 + tolerance + 0.01)
    # Failed checks do not move the baseline or extend the cached sample's life.
    assert progress.advanced_at == 1000
    with pytest.raises(ValueError, match="not advanced"):
        source_progress(progress, snapshot, now=1000 + tolerance + 1)


def test_source_progress_recovers_on_advance_or_restart() -> None:
    snapshot = demo_snapshot(600)
    progress = source_progress(None, snapshot, now=10)
    with pytest.raises(ValueError, match="not advanced"):
        source_progress(progress, snapshot, now=14)
    advanced = source_progress(progress, demo_snapshot(601), now=15)
    assert advanced.uptime_seconds == 601
    assert advanced.advanced_at == 15
    assert source_progress(advanced, demo_snapshot(601), now=18) is advanced
    restarted = source_progress(advanced, demo_snapshot(0), now=19)
    assert restarted.uptime_seconds == 0
    assert restarted.advanced_at == 19
    assert source_progress(restarted, demo_snapshot(0), now=22) is restarted


def test_restart_clears_histories_but_defers_changes_to_a_paused_display() -> None:
    state = ViewState()
    state.observe(demo_snapshot(600))
    state.observe(demo_snapshot(601))
    state.observe(demo_snapshot(0))
    assert len(state.cpu_history) == 1
    state.observe(demo_snapshot(1))
    before = list(state.cpu_history)
    state.handle_key(" ", demo_snapshot(1))
    state.reset_history()
    state.observe(demo_snapshot(0))
    assert list(state.cpu_history) == before
    assert state.display_snapshot(demo_snapshot(0)).uptime_seconds == 1
    state.handle_key(" ", demo_snapshot(0))
    state.observe(demo_snapshot(0))
    assert len(state.cpu_history) == 1
    assert state.display_snapshot(demo_snapshot(0)).uptime_seconds == 0


def test_pause_freezes_display_and_history_and_resume_advances() -> None:
    state = ViewState()
    first = demo_snapshot(10)
    second = demo_snapshot(1000)
    state.observe(first)
    assert state.handle_key(" ", first)
    state.observe(second)
    paused = render(second, width=100, height=24, state=state)
    assert "PAUSED" in paused[0]
    assert "00:00" in paused[0]
    assert "collection continues" in paused[-1]
    assert state.display_snapshot(second) == first
    assert len(state.cpu_history) == 1
    narrow = render(second, width=40, height=12, state=state)
    assert "DEMO / PAUSED" in narrow[0]
    assert state.handle_key(" ", second)
    state.observe(second)
    assert state.display_snapshot(second) == second
    assert len(state.cpu_history) == 2


def test_selection_stays_on_node_when_sorting_and_handles_eviction() -> None:
    snapshot = demo_snapshot(2)
    state = ViewState()
    assert state.handle_key("down", snapshot)
    selected = state.selected_device(snapshot)
    assert selected is not None
    assert selected.node_id == state.ordered_devices(snapshot)[1].node_id
    observed_sorts = set()
    for _ in SORT_KEYS:
        assert state.handle_key("s", snapshot)
        assert state.selected_device(snapshot) == selected
        observed_sorts.add(state.sort_key)
    assert observed_sorts == set(SORT_KEYS)
    evicted = replace(
        snapshot, devices=tuple(d for d in snapshot.devices if d != selected)
    )
    assert state.selected_device(evicted) in evicted.devices
    assert state.selected_device(replace(snapshot, devices=())) is None
    assert state.handle_key("j", replace(snapshot, devices=()))


def test_selection_scrolls_into_view_and_help_keys_are_discoverable() -> None:
    snapshot = demo_snapshot(5)
    state = ViewState()
    for _ in range(10):
        state.handle_key("j", snapshot)
    selected = state.selected_device(snapshot)
    assert selected is not None
    lines = render(snapshot, width=40, height=12, state=state)
    assert any(" > " in line for line in lines)
    assert any(selected.node_id in line for line in lines)
    assert any("PAIR" in line for line in lines)
    assert any("Pair:" in line for line in lines)
    state.handle_key("?", snapshot)
    text = "\n".join(render(snapshot, width=80, height=24, state=state))
    assert "KEYS / OBSERVATION SCOPE" in text
    assert "collection runs" in text
    assert "COLLECTOR HOST" in text
    assert "NIC bytes differ from YWP CONTROL bytes" in text
    assert not state.handle_key("escape", snapshot)
    assert not state.handle_key("q", snapshot)


def test_demo_is_deterministic_valid_and_explicitly_synthetic() -> None:
    snapshot = demo_snapshot(24)
    assert snapshot == demo_snapshot(24)
    assert snapshot.authentication == "demo"
    assert Snapshot.from_body(snapshot.to_body()) == snapshot
    assert {device.kind for device in snapshot.devices} == {"hdmi", "usb"}
    assert all("demo" in device.name for device in snapshot.devices)
    assert snapshot.host is not None
    assert snapshot.host["scope"] == "collector-host"
    with pytest.raises(ValueError, match="finite"):
        demo_snapshot(float("nan"))


def test_control_counters_are_distinguished_from_nic_and_media() -> None:
    snapshot = demo_snapshot(24)
    lines = render(snapshot, width=100, height=24, state=ViewState())
    text = "\n".join(lines)
    assert "NETWORK / NIC" in text
    assert "CONTROL:" in text
    assert "events (observed)" in text
    assert "CONTROL != NIC/media" in text


class _TerminalError(Exception):
    pass


class _Screen:
    def __init__(self, keys: list[int], clock: list[float]) -> None:
        self.keys = iter(keys)
        self.clock = clock
        self.rows: dict[int, str] = {}
        self.frames: list[str] = []
        self.timeouts: list[int] = []

    def keypad(self, enabled: bool) -> None:
        assert enabled

    def getmaxyx(self) -> tuple[int, int]:
        return 24, 100

    def erase(self) -> None:
        self.rows.clear()

    def addnstr(self, row: int, column: int, text: str, count: int, attr: int) -> None:
        assert column == 0
        assert count <= (99 if row == 23 else 100)
        self.rows[row] = text[:count]

    def noutrefresh(self) -> None:
        self.frames.append("\n".join(self.rows[row] for row in sorted(self.rows)))

    def timeout(self, wait: int) -> None:
        assert 1 <= wait <= 1000
        self.timeouts.append(wait)

    def getch(self) -> int:
        self.clock[0] += 1
        return next(self.keys)


def _terminal() -> ModuleType:
    terminal = ModuleType("curses")
    values: dict[str, Any] = {
        "KEY_UP": 259,
        "KEY_DOWN": 258,
        "A_REVERSE": 1,
        "error": _TerminalError,
        "has_colors": lambda: False,
        "curs_set": lambda _: None,
        "doupdate": lambda: None,
    }
    for key, value in values.items():
        setattr(terminal, key, value)
    return terminal


def test_interactive_source_failure_retries_and_marks_cached_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    monkeypatch.setattr(tui.time, "monotonic", lambda: clock[0])
    screen = _Screen([-1, -1, ord("q")], clock)
    calls = 0

    def source() -> Snapshot:
        nonlocal calls
        calls += 1
        if calls in (1, 3):
            raise OSError("offline\x1b[H\nretrying")
        return demo_snapshot(clock[0])

    assert tui._run_screen(screen, _terminal(), source, 1.0) == 0  # type: ignore[arg-type]
    assert calls == 3
    assert "DISCONNECTED" in screen.frames[0]
    assert "SOURCE ERROR: offline?[H?retrying" in screen.frames[0]
    assert "DEMO" in screen.frames[1]
    assert "DISCONNECTED / CACHED" in screen.frames[2]
    assert "freshness unknown" in screen.frames[2]
    assert "\x1b" not in "".join(screen.frames)


def test_interactive_pause_keeps_collecting_without_duplicate_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    monkeypatch.setattr(tui.time, "monotonic", lambda: clock[0])
    screen = _Screen([ord(" "), -1, ord(" "), ord("q")], clock)
    states: list[ViewState] = []

    def new_state() -> ViewState:
        state = ViewState()
        states.append(state)
        return state

    monkeypatch.setattr(tui, "ViewState", new_state)
    calls: list[float] = []

    def source() -> Snapshot:
        calls.append(clock[0])
        return demo_snapshot(clock[0])

    assert tui._run_screen(screen, _terminal(), source, 1.0) == 0  # type: ignore[arg-type]
    assert calls == [0, 1, 2, 3]
    assert len(states[0].cpu_history) == 2
    assert "PAUSED" in screen.frames[1]
    assert "PAUSED" in screen.frames[2]


def test_successful_but_stalled_source_stays_cached_until_progress_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    monkeypatch.setattr(tui.time, "monotonic", lambda: clock[0])
    screen = _Screen([-1] * 8 + [ord("q")], clock)
    states: list[ViewState] = []

    def new_state() -> ViewState:
        state = ViewState()
        states.append(state)
        return state

    monkeypatch.setattr(tui, "ViewState", new_state)

    def source() -> Snapshot:
        if clock[0] == 6:
            raise OSError("connection refused")
        uptime = 600 if clock[0] < 7 else 601
        return replace(demo_snapshot(uptime), authentication="site-psk")

    assert tui._run_screen(screen, _terminal(), source, 1.0) == 0  # type: ignore[arg-type]
    assert "LIVE" in screen.frames[3]
    for frame in screen.frames[4:7]:
        assert "DISCONNECTED / CACHED" in frame
        assert "freshness unknown" in frame
        assert "SOURCE ERROR" in frame
        assert "LIVE" not in frame
    assert "snapshot has not advanced for 4.0s" in screen.frames[4]
    assert "snapshot has not advanced for 5.0s" in screen.frames[5]
    assert "connection refused" in screen.frames[6]
    assert "LIVE" in screen.frames[7]
    assert "SOURCE ERROR" not in screen.frames[7]
    assert len(states[0].cpu_history) == 2


def test_paused_display_still_checks_source_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    monkeypatch.setattr(tui.time, "monotonic", lambda: clock[0])
    screen = _Screen([ord(" "), -1, -1, -1, -1, ord(" "), ord("q")], clock)

    def source() -> Snapshot:
        uptime = 600 if clock[0] < 5 else 601 + clock[0] - 5
        return replace(demo_snapshot(uptime), authentication="site-psk")

    assert tui._run_screen(screen, _terminal(), source, 1.0) == 0  # type: ignore[arg-type]
    assert "PAUSED" in screen.frames[3]
    assert "DISCONNECTED / CACHED" in screen.frames[4]
    assert "freshness unknown" in screen.frames[4]
    assert "PAUSED" in screen.frames[5]
    assert "SOURCE ERROR" not in screen.frames[5]
    assert "LIVE" in screen.frames[6]


def test_interactive_restart_resets_source_progress_and_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    monkeypatch.setattr(tui.time, "monotonic", lambda: clock[0])
    screen = _Screen([-1, -1, -1, ord("q")], clock)
    states: list[ViewState] = []

    def new_state() -> ViewState:
        state = ViewState()
        states.append(state)
        return state

    monkeypatch.setattr(tui, "ViewState", new_state)

    def source() -> Snapshot:
        uptime = 600 + clock[0] if clock[0] < 2 else clock[0] - 2
        return replace(demo_snapshot(uptime), authentication="site-psk")

    assert tui._run_screen(screen, _terminal(), source, 1.0) == 0  # type: ignore[arg-type]
    assert len(states[0].cpu_history) == 2
    assert states[0]._last_sample == 1
    assert all("SOURCE ERROR" not in frame for frame in screen.frames)


def test_top_rejects_nonterminal_and_cleans_up_on_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with pytest.raises(RuntimeError, match="--once"):
        run_top(lambda: demo_snapshot(0))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    terminal = _terminal()
    restored = []

    def wrapper(*args: object) -> int:
        try:
            raise KeyboardInterrupt
        finally:
            restored.append(True)

    terminal.wrapper = wrapper  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "curses", terminal)
    assert run_top(lambda: demo_snapshot(0)) == 0
    assert restored == [True]


@pytest.mark.parametrize("interval", [0, -1, float("nan"), float("inf")])
def test_top_rejects_invalid_interval(interval: float) -> None:
    with pytest.raises(ValueError, match="interval"):
        run_top(lambda: demo_snapshot(0), interval)
