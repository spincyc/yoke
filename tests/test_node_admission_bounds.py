"""Bounded maps never evict an admitted consumer's replay row (ADR 0029).

A producer enforcing data-plane admission (ADR 0024) binds a consumer's
address from its accepted frames. If a flood of other authentic origins
evicted that consumer's replay and source rows, replays of its captured
frames from an attacker's address could rebind the admission. The cap keeps
those rows; other rows stay FIFO. The native core (native/ywp) follows the
same rule, held to it by tests/native_ywp/test_ywp_lockstep.py.
"""

from __future__ import annotations

import pytest

import yoke.core.node as node_module
from yoke.core.frames import decode, encode
from yoke.core.node import Node
from yoke.core.types import (
    Admission,
    Effect,
    FrameType,
    Kind,
    NodeConfig,
    Persist,
    PersistedState,
    Role,
)

PSK = b"admission-bounds-site-key-0123456789"
T0 = 1_793_000_000.0
CLAIM = {
    "role": "consumer",
    "kind": "usb",
    "name": "",
    "state": "pending",
    "err": None,
    "media": None,
    "pairing": {"producer": "prod-1", "version": [1, "cli-x"]},
}


def producer() -> Node:
    node = Node(
        NodeConfig("prod-1", Role.PRODUCER, Kind.USB),
        state=PersistedState(),
        now=1.0,
        wall_time=T0,
        media={"host": "10.0.0.9", "port": 3240, "busids": ["1-1"]},
        admission=True,
    )
    (persist,) = [e for e in node.start(1.0) if isinstance(e, Persist)]
    node.on_persist_result(persist.request_id, True, 1.0)
    return node


def deliver(node: Node, raw: bytes, source: str) -> list[Effect]:
    return node.on_frame(decode(raw, PSK), raw, 2.0, wall=T0, source=source)


def test_flood_cannot_evict_an_admitted_consumers_replay_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(node_module, "MAX_TRACKED", 4)
    node = producer()
    seq = int(T0) * 1_000_000_000
    captured = []
    for _ in range(2):
        seq += 1
        captured.append(encode(FrameType.ANN, "con-a", seq, T0, CLAIM, PSK))
        effects = deliver(node, captured[-1], "10.0.1.10")
    assert Admission((("con-a", "10.0.1.10"),)) in effects
    seq += 1  # a claim that is never admitted: its row stays evictable
    deliver(node, encode(FrameType.ANN, "con-b", seq, T0, CLAIM, PSK), "10.0.1.11")
    for index in range(8):  # more origins than the maps hold
        seq += 1
        body = {"op": "unpair", "consumer": f"x{index}", "version": [1, "cli-x"]}
        raw = encode(FrameType.INT, f"cli-{index}", seq, T0, body, PSK)
        assert not [
            e for e in deliver(node, raw, "10.6.6.6") if isinstance(e, Admission)
        ]
    assert "con-a" in node._replay and "con-a" in node._sources
    assert "con-b" not in node._replay  # plain FIFO for everyone else
    rejected = node._drops["stale"] + node._dups
    for raw in captured:  # replays from an attacker's address bind nothing
        assert not [
            e for e in deliver(node, raw, "10.6.6.6") if isinstance(e, Admission)
        ]
    assert node._drops["stale"] + node._dups == rejected + 2  # stale, then dup
    assert node._admitted == (("con-a", "10.0.1.10"),)
