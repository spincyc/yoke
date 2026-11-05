"""Native C nodes in the deterministic simulation (tests/simnet.py).

``IMPLEMENTATIONS`` names the node mixes the scenarios run with: the Python
reference, all-native, native consumers with Python producers, and native
producers with Python consumers.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from tests.native_ywp.cnode import Library, NodeFactory, factory
from tests.native_ywp.conftest import build_shared
from yoke.core.types import NodeConfig, Role

_CHOICES: dict[str, Callable[[NodeConfig], bool]] = {
    "c": lambda config: True,
    "c-consumers": lambda config: config.role is Role.CONSUMER,
    "c-producers": lambda config: config.role is Role.PRODUCER,
}
IMPLEMENTATIONS = ("python", *_CHOICES)


def build_library(directory: Path) -> Library:
    return Library(build_shared(directory / "libywp.so"))


def native_factory(lib: Library, implementation: str) -> NodeFactory:
    return factory(lib, _CHOICES[implementation])
