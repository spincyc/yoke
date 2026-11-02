"""Offline tests for config loading and atomic state persistence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from yoke import statefile
from yoke.config import ConfigError, load
from yoke.core.types import Kind, Pairing, PersistedState, Role


def write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "agent.toml"
    path.write_text(text)
    return path


MINIMAL = """
node_id = "tv-den"
role = "consumer"
kind = "hdmi"
state_file = "{state}"
"""


class TestConfigLoad:
    def test_minimal(self, tmp_path: Path) -> None:
        cfg = load(write_config(tmp_path, MINIMAL.format(state=tmp_path / "s.json")))
        assert cfg.node.node_id == "tv-den"
        assert cfg.node.role is Role.CONSUMER
        assert cfg.node.kind is Kind.HDMI
        assert cfg.node.name == "tv-den"  # defaults to node_id
        assert cfg.psk == b""  # open mode
        assert cfg.port == 9753
        assert cfg.driver == {}

    def test_full(self, tmp_path: Path) -> None:
        psk = tmp_path / "psk"
        psk.write_bytes(b"sekrit\n")
        psk.chmod(0o600)
        cfg = load(
            write_config(
                tmp_path,
                MINIMAL.format(state=tmp_path / "s.json")
                + f'name = "Den TV"\npsk_file = "{psk}"\n'
                + 'group = "239.1.2.3"\nport = 1234\ninterface = "10.0.0.5"\n'
                + '[driver]\ntype = "jap"\ndecoder_host = "10.0.0.20"\n',
            )
        )
        assert cfg.node.name == "Den TV"
        assert cfg.psk == b"sekrit"  # only the trailing newline stripped
        assert (cfg.group, cfg.port) == ("239.1.2.3", 1234)
        assert cfg.interface == "10.0.0.5"
        assert cfg.driver["type"] == "jap"

    def test_psk_preserves_internal_and_whitespace_bytes(self, tmp_path: Path) -> None:
        psk = tmp_path / "psk"
        psk.write_bytes(b" \tkey with spaces \x00\n")  # only final \n stripped
        psk.chmod(0o600)
        cfg = load(
            write_config(
                tmp_path,
                MINIMAL.format(state=tmp_path / "s.json") + f'psk_file = "{psk}"\n',
            )
        )
        assert cfg.psk == b" \tkey with spaces \x00"

    def test_group_or_other_readable_psk_refused(self, tmp_path: Path) -> None:
        psk = tmp_path / "psk"
        psk.write_bytes(b"sekrit")
        psk.chmod(0o644)
        text = MINIMAL.format(state=tmp_path / "s.json") + f'psk_file = "{psk}"\n'
        with pytest.raises(ConfigError, match="group/other-accessible"):
            load(write_config(tmp_path, text))

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="not found"):
            load(tmp_path / "nope.toml")

    def test_invalid_toml(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="invalid TOML"):
            load(write_config(tmp_path, "node_id = "))

    @pytest.mark.parametrize(
        "mutation",
        [
            'role = "director"',
            'kind = "vga"',
            'node_id = "Bad_ID"',
            "port = 0",
            "port = true",
            "name = 7",
            '[driver]\nnothing = "here"',
        ],
    )
    def test_invalid_values(self, tmp_path: Path, mutation: str) -> None:
        base = MINIMAL.format(state=tmp_path / "s.json")
        # Later assignments in TOML would be duplicates; rebuild instead.
        for line in mutation.splitlines():
            key = line.split(" ", 1)[0]
            base = "\n".join(ln for ln in base.splitlines() if not ln.startswith(key))
        with pytest.raises(ConfigError):
            load(write_config(tmp_path, base + "\n" + mutation))

    def test_empty_psk_rejected(self, tmp_path: Path) -> None:
        psk = tmp_path / "psk"
        psk.write_bytes(b"\n")
        psk.chmod(0o600)
        text = MINIMAL.format(state=tmp_path / "s.json") + f'psk_file = "{psk}"\n'
        with pytest.raises(ConfigError, match="empty"):
            load(write_config(tmp_path, text))

    def test_unreadable_psk(self, tmp_path: Path) -> None:
        text = MINIMAL.format(state=tmp_path / "s.json") + 'psk_file = "/nope/psk"\n'
        with pytest.raises(ConfigError, match="psk_file"):
            load(write_config(tmp_path, text))


class TestStateFile:
    def test_missing_is_fresh(self, tmp_path: Path) -> None:
        state = statefile.load(tmp_path / "none.json", allow_missing=True)
        assert state == PersistedState()

    def test_missing_requires_explicit_commissioning(self, tmp_path: Path) -> None:
        with pytest.raises(statefile.StateError, match="missing state"):
            statefile.load(tmp_path / "none.json")

    def test_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "deep" / "state.json"
        state = PersistedState(seq=17, pairing=Pairing("cam-1", (3, "cli-ksh")))
        statefile.save(path, state)
        assert statefile.load(path) == state

    def test_save_is_atomic_replace(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        statefile.save(path, PersistedState(seq=1))
        statefile.save(path, PersistedState(seq=2))
        assert statefile.load(path).seq == 2
        assert not path.with_name("state.json.tmp").exists()

    @pytest.mark.parametrize(
        "content",
        [
            b"not json",
            b"[]",
            b"{}",
            json.dumps({"seq": 1}).encode(),
            json.dumps(
                {"seq": "x", "pairing": {"producer": None, "version": [0, ""]}}
            ).encode(),
        ],
    )
    def test_corrupt_state_raises(self, tmp_path: Path, content: bytes) -> None:
        path = tmp_path / "state.json"
        path.write_bytes(content)
        with pytest.raises(statefile.StateError):
            statefile.load(path)


@pytest.mark.parametrize(
    "extra",
    [
        'group = "127.0.0.1"',
        'group = "239.1.2.3/24"',
        'group = "ff02::1"',
        'interface = "eth0"',
        'interface = "239.1.2.3"',
        'interface = "255.255.255.255"',
        "port = 65536",
    ],
)
def test_network_options_fail_at_config_load(tmp_path: Path, extra: str) -> None:
    with pytest.raises(ConfigError):
        load(write_config(tmp_path, MINIMAL.format(state=tmp_path / "s.json") + extra))


def test_config_read_error_is_actionable(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read config"):
        load(tmp_path)


def test_non_utf8_config_is_actionable(tmp_path: Path) -> None:
    path = tmp_path / "agent.toml"
    path.write_bytes(b"\xff")
    with pytest.raises(ConfigError, match="UTF-8"):
        load(path)


def test_config_psk_fifo_is_rejected_without_waiting(tmp_path: Path) -> None:
    import os

    path = tmp_path / "psk"
    os.mkfifo(path, 0o600)
    with pytest.raises(ConfigError, match="regular"):
        load(
            write_config(
                tmp_path,
                MINIMAL.format(state=tmp_path / "s.json") + f'psk_file = "{path}"\n',
            )
        )


def test_config_rejects_nul_state_path(tmp_path: Path) -> None:
    text = MINIMAL.format(state="bad\\u0000state")
    with pytest.raises(ConfigError, match="state_file"):
        load(write_config(tmp_path, text))
