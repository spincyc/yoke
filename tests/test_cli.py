"""Tests for ``yoke.cli`` driven entirely by scripted fake IO.

``FakeNet`` scripts absolute-time datagram deliveries against a fake
clock and records sends; nothing here opens a socket, sleeps, or reads
the wall clock.
"""

from __future__ import annotations

import json
import sys
import types
from collections import deque
from pathlib import Path
from typing import Any

import pytest

from yoke.cli import (
    IntentIO,
    build_parser,
    default_origin,
    main,
    render_status_table,
    resolve_psk,
    run_intent,
    run_snoop,
    run_status,
)
from yoke.core.frames import decode, encode
from yoke.core.types import NODE_ID_RE, FrameType
from yoke.snoop import NodeView

PSK = b"cli-site-secret-0123"
ORIGIN = "cli-test"
TS = 1_755_000_000.0
START = 1000.0


def ann_producer(
    origin: str = "cam-1",
    seq: int = 1,
    *,
    kind: str = "hdmi",
    state: str = "active",
    name: str = "Rack cam",
    psk: bytes = PSK,
) -> bytes:
    body: dict[str, Any] = {
        "role": "producer",
        "kind": kind,
        "name": name,
        "state": state,
        "err": None,
        "pairing": None,
        "media": {"group": "239.10.0.1", "port": 5004},
    }
    return encode(FrameType.ANN, origin, seq, TS, body, psk)


def ann_consumer(
    origin: str = "tv-den",
    seq: int = 1,
    *,
    producer: str | None = None,
    version: tuple[int, str] = (0, ""),
    kind: str = "hdmi",
    state: str = "idle",
    err: str | None = None,
    name: str = "Den TV",
    psk: bytes = PSK,
) -> bytes:
    body: dict[str, Any] = {
        "role": "consumer",
        "kind": kind,
        "name": name,
        "state": state,
        "err": err,
        "pairing": {"producer": producer, "version": [version[0], version[1]]},
        "media": None,
    }
    return encode(FrameType.ANN, origin, seq, TS, body, psk)


def ack(
    consumer: str = "tv-den",
    version: tuple[int, str] = (1, ORIGIN),
    *,
    applied: bool = True,
    reason: str = "applied",
    seq: int = 2,
    psk: bytes = PSK,
) -> bytes:
    body: dict[str, Any] = {
        "version": [version[0], version[1]],
        "applied": applied,
        "reason": reason,
    }
    return encode(FrameType.ACK, consumer, seq, TS, body, psk)


class FakeNet:
    """Scripted datagram world: (absolute time, raw) deliveries, a fake
    clock that jumps to whatever is waited on, and recorded sends."""

    def __init__(self, script: list[tuple[float, bytes]], start: float = START) -> None:
        self.now = start
        self.script = deque(script)
        self.sends: list[tuple[float, bytes]] = []
        self._counter = 0

    def clock(self) -> float:
        return self.now

    def send(self, raw: bytes) -> None:
        self.sends.append((self.now, raw))

    def seq(self) -> int:
        self._counter += 1
        return int(self.now * 1_000_000_000) + self._counter

    def recv(self, deadline: float) -> tuple[bytes, float] | None:
        if self.script and self.script[0][0] <= deadline:
            when, raw = self.script.popleft()
            self.now = max(self.now, when)
            return raw, self.now
        self.now = max(self.now, deadline)
        return None

    def io(self, psk: bytes = PSK, origin: str = ORIGIN) -> IntentIO:
        return IntentIO(
            send=self.send,
            recv=self.recv,
            clock=self.clock,
            seq=self.seq,
            psk=psk,
            origin=origin,
        )


def sent_bodies(net: FakeNet) -> list[dict[str, Any]]:
    return [decode(raw, PSK).body for _, raw in net.sends]


# -- run_intent: pair --------------------------------------------------------


def test_pair_applied_ack_success() -> None:
    net = FakeNet(
        [
            (1000.5, ann_consumer(version=(4, "prev-cli"))),
            (1001.1, ack(version=(5, ORIGIN), seq=2)),
        ]
    )
    outcome = run_intent(net.io(), "pair", "tv-den", "cam-1")
    assert outcome.exit_code == 0
    assert outcome.sent_version == (5, ORIGIN)
    assert outcome.sends == 2
    assert outcome.consumer_announced is True
    assert len(outcome.out) == 1
    assert "applied" in outcome.out[0]
    assert "[5,cli-test]" in outcome.out[0]
    # Announce showed [4, x] -> the intent carries [5, our origin].
    first = sent_bodies(net)[0]
    assert first == {
        "op": "pair",
        "consumer": "tv-den",
        "producer": "cam-1",
        "version": [5, ORIGIN],
    }


def test_pair_rejected_ack_surfaces_reason() -> None:
    net = FakeNet(
        [
            (1000.2, ann_consumer()),
            (1000.6, ack(version=(1, ORIGIN), applied=False, reason="kind-mismatch")),
        ]
    )
    outcome = run_intent(net.io(), "pair", "tv-den", "cam-1")
    assert outcome.exit_code == 2
    assert outcome.sends == 1
    assert any("kind-mismatch" in line for line in outcome.err)


def test_no_ack_deadline_consumer_announced() -> None:
    net = FakeNet([(1000.5, ann_consumer())])
    outcome = run_intent(net.io(), "pair", "tv-den", "cam-1")
    assert outcome.exit_code == 1
    assert outcome.sends == 5
    assert outcome.consumer_announced is True
    assert any("announced but no ack" in line for line in outcome.err)
    assert not any("never announced" in line for line in outcome.err)
    # Retransmission schedule: 5 sends, 0.5 s doubling spacing from t0.
    assert [when for when, _ in net.sends] == pytest.approx(
        [1000.5, 1001.0, 1002.0, 1004.0, 1008.0]
    )
    assert net.clock() == pytest.approx(1010.5)  # CLI_DEADLINE after t0


def test_no_ack_deadline_consumer_never_announced() -> None:
    net = FakeNet([])
    outcome = run_intent(net.io(), "pair", "tv-den", "cam-1")
    assert outcome.exit_code == 1
    assert outcome.sends == 5
    assert outcome.consumer_announced is False
    assert any("never announced" in line for line in outcome.err)
    assert any("version 0" in line for line in outcome.err)
    # Unknown consumer -> version [1, origin]; sends start after the window.
    assert sent_bodies(net)[0]["version"] == [1, ORIGIN]
    assert net.sends[0][0] == pytest.approx(1002.5)


def test_ack_from_wrong_node_or_wrong_version_ignored() -> None:
    net = FakeNet(
        [
            (1000.2, ann_consumer(version=(4, "old"))),
            (1000.8, ack(consumer="impostor-1", version=(5, ORIGIN), seq=1)),
            (1000.9, ack(version=(9, "zzz"), seq=2)),
            (1001.2, ack(version=(5, ORIGIN), seq=3)),
        ]
    )
    outcome = run_intent(net.io(), "pair", "tv-den", "cam-1")
    assert outcome.exit_code == 0
    assert outcome.sent_version == (5, ORIGIN)
    assert outcome.sends == 2  # the bogus acks did not resolve the intent


def test_target_consumer_that_is_a_producer_refused_before_sending() -> None:
    # Regression from the live end-to-end smoke: `yoke pair cam-1 cam-1`
    # named a producer as the consumer; producers never ack, so the CLI
    # burned the full deadline. It must refuse fast instead.
    net = FakeNet([(1000.1, ann_producer())])
    outcome = run_intent(net.io(), "pair", "cam-1", "cam-1")
    assert outcome.exit_code == 2
    assert net.sends == []
    assert any("not a consumer" in line for line in outcome.err)


def test_named_producer_that_is_a_consumer_refused_before_sending() -> None:
    # tv-two must announce before the target consumer: collection stops
    # early once the target is seen, so the check is best-effort on what
    # was actually observed.
    net = FakeNet(
        [
            (1000.1, ann_consumer(origin="tv-two", name="Other TV")),
            (1000.2, ann_consumer()),
        ]
    )
    outcome = run_intent(net.io(), "pair", "tv-den", "tv-two")
    assert outcome.exit_code == 2
    assert net.sends == []
    assert any("not a producer" in line for line in outcome.err)


def test_kind_mismatch_precheck_refuses_before_sending() -> None:
    net = FakeNet(
        [
            (1000.1, ann_producer(kind="usb")),
            (1000.2, ann_consumer(kind="hdmi")),
        ]
    )
    outcome = run_intent(net.io(), "pair", "tv-den", "cam-1")
    assert outcome.exit_code == 2
    assert net.sends == []
    assert any("kind mismatch" in line for line in outcome.err)


def test_malformed_datagrams_during_collection_are_skipped() -> None:
    net = FakeNet(
        [
            (1000.10, b"\x00garbage not a frame"),
            (1000.15, b"x" * 1300),
            (1000.20, ann_consumer()),
            (1000.40, ack(version=(1, ORIGIN))),
        ]
    )
    outcome = run_intent(net.io(), "pair", "tv-den", "cam-1")
    assert outcome.exit_code == 0
    assert outcome.sent_version == (1, ORIGIN)


def test_error_state_consumer_is_still_valid_target() -> None:
    net = FakeNet(
        [
            (1000.2, ann_consumer(state="error", err="driver down", version=(2, "c"))),
            (1000.6, ack(version=(3, ORIGIN))),
        ]
    )
    outcome = run_intent(net.io(), "pair", "tv-den", "cam-1")
    assert outcome.exit_code == 0
    assert outcome.sent_version == (3, ORIGIN)


@pytest.mark.parametrize(
    ("op", "consumer", "producer"),
    [
        ("pair", "BAD", "cam-1"),
        ("pair", "tv-den", "Bad_Prod"),
        ("pair", "-lead", "cam-1"),
        ("unpair", "UPPER", None),
    ],
)
def test_bad_node_ids_exit_2_with_no_sends(
    op: str, consumer: str, producer: str | None
) -> None:
    net = FakeNet([(1000.1, ann_consumer())])
    outcome = run_intent(net.io(), op, consumer, producer)
    assert outcome.exit_code == 2
    assert net.sends == []
    assert outcome.sends == 0
    assert any("invalid" in line for line in outcome.err)


# -- run_intent: unpair ------------------------------------------------------


def test_unpair_flow_omits_producer_and_bumps_version() -> None:
    net = FakeNet(
        [
            (1000.3, ann_consumer(producer="cam-1", version=(3, "old-cli"))),
            (1000.9, ack(version=(4, ORIGIN))),
        ]
    )
    outcome = run_intent(net.io(), "unpair", "tv-den")
    assert outcome.exit_code == 0
    assert outcome.sent_version == (4, ORIGIN)
    body = sent_bodies(net)[0]
    assert body == {"op": "unpair", "consumer": "tv-den", "version": [4, ORIGIN]}
    assert "producer" not in body


# -- status ------------------------------------------------------------------


def test_status_table_rendering() -> None:
    net = FakeNet(
        [
            (1000.2, ann_producer()),
            (
                1000.4,
                ann_consumer(producer="cam-1", version=(3, "cli-old"), state="active"),
            ),
        ]
    )
    code, table = run_status(net.recv, net.clock, PSK, 2.5)
    assert code == 0
    lines = table.splitlines()
    header = lines[0].split()
    assert header == ["ID", "ROLE", "KIND", "STATE", "PAIRING", "NAME"]
    cam_row = next(line for line in lines if line.startswith("cam-1"))
    assert "producer" in cam_row
    assert "hdmi" in cam_row
    assert "active" in cam_row
    tv_row = next(line for line in lines if line.startswith("tv-den"))
    assert "consumer" in tv_row
    assert "tv-den->cam-1@3" in tv_row
    assert "Den TV" in tv_row
    assert net.clock() == pytest.approx(1002.5)  # full window, no early exit


def test_status_table_flags_stale_and_error() -> None:
    view = NodeView(
        node_id="tv-den",
        role="consumer",
        kind="hdmi",
        name="Den TV",
        state="error",
        err="hdmi-lost",
        pairing_producer="cam-1",
        pairing_version=(2, "c"),
        media=None,
        last_seen=1000.0,
    )
    table = render_status_table({"tv-den": view}, now=1010.0)
    row = table.splitlines()[1]
    assert "stale" in row
    assert "err:hdmi-lost" in row


def test_status_table_empty() -> None:
    assert "(no nodes announced)" in render_status_table({}, now=0.0)


# -- snoop wiring ------------------------------------------------------------


def make_recv(raws: list[bytes], *, interrupt: bool) -> Any:
    items = iter(raws)

    def recv() -> tuple[bytes, float] | None:
        try:
            return next(items), START
        except StopIteration:
            if interrupt:
                raise KeyboardInterrupt from None
            return None

    return recv


def test_run_snoop_json_lines_and_topology() -> None:
    out: list[str] = []
    err: list[str] = []
    recv = make_recv([ann_producer(), ann_consumer(), b"junk"], interrupt=False)
    code = run_snoop(recv, PSK, True, out.append, err.append)
    assert code == 0
    assert len(out) == 3
    for line in out:
        assert "\n" not in line
        obj = json.loads(line)
        assert line == json.dumps(
            obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
    topo = json.loads(err[-1])
    assert "cam-1" in topo["producers"]
    assert "tv-den" in topo["consumers"]
    assert topo["counts"]["malformed"] == 1


def test_run_snoop_sigint_exits_cleanly_with_summary() -> None:
    out: list[str] = []
    err: list[str] = []
    recv = make_recv([ann_producer()], interrupt=True)
    code = run_snoop(recv, PSK, False, out.append, err.append)
    assert code == 0
    assert len(out) == 1
    assert "cam-1" in out[0]
    assert "cam-1" in json.loads(err[-1])["producers"]


# -- agent subcommand --------------------------------------------------------


def write_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "agent.toml"
    state = (tmp_path / "state.json").as_posix()
    cfg.write_text(
        'node_id = "tv-den"\n'
        'role = "consumer"\n'
        'kind = "hdmi"\n'
        f'state_file = "{state}"\n'
    )
    return cfg


def test_agent_runner_missing_falls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = write_config(tmp_path)
    # None in sys.modules forces ImportError even after runner.py lands.
    monkeypatch.setitem(sys.modules, "yoke.net.runner", None)
    assert main(["agent", "--config", str(cfg)]) == 3
    assert "agent runner not yet available" in capsys.readouterr().err


def test_agent_runs_fake_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = write_config(tmp_path)
    seen: list[Any] = []
    fake = types.ModuleType("yoke.net.runner")

    def run_agent(agent_cfg: Any) -> int:
        seen.append(agent_cfg)
        return 0

    fake.run_agent = run_agent  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "yoke.net.runner", fake)
    assert main(["agent", "--config", str(cfg)]) == 0
    assert len(seen) == 1
    assert seen[0].node.node_id == "tv-den"
    assert seen[0].psk == b""


def test_agent_bad_config_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["agent", "--config", str(tmp_path / "missing.toml")]) == 2
    assert "not found" in capsys.readouterr().err


# -- main-level validation (no socket is ever opened on these paths) ---------


def test_main_bad_producer_id_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    psk_file = tmp_path / "psk"
    psk_file.write_bytes(b"secret")
    code = main(["--psk-file", str(psk_file), "pair", "BAD_ID", "tv-den"])
    assert code == 2
    assert "invalid producer id" in capsys.readouterr().err


def test_main_bad_origin_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    psk_file = tmp_path / "psk"
    psk_file.write_bytes(b"secret")
    code = main(
        [
            "--psk-file",
            str(psk_file),
            "--origin",
            "Bad Origin",
            "pair",
            "cam-1",
            "tv-den",
        ]
    )
    assert code == 2
    assert "invalid origin id" in capsys.readouterr().err


def test_main_unreadable_psk_file_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["--psk-file", str(tmp_path / "nope"), "pair", "cam-1", "tv-den"])
    assert code == 2
    assert "cannot read PSK file" in capsys.readouterr().err


# -- defaults and parsing ----------------------------------------------------


def test_default_origin_sanitizes_hostname() -> None:
    assert default_origin("My-Host.local") == "cli-my-host-local"
    assert default_origin("box9") == "cli-box9"
    long = default_origin("a" * 64)
    assert len(long) == 32
    assert NODE_ID_RE.match(long)
    assert NODE_ID_RE.match(default_origin(""))
    assert NODE_ID_RE.match(default_origin("Ünïcode Høst!"))


def test_resolve_psk_explicit_file(tmp_path: Path) -> None:
    psk_file = tmp_path / "psk"
    psk_file.write_bytes(b"secret-key\n")
    psk, warning = resolve_psk(str(psk_file))
    assert psk == b"secret-key"
    assert warning is None


def test_resolve_psk_default_present(tmp_path: Path) -> None:
    default = tmp_path / "psk"
    default.write_bytes(b"site-key")
    psk, warning = resolve_psk(None, default_path=default)
    assert psk == b"site-key"
    assert warning is None


def test_resolve_psk_default_missing_is_open_mode(tmp_path: Path) -> None:
    psk, warning = resolve_psk(None, default_path=tmp_path / "absent")
    assert psk == b""
    assert warning is not None
    assert "open mode" in warning


def test_resolve_psk_explicit_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(OSError, match="nope"):
        resolve_psk(str(tmp_path / "nope"))


def test_parser_defaults_and_watch() -> None:
    args = build_parser().parse_args(["status", "--watch", "5"])
    assert args.watch == 5.0
    assert args.group == "239.121.111.107"
    assert args.port == 9753
    assert args.psk_file is None
    snoop_args = build_parser().parse_args(["snoop", "--json"])
    assert snoop_args.json is True
    pair_args = build_parser().parse_args(["pair", "cam-1", "tv-den"])
    assert pair_args.producer == "cam-1"
    assert pair_args.consumer == "tv-den"
