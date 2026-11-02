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
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from yoke.cli import (
    IntentIO,
    build_parser,
    default_origin,
    main,
    new_operation_author,
    render_status_table,
    resolve_psk,
    run_intent,
    run_snoop,
    run_status,
)
from yoke.core.frames import decode, encode
from yoke.core.node import Node
from yoke.core.types import (
    NODE_ID_RE,
    PEER_TIMEOUT,
    Body,
    Effect,
    FrameType,
    Kind,
    NodeConfig,
    Persist,
    PersistedState,
    Role,
    SendFrame,
)
from yoke.snoop import NodeView

PSK = b"cli-site-secret-0123"
ORIGIN = "cli-test"
AUTHOR = "op-test"
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
    version: tuple[int, str] = (1, AUTHOR),
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
            operation_author=lambda: AUTHOR,
        )


def sent_bodies(net: FakeNet) -> list[dict[str, Any]]:
    return [decode(raw, PSK).body for _, raw in net.sends]


# -- run_intent: pair --------------------------------------------------------


def test_pair_applied_ack_success() -> None:
    net = FakeNet(
        [
            (1000.5, ann_consumer(version=(4, "prev-cli"))),
            (1001.1, ack(version=(5, AUTHOR), seq=2)),
        ]
    )
    outcome = run_intent(net.io(), "pair", "tv-den", "cam-1")
    assert outcome.exit_code == 0
    assert outcome.sent_version == (5, AUTHOR)
    assert outcome.sends == 2
    assert outcome.consumer_announced is True
    assert len(outcome.out) == 1
    assert "applied" in outcome.out[0]
    assert "[5,op-test]" in outcome.out[0]
    # Announce showed [4, x] -> the intent carries [5, our origin].
    first = sent_bodies(net)[0]
    assert first == {
        "op": "pair",
        "consumer": "tv-den",
        "producer": "cam-1",
        "version": [5, AUTHOR],
    }


def test_pair_rejected_ack_surfaces_reason() -> None:
    net = FakeNet(
        [
            (1000.2, ann_consumer()),
            (1000.6, ack(version=(1, AUTHOR), applied=False, reason="kind-mismatch")),
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
    assert sent_bodies(net)[0]["version"] == [1, AUTHOR]
    assert net.sends[0][0] == pytest.approx(1002.5)


def test_ack_from_wrong_node_or_wrong_version_ignored() -> None:
    net = FakeNet(
        [
            (1000.2, ann_consumer(version=(4, "old"))),
            (1000.8, ack(consumer="impostor-1", version=(5, AUTHOR), seq=1)),
            (1000.9, ack(version=(9, "zzz"), seq=2)),
            (1001.2, ack(version=(5, AUTHOR), seq=3)),
        ]
    )
    outcome = run_intent(net.io(), "pair", "tv-den", "cam-1")
    assert outcome.exit_code == 0
    assert outcome.sent_version == (5, AUTHOR)
    assert outcome.sends == 2  # the bogus acks did not resolve the intent


def test_target_consumer_that_is_a_producer_refused_before_sending() -> None:
    # Regression from the live end-to-end smoke: `yoke pair cam-1 cam-1`
    # named a producer as the consumer; producers never ack, so the CLI
    # burned the full deadline. It must refuse fast instead.
    net = FakeNet([(1000.1, ann_producer())])
    outcome = run_intent(net.io(), "pair", "cam-1", "cam-2")
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
            (1000.40, ack(version=(1, AUTHOR))),
        ]
    )
    outcome = run_intent(net.io(), "pair", "tv-den", "cam-1")
    assert outcome.exit_code == 0
    assert outcome.sent_version == (1, AUTHOR)


def test_error_state_consumer_is_still_valid_target() -> None:
    net = FakeNet(
        [
            (1000.2, ann_consumer(state="error", err="driver down", version=(2, "c"))),
            (1000.6, ack(version=(3, AUTHOR))),
        ]
    )
    outcome = run_intent(net.io(), "pair", "tv-den", "cam-1")
    assert outcome.exit_code == 0
    assert outcome.sent_version == (3, AUTHOR)


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
            (1000.9, ack(version=(4, AUTHOR))),
        ]
    )
    outcome = run_intent(net.io(), "unpair", "tv-den")
    assert outcome.exit_code == 0
    assert outcome.sent_version == (4, AUTHOR)
    body = sent_bodies(net)[0]
    assert body == {"op": "unpair", "consumer": "tv-den", "version": [4, AUTHOR]}
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
    code = run_snoop(recv, PSK, True, out.append, err.append, clock=lambda: START)
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
    code = run_snoop(recv, PSK, False, out.append, err.append, clock=lambda: START)
    assert code == 0
    assert len(out) == 1
    assert "cam-1" in out[0]
    assert "cam-1" in json.loads(err[-1])["producers"]


def test_run_snoop_final_snapshot_expires_peers_after_silent_eof() -> None:
    net = FakeNet([(START, ann_producer())])
    out: list[str] = []
    err: list[str] = []

    def recv() -> tuple[bytes, float] | None:
        return net.recv(START + PEER_TIMEOUT + 0.1)

    assert run_snoop(recv, PSK, True, out.append, err.append, clock=net.clock) == 0
    assert len(out) == 1
    topology = json.loads(err[-1])
    assert topology["producers"]["cam-1"]["live"] is False
    assert topology["counts"]["ok"] == 1
    assert net.sends == []


def test_main_snoop_sigint_after_group_silence_expires_final_topology(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    net = FakeNet([(START, ann_producer())])

    class SilentSocket:
        closed = False

        def settimeout(self, timeout: float | None) -> None:
            pass

        def recvfrom(self, size: int) -> tuple[bytes, tuple[str, int]]:
            item = net.recv(START + PEER_TIMEOUT + 0.1)
            if item is None:
                raise KeyboardInterrupt
            raw, _ = item
            return raw, ("127.0.0.1", 9753)

        def sendto(self, *args: object) -> None:
            raise AssertionError("snooping transmitted a control datagram")

        def close(self) -> None:
            self.closed = True

    sock = SilentSocket()
    monkeypatch.setattr("yoke.cli.open_multicast_socket", lambda *a, **kw: sock)
    monkeypatch.setattr("yoke.cli.resolve_psk", lambda *a: (PSK, None))
    monkeypatch.setattr("yoke.cli.time.monotonic", net.clock)
    assert main(["snoop", "--json"]) == 0
    captured = capsys.readouterr()
    assert len(captured.out.splitlines()) == 1
    assert json.loads(captured.out)["verdict"] == "ok"
    topology = json.loads(captured.err)
    assert topology["producers"]["cam-1"]["live"] is False
    assert topology["counts"]["ok"] == 1
    assert sock.closed


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
    psk_file.chmod(0o600)
    code = main(["--psk-file", str(psk_file), "pair", "BAD_ID", "tv-den"])
    assert code == 2
    assert "invalid producer id" in capsys.readouterr().err


def test_main_bad_origin_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    psk_file = tmp_path / "psk"
    psk_file.write_bytes(b"secret")
    psk_file.chmod(0o600)
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
    psk_file.chmod(0o600)
    psk, warning = resolve_psk(str(psk_file))
    assert psk == b"secret-key"
    assert warning is None


def test_resolve_psk_default_present(tmp_path: Path) -> None:
    default = tmp_path / "psk"
    default.write_bytes(b"site-key")
    default.chmod(0o600)
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


@pytest.mark.parametrize(
    "consumer,producer,origin",
    [
        ("tv-den", "tv-den", ORIGIN),
        ("tv-den", "cam-1", "tv-den"),
        ("tv-den", "cam-1", "cam-1"),
        ("tv-den\n", "cam-1", ORIGIN),
        ("tv-den", "cam-1", "cli-test\n"),
    ],
)
def test_identity_conflicts_refuse_without_receiving(
    consumer: str, producer: str, origin: str
) -> None:
    net = FakeNet([])
    outcome = run_intent(net.io(origin=origin), "pair", consumer, producer)
    assert outcome.exit_code == 2
    assert net.now == START
    assert net.sends == []


def test_pair_version_exhaustion_is_actionable() -> None:
    net = FakeNet([(1000.1, ann_consumer(version=((1 << 63) - 1, "old")))])
    outcome = run_intent(net.io(), "pair", "tv-den", "cam-1")
    assert outcome.exit_code == 2
    assert outcome.err == ("consumer pairing version is exhausted",)
    assert net.sends == []


def test_applied_ack_reports_commit_without_claiming_media_active() -> None:
    net = FakeNet([(1000.1, ann_consumer()), (1000.2, ack())])
    outcome = run_intent(net.io(), "pair", "tv-den", "cam-1")
    assert outcome.committed
    assert outcome.activation == "unconfirmed"
    assert "committed" in outcome.out[0]
    assert "activation unconfirmed" in outcome.out[0]


def test_matching_announce_reports_separate_activation_state() -> None:
    net = FakeNet(
        [
            (1000.1, ann_consumer()),
            (
                1000.2,
                ann_consumer(
                    seq=2, producer="cam-1", version=(1, AUTHOR), state="pending"
                ),
            ),
            (1000.3, ack(seq=3)),
        ]
    )
    outcome = run_intent(net.io(), "pair", "tv-den", "cam-1")
    assert outcome.committed
    assert outcome.activation == "pending"


def test_status_json_is_stable_and_marks_lab_mode() -> None:
    net = FakeNet([(1000.1, ann_consumer(psk=b""))])
    code, output = run_status(net.recv, net.clock, b"", 2.5, json_mode=True)
    obj = json.loads(output)
    assert code == 0
    assert obj["schema_version"] == 1
    assert obj["authentication"] == "open-unauthenticated"
    assert obj["nodes"][0]["id"] == "tv-den"
    assert obj["nodes"][0]["pairing"] == {"producer": None, "version": [0, ""]}
    assert output == json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


@pytest.mark.parametrize("window", ["nan", "inf", "-inf", "0", "-1"])
def test_invalid_collection_windows_fail_before_socket(
    window: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("socket opened for invalid input")

    monkeypatch.setattr("yoke.cli.open_multicast_socket", forbidden)
    assert main(["status", f"--watch={window}", "--json"]) == 2
    captured = capsys.readouterr()
    assert "finite and positive" in captured.err
    assert json.loads(captured.out)["exit_code"] == 2


@pytest.mark.parametrize(
    "args",
    [
        ["--group", "127.0.0.1"],
        ["--group", "239.1.2.3/24"],
        ["--port", "0"],
        ["--port", "65536"],
        ["--interface", "eth0"],
        ["--interface", "239.1.2.3"],
    ],
)
def test_invalid_network_arguments_fail_before_socket(
    args: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("socket opened for invalid input")

    monkeypatch.setattr("yoke.cli.open_multicast_socket", forbidden)
    assert main([*args, "status"]) == 2


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o620, 0o601])
def test_cli_refuses_exposed_psk(tmp_path: Path, mode: int) -> None:
    path = tmp_path / "psk"
    path.write_bytes(b"secret")
    path.chmod(mode)
    with pytest.raises(ValueError, match="group/other-accessible"):
        resolve_psk(str(path))


@pytest.mark.parametrize("content", [b"", b"\n", b"x" * 4097])
def test_cli_rejects_empty_and_oversize_psk(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "psk"
    path.write_bytes(content)
    path.chmod(0o600)
    with pytest.raises(ValueError):
        resolve_psk(str(path))


def test_cli_rejects_psk_symlink(tmp_path: Path) -> None:
    key = tmp_path / "key"
    key.write_bytes(b"secret")
    key.chmod(0o600)
    link = tmp_path / "psk"
    link.symlink_to(key)
    with pytest.raises(OSError):
        resolve_psk(str(link))


@pytest.mark.parametrize("command", ["status", "snoop"])
def test_passive_commands_never_send_and_select_interface(
    command: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class ReceiveOnlySocket:
        closed = False

        def settimeout(self, value: float | None) -> None:
            pass

        def recvfrom(self, size: int) -> Any:
            if command == "snoop":
                raise KeyboardInterrupt
            raise TimeoutError

        def sendto(self, *args: Any) -> None:
            raise AssertionError("passive command transmitted a datagram")

        def close(self) -> None:
            self.closed = True

    sock = ReceiveOnlySocket()
    opened: list[tuple[str, int, str]] = []

    def open_socket(group: str, port: int, *, interface: str) -> Any:
        opened.append((group, port, interface))
        return sock

    monkeypatch.setattr("yoke.cli.open_multicast_socket", open_socket)
    assert main(["--interface", "127.0.0.1", command, "--json"]) == 0
    assert opened == [("239.121.111.107", 9753, "127.0.0.1")]
    assert sock.closed
    capsys.readouterr()


def test_network_setup_errors_are_stable_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def denied(*args: Any, **kwargs: Any) -> Any:
        raise OSError("interface unavailable")

    monkeypatch.setattr("yoke.cli.open_multicast_socket", denied)
    assert main(["status", "--json"]) == 3
    captured = capsys.readouterr()
    assert "interface unavailable" in captured.err
    assert json.loads(captured.out)["exit_code"] == 3


@pytest.mark.parametrize("commission", [False, True])
def test_agent_commissioning_permission_is_explicit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    commission: bool,
) -> None:
    cfg = write_config(tmp_path)
    seen: list[Any] = []
    fake = types.ModuleType("yoke.net.runner")
    fake.run_agent = lambda agent_cfg: seen.append(agent_cfg)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "yoke.net.runner", fake)
    args = ["agent", "--config", str(cfg)]
    if commission:
        args.append("--commission")
    assert main(args) == 0
    assert seen[0].allow_new_state is commission
    assert "UNAUTHENTICATED" in capsys.readouterr().err


def test_simulate_dispatch_is_offline(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from yoke import simulation

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("offline simulation opened control I/O")

    def run(args: Any, emit: Any) -> int:
        assert args.seed == 42
        emit('{"passed":true}')
        return 0

    monkeypatch.setattr("yoke.cli.open_multicast_socket", forbidden)
    monkeypatch.setattr("yoke.cli.resolve_psk", forbidden)
    monkeypatch.setattr(simulation, "cmd_simulate", run)
    assert main(["simulate", "--seed", "42", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"passed": True}


def test_empty_explicit_origin_is_rejected(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("socket opened for empty origin")

    monkeypatch.setattr("yoke.cli.open_multicast_socket", forbidden)
    assert main(["--origin", "", "pair", "cam-1", "tv-den"]) == 2
    assert "invalid origin id" in capsys.readouterr().err


def test_status_table_escapes_control_characters() -> None:
    net = FakeNet(
        [(1000.1, ann_consumer(name="TV\n\x1b[2J", state="error", err="bad\rvalue"))]
    )
    _, table = run_status(net.recv, net.clock, PSK, 2.5)
    assert len(table.splitlines()) == 2
    assert "\x1b" not in table
    assert "\\n" in table
    assert "\\x1b" in table
    assert "\\r" in table


def test_wall_clock_rollback_does_not_change_retry_deadline() -> None:
    net = FakeNet([(1000.1, ann_consumer())])
    stamps = iter([2000.0, 2000.0, 1900.0, 1800.0, 1700.0, 1600.0])
    io = replace(net.io(), wall_clock=lambda: next(stamps))
    outcome = run_intent(io, "pair", "tv-den", "cam-1")
    assert outcome.exit_code == 1
    assert [when for when, _ in net.sends] == pytest.approx(
        [1000.1, 1000.6, 1001.6, 1003.6, 1007.6]
    )
    assert net.now == pytest.approx(1010.1)
    assert [decode(raw, PSK).ts for _, raw in net.sends] == [
        2000.0,
        1900.0,
        1800.0,
        1700.0,
        1600.0,
    ]


def test_cli_origin_announced_by_endpoint_is_refused() -> None:
    net = FakeNet(
        [
            (1000.1, ann_producer(origin=ORIGIN)),
            (1000.2, ann_consumer()),
        ]
    )
    outcome = run_intent(net.io(), "pair", "tv-den", "cam-1")
    assert outcome.exit_code == 2
    assert "already announced" in outcome.err[0]
    assert net.sends == []


def test_extreme_finite_window_fails_before_socket(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("socket opened for unrepresentable timeout")

    monkeypatch.setattr("yoke.cli.open_multicast_socket", forbidden)
    assert main(["status", "--watch=1e308", "--json"]) == 2
    assert "platform timeout limit" in capsys.readouterr().err


def test_new_operations_have_distinct_authors_but_retries_keep_one_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nonces = iter(["a" * 28, "b" * 28])
    monkeypatch.setattr("yoke.cli.secrets.token_hex", lambda size: next(nonces))
    net = FakeNet([])
    io = replace(net.io(), operation_author=new_operation_author)
    first = run_intent(io, "pair", "tv-den", "cam-1")
    second = run_intent(io, "pair", "tv-den", "cam-2")
    assert first.sent_version == (1, "op-" + "a" * 28)
    assert second.sent_version == (1, "op-" + "b" * 28)
    sent = [decode(raw, PSK) for _, raw in net.sends]
    assert {frame.origin for frame in sent} == {ORIGIN}
    assert all(frame.body["version"] == list(first.sent_version) for frame in sent[:5])
    assert all(frame.body["version"] == list(second.sent_version) for frame in sent[5:])
    assert NODE_ID_RE.fullmatch(first.sent_version[1])
    assert NODE_ID_RE.fullmatch(second.sent_version[1])


def test_delayed_authentic_ack_cannot_commit_a_new_target_from_stale_announce() -> None:
    node = Node(
        NodeConfig("tv-den", Role.CONSUMER, Kind.HDMI),
        state=PersistedState(),
        now=START,
        wall_time=TS,
    )

    def persist(effects: list[Effect]) -> list[Effect]:
        completed = list(effects)
        for effect in effects:
            if isinstance(effect, Persist):
                completed.extend(
                    persist(node.on_persist_result(effect.request_id, True, START))
                )
        return completed

    startup = persist(node.start(START))
    startup_ann = next(
        effect
        for effect in startup
        if isinstance(effect, SendFrame) and effect.type is FrameType.ANN
    )
    previous: Body = {
        "op": "pair",
        "consumer": "tv-den",
        "producer": "cam-1",
        "version": [1, "op-z"],
    }
    prior_raw = encode(FrameType.INT, ORIGIN, 1, TS, previous, PSK)
    prior_effects = persist(node.on_frame(decode(prior_raw, PSK), prior_raw, START))
    prior_ack = next(
        effect
        for effect in prior_effects
        if isinstance(effect, SendFrame) and effect.type is FrameType.ACK
    )
    assert prior_ack.body["applied"] is True
    assert node.pairing.producer == "cam-1"

    net = FakeNet(
        [
            (
                START,
                encode(
                    startup_ann.type,
                    "tv-den",
                    startup_ann.seq,
                    TS,
                    startup_ann.body,
                    PSK,
                ),
            ),
            (
                START + 0.1,
                encode(
                    prior_ack.type, "tv-den", prior_ack.seq, TS, prior_ack.body, PSK
                ),
            ),
        ]
    )
    actual_acks: list[Body] = []

    def send(raw: bytes) -> None:
        net.send(raw)
        effects = persist(node.on_frame(decode(raw, PSK), raw, net.now))
        for effect in effects:
            if isinstance(effect, SendFrame) and effect.type is FrameType.ACK:
                actual_acks.append(effect.body)
                net.script.append(
                    (
                        START + 0.2,
                        encode(effect.type, "tv-den", effect.seq, TS, effect.body, PSK),
                    )
                )

    io = replace(
        net.io(),
        send=send,
        wall_clock=lambda: TS,
        operation_author=lambda: "op-a",
    )
    outcome = run_intent(io, "pair", "tv-den", "cam-2")
    assert outcome.sent_version == (1, "op-a")
    assert outcome.exit_code == 2
    assert not outcome.committed
    assert not outcome.out
    assert actual_acks == [
        {"version": [1, "op-a"], "applied": False, "reason": "superseded"}
    ]
    assert node.pairing.producer == "cam-1"
    assert node.pairing.version == (1, "op-z")
