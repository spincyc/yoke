"""A small, ASCII-safe terminal monitor over read-only telemetry snapshots.

Rendering never samples or performs I/O. The interactive shell samples on its
own deadline; keyboard input and terminal resizes only redraw the current view.
"""

from __future__ import annotations

import math
import sys
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from types import ModuleType
from typing import TYPE_CHECKING

from yoke.telemetry.model import DeviceSnapshot, Snapshot

if TYPE_CHECKING:
    import curses

HISTORY_LENGTH = 120
SORT_KEYS = ("name", "type", "state", "age")
_SPARKS = " .:-=+*#%@"


def _safe(value: object) -> str:
    """Use one cell per character; neutralize escape, bidi and wide text."""
    return "".join(char if " " <= char <= "~" else "?" for char in str(value))


def _clip(value: object, width: int) -> str:
    text = _safe(value)
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    return text[: max(0, width - 1)] + "~"


def _number(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        result = float(value)
        return result if math.isfinite(result) else None
    return None


def _mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, dict):
        return value
    return {}


def _items(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        return []
    return [_mapping(item) for item in value]


def _percent(value: object) -> str:
    number = _number(value)
    return "unknown" if number is None else f"{100 * number:4.1f}%"


def _bytes(value: object) -> str:
    number = _number(value)
    if number is None:
        return "unknown"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(number) < 1024 or unit == "TiB":
            return f"{number:.0f}{unit}" if unit == "B" else f"{number:.1f}{unit}"
        number /= 1024
    raise AssertionError("unreachable")


def _age(value: object) -> str:
    number = _number(value)
    if number is None:
        return "?"
    if number < 60:
        return f"{max(0, number):.1f}s"
    if number < 3600:
        return f"{number / 60:.0f}m"
    return f"{number / 3600:.1f}h"


def _uptime(value: object) -> str:
    number = _number(value)
    if number is None:
        return "unknown"
    minutes = max(0, int(number)) // 60
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _network_rate(host: Mapping[str, object], key: str) -> float | None:
    values = [_number(item.get(key)) for item in _items(host.get("network"))]
    # Partial interface availability must not look like a complete host total.
    if not values or any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None)


@dataclass(frozen=True)
class SourceProgress:
    """The last source uptime advance, measured in the client's monotonic time."""

    uptime_seconds: float
    advanced_at: float


def source_progress(
    previous: SourceProgress | None, snapshot: Snapshot, *, now: float
) -> SourceProgress:
    """Check progress without I/O, even when HTTP keeps returning success.

    A lower uptime establishes a new source incarnation. The caller owns the
    monotonic clock; uptime belongs to the collector and is never compared with
    that clock directly. An unchanged sample tolerates three advertised sample
    periods, with at least three seconds for fast collectors.
    """
    if previous is None or snapshot.uptime_seconds != previous.uptime_seconds:
        return SourceProgress(snapshot.uptime_seconds, now)
    age = now - previous.advanced_at
    limit = max(3 * snapshot.sample_interval_seconds, 3.0)
    if age > limit:
        raise ValueError(f"snapshot has not advanced for {age:.1f}s; freshness unknown")
    return previous


@dataclass
class ViewState:
    """Interaction state and four bounded histories, independent of redraws."""

    selected_node_id: str | None = None
    sort_index: int = 0
    paused: bool = False
    show_help: bool = False
    source_error: str | None = None
    cpu_history: deque[float | None] = field(
        default_factory=lambda: deque(maxlen=HISTORY_LENGTH)
    )
    memory_history: deque[float | None] = field(
        default_factory=lambda: deque(maxlen=HISTORY_LENGTH)
    )
    rx_history: deque[float | None] = field(
        default_factory=lambda: deque(maxlen=HISTORY_LENGTH)
    )
    tx_history: deque[float | None] = field(
        default_factory=lambda: deque(maxlen=HISTORY_LENGTH)
    )
    _displayed: Snapshot | None = field(default=None, repr=False)
    _last_sample: float | None = field(default=None, repr=False)
    _history_reset_pending: bool = field(default=False, repr=False)

    @property
    def sort_key(self) -> str:
        return SORT_KEYS[self.sort_index % len(SORT_KEYS)]

    def display_snapshot(self, snapshot: Snapshot) -> Snapshot:
        if self.paused and self._displayed is not None:
            return self._displayed
        return snapshot

    def observe(self, snapshot: Snapshot) -> None:
        """Record a new sample once; a paused display retains its histories."""
        if self.paused:
            return
        if self._history_reset_pending or (
            self._last_sample is not None
            and snapshot.uptime_seconds < self._last_sample
        ):
            self.reset_history()
        self._displayed = snapshot
        if snapshot.uptime_seconds == self._last_sample:
            return
        self._last_sample = snapshot.uptime_seconds
        host = _mapping(snapshot.host)
        self.cpu_history.append(_number(_mapping(host.get("cpu")).get("busy_ratio")))
        self.memory_history.append(
            _number(_mapping(host.get("memory")).get("used_ratio"))
        )
        self.rx_history.append(_network_rate(host, "rx_bytes_per_second"))
        self.tx_history.append(_network_rate(host, "tx_bytes_per_second"))

    def reset_history(self) -> None:
        """Separate source incarnations while preserving a paused display."""
        if self.paused:
            self._history_reset_pending = True
            return
        for history in (
            self.cpu_history,
            self.memory_history,
            self.rx_history,
            self.tx_history,
        ):
            history.clear()
        self._last_sample = None
        self._history_reset_pending = False

    def ordered_devices(self, snapshot: Snapshot) -> list[DeviceSnapshot]:
        devices = snapshot.devices
        if self.sort_key == "type":
            return sorted(devices, key=lambda item: (item.kind, item.node_id))
        if self.sort_key == "state":
            return sorted(devices, key=lambda item: (item.state, item.node_id))
        if self.sort_key == "age":
            return sorted(
                devices,
                key=lambda item: (item.last_seen_age_seconds, item.node_id),
            )
        return sorted(
            devices, key=lambda item: (_safe(item.name).lower(), item.node_id)
        )

    def selected_device(self, snapshot: Snapshot) -> DeviceSnapshot | None:
        devices = self.ordered_devices(snapshot)
        return next(
            (item for item in devices if item.node_id == self.selected_node_id),
            devices[0] if devices else None,
        )

    def handle_key(self, key: str, snapshot: Snapshot) -> bool:
        """Apply normalized keys; return false when the operator quits."""
        if key in ("q", "Q", "escape"):
            return False
        if key == " ":
            self.paused = not self.paused
        elif key == "?":
            self.show_help = not self.show_help
        elif key == "s":
            selected = self.selected_device(self.display_snapshot(snapshot))
            if selected is not None:
                self.selected_node_id = selected.node_id
            self.sort_index = (self.sort_index + 1) % len(SORT_KEYS)
        elif key in ("up", "down", "j", "k"):
            devices = self.ordered_devices(self.display_snapshot(snapshot))
            selected = self.selected_device(self.display_snapshot(snapshot))
            if selected is not None:
                position = devices.index(selected)
                step = 1 if key in ("down", "j") else -1
                position = max(0, min(len(devices) - 1, position + step))
                self.selected_node_id = devices[position].node_id
        return True


class _Canvas:
    def __init__(self, width: int, height: int) -> None:
        self.width = max(0, width)
        self.height = max(0, height)
        self.rows = [" " * self.width for _ in range(self.height)]

    def put(self, x: int, y: int, value: object, width: int | None = None) -> None:
        if not 0 <= y < self.height or x < 0 or x >= self.width:
            return
        available = self.width - x
        text = _clip(value, available if width is None else min(width, available))
        self.rows[y] = self.rows[y][:x] + text + self.rows[y][x + len(text) :]

    def box(self, x: int, y: int, width: int, height: int, title: str) -> None:
        if width < 2 or height < 2:
            return
        self.put(x, y, "+" + "-" * (width - 2) + "+")
        self.put(x + 2, y, " " + _clip(title, width - 6) + " ", width - 4)
        for row in range(y + 1, y + height - 1):
            self.put(x, row, "|" + " " * (width - 2) + "|")
        self.put(x, y + height - 1, "+" + "-" * (width - 2) + "+")


def _spark(values: Sequence[float | None], width: int, ceiling: float = 1) -> str:
    if width <= 0:
        return ""
    recent = list(values)[-width:]
    scale = max(ceiling, max((value or 0 for value in recent), default=0), 0.0001)
    graph = "".join(
        "?"
        if value is None
        else _SPARKS[min(len(_SPARKS) - 1, max(0, int(value / scale * 9)))]
        for value in recent
    )
    return graph.rjust(width)


def _bar(value: object, width: int) -> str:
    number = _number(value)
    if number is None:
        return _clip("[ unknown ]", width)
    inside = max(0, width - 2)
    filled = min(inside, max(0, round(number * inside)))
    return "[" + "|" * filled + "." * (inside - filled) + "]"


def _history(values: deque[float | None], current: object) -> list[float | None]:
    return list(values) if values else [_number(current)]


def _host_panels(canvas: _Canvas, snapshot: Snapshot, state: ViewState) -> int:
    host = _mapping(snapshot.host)
    cpu = _mapping(host.get("cpu"))
    memory = _mapping(host.get("memory"))
    swap = _mapping(host.get("swap"))
    rx = _network_rate(host, "rx_bytes_per_second")
    tx = _network_rate(host, "tx_bytes_per_second")
    association = host.get("node_id") or "unassociated"
    cache = " / CACHED" if state.source_error else ""
    if canvas.width < 76 or canvas.height < 19:
        canvas.box(0, 1, canvas.width, 5, "COLLECTOR HOST" + cache)
        inner = canvas.width - 4
        canvas.put(
            2,
            2,
            f"CPU {_percent(cpu.get('busy_ratio'))}  "
            f"MEM {_percent(memory.get('used_ratio'))}",
            inner,
        )
        canvas.put(2, 3, f"NIC RX {_bytes(rx)}/s  TX {_bytes(tx)}/s", inner)
        canvas.put(
            2,
            4,
            "cpu "
            + _spark(
                _history(state.cpu_history, cpu.get("busy_ratio")), max(1, inner - 4)
            ),
            inner,
        )
        return 6

    canvas.put(0, 1, f"COLLECTOR HOST / {association}{cache}")
    panel_height = 8 if canvas.height >= 30 else 6
    first = (canvas.width - 2) // 3
    widths = (first, first, canvas.width - 2 * first - 2)
    x_positions = (0, first + 1, first * 2 + 2)
    for x, width, title in zip(
        x_positions, widths, ("CPU", "MEMORY", "NETWORK / NIC"), strict=True
    ):
        canvas.box(x, 2, width, panel_height, title)
    x, width = x_positions[0] + 2, widths[0] - 4
    canvas.put(x, 3, f"busy  {_percent(cpu.get('busy_ratio'))}", width)
    canvas.put(x, 4, _bar(cpu.get("busy_ratio"), width), width)
    canvas.put(
        x, 5, _spark(_history(state.cpu_history, cpu.get("busy_ratio")), width), width
    )
    load = _number(cpu.get("load1"))
    load_text = "?" if load is None else f"{load:.2f}"
    canvas.put(
        x, 6, f"cores {cpu.get('logical_count') or '?'} / load {load_text}", width
    )
    x, width = x_positions[1] + 2, widths[1] - 4
    canvas.put(x, 3, f"used  {_percent(memory.get('used_ratio'))}", width)
    canvas.put(x, 4, _bar(memory.get("used_ratio"), width), width)
    canvas.put(
        x,
        5,
        _spark(_history(state.memory_history, memory.get("used_ratio")), width),
        width,
    )
    canvas.put(
        x,
        6,
        f"{_bytes(memory.get('total_bytes'))} / "
        f"swap {_percent(swap.get('used_ratio'))}",
        width,
    )
    x, width = x_positions[2] + 2, widths[2] - 4
    canvas.put(x, 3, f"RX {_bytes(rx)}/s", width)
    canvas.put(
        x, 4, "rx " + _spark(_history(state.rx_history, rx), width - 3, 0), width
    )
    canvas.put(x, 5, f"TX {_bytes(tx)}/s", width)
    canvas.put(
        x, 6, "tx " + _spark(_history(state.tx_history, tx), width - 3, 0), width
    )
    if panel_height == 8:
        x, width = x_positions[0] + 2, widths[0] - 4
        temperatures = [
            _number(item.get("celsius")) for item in _items(host.get("temperatures"))
        ]
        known = [value for value in temperatures if value is not None]
        temperature = f"{max(known):.1f} C" if known else "unknown"
        canvas.put(x, 7, f"temp max {temperature}", width)
        canvas.put(x, 8, f"host up {_uptime(host.get('uptime_seconds'))}", width)
        x, width = x_positions[1] + 2, widths[1] - 4
        canvas.put(x, 7, f"free {_bytes(memory.get('available_bytes'))}", width)
        canvas.put(x, 8, f"swap total {_bytes(swap.get('total_bytes'))}", width)
        x, width = x_positions[2] + 2, widths[2] - 4
        interfaces = _items(host.get("network"))
        canvas.put(x, 7, f"{len(interfaces)} observed interfaces", width)
        errors = [
            _number(item.get(key))
            for item in interfaces
            for key in ("rx_errors", "tx_errors", "rx_dropped", "tx_dropped")
        ]
        total = (
            str(int(sum(value for value in errors if value is not None)))
            if errors and all(value is not None for value in errors)
            else "unknown"
        )
        canvas.put(x, 8, f"errors/drops {total}", width)
    return 2 + panel_height


def _fleet(
    canvas: _Canvas, snapshot: Snapshot, state: ViewState, y: int, height: int
) -> None:
    devices = state.ordered_devices(snapshot)
    live = sum(item.live for item in devices)
    freshness = "cached" if state.source_error else f"{live} live"
    title = f"ENDPOINTS / {len(devices)} observed / {freshness}"
    if canvas.width >= 76:
        title += f" / sort: {state.sort_key}"
    canvas.box(0, y, canvas.width, height, title)
    inside = canvas.width - 4
    if canvas.width < 60:
        node_width = max(6, min(14, inside - 25))
        headings = f"  {'NODE':<{node_width}} TYPE {'STATE':7} {'AGE':>4} PAIR"
    else:
        node_width = max(10, min(32, (inside - 29) // 2))
        headings = f"  {'NODE':<{node_width}} TYPE  STATE     AGE   PAIR"
    canvas.put(2, y + 1, headings, inside)
    if height < 4:
        return
    selected = state.selected_device(snapshot)
    capacity = height - 3
    position = devices.index(selected) if selected is not None else 0
    start = max(0, position - capacity + 1)
    for index, item in enumerate(devices[start : start + capacity]):
        marker = ">" if item == selected else " "
        name = _clip(item.node_id, node_width).ljust(node_width)
        status = "stale" if not item.live else item.state
        if state.source_error:
            status = item.state
        if canvas.width < 60:
            pair = item.pairing_producer or "none"
            row = (
                f"{marker} {name} {item.kind[:4]:4} {status:7} "
                f"{_age(item.last_seen_age_seconds):>4} "
                f"{_clip(pair, inside - node_width - 21)}"
            )
        else:
            pair = item.pairing_producer or (
                "source" if item.role == "producer" else "none"
            )
            row = (
                f"{marker} {name} {item.kind:5} {status:8} "
                f"{_age(item.last_seen_age_seconds):>5} {pair}"
            )
        canvas.put(2, y + 2 + index, row, inside)
    if not devices:
        canvas.put(2, y + 2, "Waiting for endpoint announcements", inside)
    if len(devices) > capacity and canvas.width >= 60:
        canvas.put(
            canvas.width - 21,
            y + height - 1,
            f" {start + 1}-{min(start + capacity, len(devices))}/{len(devices)} ",
            19,
        )


def _detail(
    canvas: _Canvas, snapshot: Snapshot, state: ViewState, y: int, height: int
) -> None:
    selected = state.selected_device(snapshot)
    if height < 4:
        value = (
            "Selected: none"
            if selected is None
            else f"Pair: {selected.node_id} -> {selected.pairing_producer or 'none'}"
        )
        canvas.put(0, y, value)
        return
    canvas.box(0, y, canvas.width, height, "SELECTED ENDPOINT")
    width = canvas.width - 4
    if selected is None:
        canvas.put(2, y + 1, "No endpoint selected", width)
        if state.source_error:
            canvas.put(2, y + 2, f"Source error: {state.source_error}", width)
        return
    item = selected
    canvas.put(2, y + 1, f"{item.node_id} / {item.name}", width)
    freshness = (
        "freshness unknown"
        if state.source_error
        else ("live" if item.live else "stale")
    )
    pair = item.pairing_producer or "none"
    canvas.put(
        2,
        y + 2,
        f"{item.kind} {item.role} / {item.state} / {freshness} / pair {pair}",
        width,
    )
    if height >= 5:
        event_count = sum(item.events.values())
        canvas.put(
            2,
            y + 3,
            f"CONTROL: {item.control_frames} frames / {_bytes(item.control_bytes)} / "
            f"{event_count} events (observed)",
            width,
        )
    if height >= 6:
        detail = (
            f"Source error: {state.source_error}"
            if state.source_error
            else f"Backend: {item.driver_detail or 'no detail announced'}"
        )
        canvas.put(2, y + 4, detail, width)
    if height >= 7:
        canvas.put(2, y + 5, "Backend state is not proof of media flow.", width)
    if height >= 8:
        canvas.put(
            2,
            y + 6,
            "Observed counters reset on collector restart or endpoint eviction.",
            width,
        )


def _help(canvas: _Canvas) -> None:
    lines = (
        "q / Esc       quit",
        "arrows / j k  select endpoint",
        "s             cycle name/type/state/age",
        "space         pause display; collection runs",
        "?             close help",
        "",
        "Host graphs describe the COLLECTOR HOST.",
        "NIC bytes differ from YWP CONTROL bytes.",
        "Active backend state does not prove media.",
    )
    width = min(canvas.width, 62)
    height = min(canvas.height, len(lines) + 2)
    x, y = (canvas.width - width) // 2, (canvas.height - height) // 2
    canvas.box(x, y, width, height, "KEYS / OBSERVATION SCOPE")
    for index, line in enumerate(lines[: max(0, height - 2)]):
        canvas.put(x + 2, y + index + 1, line, width - 4)


def render(
    snapshot: Snapshot, *, width: int, height: int, state: ViewState
) -> list[str]:
    """Return an exact-size printable-ASCII canvas without mutating state."""
    canvas = _Canvas(width, height)
    if width < 40 or height < 12:
        canvas.put(0, 0, "Yoke / terminal too small")
        canvas.put(0, 1, "Use at least 40 columns x 12 rows")
        canvas.put(0, 2, "q quit / resize to continue")
        return canvas.rows
    snapshot = state.display_snapshot(snapshot)
    mode = (
        "DISCONNECTED / CACHED"
        if state.source_error
        else ("PAUSED" if state.paused else "LIVE")
    )
    if snapshot.authentication == "demo":
        authentication = "synthetic data"
        if not state.source_error:
            mode = "DEMO / PAUSED" if state.paused else "DEMO"
    else:
        authentication = (
            "site PSK"
            if snapshot.authentication == "site-psk"
            else "OPEN / unauthenticated"
        )
    if state.source_error and state._displayed is None:
        authentication = "auth unknown"
    if width >= 76:
        canvas.put(
            0,
            0,
            f"Yoke / telemetry   {mode}   up {_uptime(snapshot.uptime_seconds)}   "
            f"{authentication}",
        )
    else:
        auth_short = (
            ""
            if snapshot.authentication == "demo"
            else ("PSK / " if snapshot.authentication == "site-psk" else "OPEN / ")
        )
        if state.source_error and state._displayed is None:
            auth_short = ""
        canvas.put(
            0,
            0,
            f"Yoke / {mode} / {auth_short}up {_uptime(snapshot.uptime_seconds)}",
        )
    fleet_y = _host_panels(canvas, snapshot, state)
    remaining = height - fleet_y - 1
    detail_height = (
        8
        if remaining >= 20
        else (6 if remaining >= 12 else (4 if remaining >= 9 else 1))
    )
    fleet_height = remaining - detail_height
    _fleet(canvas, snapshot, state, fleet_y, fleet_height)
    _detail(canvas, snapshot, state, fleet_y + fleet_height, detail_height)
    if state.source_error:
        footer = f"SOURCE ERROR: {state.source_error} / q quit"
    elif state.paused:
        footer = (
            "PAUSED display / collection continues / space resume / ? help / q quit"
        )
    elif width >= 76:
        footer = (
            "q quit   arrows/jk select   s sort   space pause   ? help   "
            "CONTROL != NIC/media"
        )
    else:
        footer = "q quit  jk select  s sort  space pause  ?"
    canvas.put(0, height - 1, footer)
    if state.show_help:
        _help(canvas)
    return canvas.rows


def _palette(terminal: ModuleType) -> tuple[int, int, int]:
    """Muted green/cyan/neutral accents; visible words also carry all meaning."""
    try:
        if not terminal.has_colors():
            return 0, 0, terminal.A_REVERSE
        terminal.start_color()
        background = -1
        try:
            terminal.use_default_colors()
        except terminal.error:
            background = terminal.COLOR_BLACK
        colors = terminal.COLORS
        terminal.init_pair(
            1, 108 if colors >= 256 else terminal.COLOR_GREEN, background
        )
        terminal.init_pair(2, 109 if colors >= 256 else terminal.COLOR_CYAN, background)
        return terminal.color_pair(1), terminal.color_pair(2), terminal.A_REVERSE
    except terminal.error:
        return 0, 0, terminal.A_REVERSE


def _run_screen(
    screen: curses.window,
    terminal: ModuleType,
    get_snapshot: Callable[[], Snapshot],
    interval: float,
) -> int:
    state = ViewState()
    latest = Snapshot(0.0, "open-unauthenticated", (), {}, None)
    progress: SourceProgress | None = None
    next_sample = 0.0
    green, cyan, selection = _palette(terminal)
    screen.keypad(True)
    with suppress(terminal.error):
        terminal.curs_set(0)
    while True:
        now = time.monotonic()
        if now >= next_sample:
            try:
                candidate = get_snapshot()
                checked_progress = source_progress(
                    progress, candidate, now=time.monotonic()
                )
            except (OSError, ValueError) as error:
                state.source_error = _safe(error)
            else:
                if (
                    progress is not None
                    and checked_progress.uptime_seconds < progress.uptime_seconds
                ):
                    state.reset_history()
                progress = checked_progress
                latest = candidate
                state.source_error = None
                state.observe(latest)
            next_sample = time.monotonic() + interval
        height, width = screen.getmaxyx()
        frame = render(latest, width=width, height=height, state=state)
        try:
            screen.erase()
            for row, line in enumerate(frame):
                # Writing the lower-right cell can raise even when it succeeds.
                limit = max(0, width - 1) if row == height - 1 else width
                if limit == 0:
                    continue
                attribute = 0
                if "COLLECTOR HOST" in line or "ENDPOINTS /" in line:
                    attribute = green
                elif "SELECTED ENDPOINT" in line or "NETWORK / NIC" in line:
                    attribute = cyan
                if line.startswith("| >"):
                    attribute = selection
                screen.addnstr(row, 0, line, limit, attribute)
            screen.noutrefresh()
            terminal.doupdate()
        except terminal.error:
            # The size can change between reading it and updating the screen.
            pass
        wait_ms = max(1, min(1000, math.ceil((next_sample - time.monotonic()) * 1000)))
        screen.timeout(wait_ms)
        key = screen.getch()
        normalized = {
            terminal.KEY_UP: "up",
            terminal.KEY_DOWN: "down",
            27: "escape",
        }.get(key)
        if normalized is None and 0 <= key < 128:
            normalized = chr(key)
        if normalized is not None and not state.handle_key(normalized, latest):
            return 0


def run_top(get_snapshot: Callable[[], Snapshot], interval: float = 1.0) -> int:
    """Run a curses session, always restoring the terminal on exit or failure."""
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("sample interval must be positive and finite")
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RuntimeError("interactive top needs a terminal; use --once")
    try:
        import curses
    except ImportError as error:
        raise RuntimeError("this Python has no curses support; use --once") from error
    try:
        return curses.wrapper(_run_screen, curses, get_snapshot, interval)
    except KeyboardInterrupt:
        return 0
    except curses.error as error:
        raise RuntimeError(
            f"cannot initialize terminal: {_safe(error)}; use --once"
        ) from error
