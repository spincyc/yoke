"""Native nodes reproduce the reference's bus traffic exactly.

Every scenario in tests/test_simnet_scenarios.py runs once with Python nodes
and once per native mix (tests/native_ywp/simnet_native.py). The native node
draws the same jitter (MT19937 seeded like random.Random) and implements the
same state machine, so every datagram on the simulated bus (time, sender and
exact bytes) must be identical, not merely the converged outcome. A
divergence anywhere in timing, sequence numbers, bodies or encoding fails.
HOST ONLY: simulated network, no sockets.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

import tests.test_simnet_scenarios as scenarios
from tests import simnet
from tests.native_ywp.cnode import Library
from tests.native_ywp.simnet_native import (
    IMPLEMENTATIONS,
    build_library,
    native_factory,
)

Traffic = list[list[tuple[float, str, bytes]]]


def _calls() -> Iterator[tuple[str, Callable[..., None], dict[str, Any]]]:
    for name, function in inspect.getmembers(scenarios, inspect.isfunction):
        if not name.startswith("test_") or function.__module__ != scenarios.__name__:
            continue
        marks = [
            m for m in getattr(function, "pytestmark", []) if m.name == "parametrize"
        ]
        if not marks:
            yield name, function, {}
            continue
        argname, values = marks[0].args
        for value in values:
            yield f"{name}[{value}]", function, {argname: value}


CALLS = list(_calls())


@pytest.fixture(scope="module")
def library(tmp_path_factory: pytest.TempPathFactory) -> Library:
    return build_library(tmp_path_factory.mktemp("ywp-trace"))


def _traffic(
    monkeypatch: pytest.MonkeyPatch,
    function: Callable[..., None],
    kwargs: dict[str, Any],
    factory: Callable[..., Any],
) -> Traffic:
    nets: list[simnet.SimNet] = []

    class Recording(simnet.SimNet):
        def __init__(self, **options: Any) -> None:
            super().__init__(**options)
            nets.append(self)

    with monkeypatch.context() as patch:
        patch.setattr(scenarios, "SimNet", Recording)
        patch.setattr(simnet, "NODE_FACTORY", factory)
        function(**kwargs)
    return [
        [(entry.time, entry.sender, entry.raw) for entry in net.log] for net in nets
    ]


@pytest.mark.parametrize(
    ("name", "function", "kwargs"), CALLS, ids=[c[0] for c in CALLS]
)
def test_native_mixes_reproduce_reference_traffic(
    monkeypatch: pytest.MonkeyPatch,
    library: Library,
    name: str,
    function: Callable[..., None],
    kwargs: dict[str, Any],
) -> None:
    expected = _traffic(monkeypatch, function, kwargs, simnet.python_node)
    assert expected and all(expected), name
    for implementation in IMPLEMENTATIONS[1:]:
        got = _traffic(
            monkeypatch, function, kwargs, native_factory(library, implementation)
        )
        assert len(got) == len(expected), implementation
        for net_index, (mine, theirs) in enumerate(zip(got, expected, strict=True)):
            for index, (a, b) in enumerate(zip(mine, theirs, strict=False)):
                assert a == b, (implementation, net_index, index, a, b)
            assert len(mine) == len(theirs), (implementation, net_index)


def test_every_scenario_is_covered() -> None:
    assert len(CALLS) >= 28
    assert Path(scenarios.__file__).name == "test_simnet_scenarios.py"
