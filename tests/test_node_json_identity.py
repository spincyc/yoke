"""Opaque JSON identity in the reference node is canonical bytes (ADR 0029).

A producer description "changes", and a rejected intent's non-id producer
"matches", by canonical bytes, not by Python equality (which merges 1, 1.0
and true). The native core (native/ywp) compares the same way; the
lockstep test in tests/native_ywp holds the two to it.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

from yoke.core.frames import canonical_bytes, decode
from yoke.core.node import Node
from yoke.core.types import (
    AttachMedia,
    Effect,
    FrameType,
    Kind,
    NodeConfig,
    Persist,
    PersistedState,
    Role,
)

PSK = b"identity-test-site-key-0123456789"
T0 = 1_793_000_000.0
_seq = [int(T0) * 1_000_000_000]


def frame(frame_type: FrameType, origin: str, body: dict[str, Any]) -> bytes:
    """A canonical signed frame, floats in the body included (a reference
    encoder refuses them; receivers must tolerate them)."""
    _seq[0] += 1
    unsigned = {
        "v": 1,
        "t": frame_type.value,
        "id": origin,
        "seq": _seq[0],
        "ts": T0,
        "body": body,
    }
    sig = hmac.new(PSK, canonical_bytes(unsigned), hashlib.sha256).hexdigest()
    return canonical_bytes({**unsigned, "sig": sig})


def settle(node: Node, effects: list[Effect]) -> list[Effect]:
    out: list[Effect] = []
    for effect in effects:
        out.append(effect)
        if isinstance(effect, Persist):
            out += settle(node, node.on_persist_result(effect.request_id, True, 1.0))
    return out


def deliver(node: Node, raw: bytes) -> list[Effect]:
    return settle(node, node.on_frame(decode(raw, PSK), raw, 1.0, wall=T0))


def consumer() -> Node:
    node = Node(
        NodeConfig("con-1", Role.CONSUMER, Kind.USB),
        state=PersistedState(),
        now=1.0,
        wall_time=T0,
    )
    settle(node, node.start(1.0))
    return node


def announce(media: dict[str, Any]) -> bytes:
    return frame(
        FrameType.ANN,
        "prod-1",
        {
            "role": "producer",
            "kind": "usb",
            "name": "p",
            "state": "active",
            "err": None,
            "pairing": None,
            "media": media,
        },
    )


def test_numerically_equal_media_of_another_type_is_a_changed_description() -> None:
    node = consumer()
    deliver(node, announce({"host": "10.0.0.1", "port": 3240, "busids": ["1-1"]}))
    effects = deliver(
        node,
        frame(
            FrameType.INT,
            "cli-a",
            {
                "op": "pair",
                "consumer": "con-1",
                "producer": "prod-1",
                "version": [1, "cli-a"],
            },
        ),
    )
    assert any(isinstance(e, AttachMedia) for e in effects)
    for changed in (
        {"host": "10.0.0.1", "port": 3240.0, "busids": ["1-1"]},
        {"host": "10.0.0.1", "port": 3240, "busids": ["1-1"], "x": True},
        {"host": "10.0.0.1", "port": 3240, "busids": ["1-1"], "x": 1},
    ):
        attaches = [
            e for e in deliver(node, announce(changed)) if isinstance(e, AttachMedia)
        ]
        assert attaches == [AttachMedia("prod-1", changed)], changed
    # Re-announcing the same canonical description changes nothing.
    assert not [
        e for e in deliver(node, announce(changed)) if isinstance(e, AttachMedia)
    ]


def test_rejected_non_id_producer_matches_only_identical_canonical_json() -> None:
    node = consumer()
    deliver(
        node,
        frame(
            FrameType.INT,
            "cli-a",
            {
                "op": "pair",
                "consumer": "con-1",
                "producer": 1,
                "version": [4, "cli-a"],
            },
        ),
    )  # unknown-producer: pinned as rejected with producer 1
    assert node._rejected["con-1"] == ((4, "cli-a"), 1)
    # The same version with producer true is a different value, so this
    # (cacheable) unpair is not the rejected intent and is cached.
    deliver(
        node,
        frame(
            FrameType.INT,
            "cli-b",
            {
                "op": "unpair",
                "consumer": "con-1",
                "producer": True,
                "version": [4, "cli-a"],
            },
        ),
    )
    assert node._intent_cache["con-1"].version == (4, "cli-a")
