"""Agent configuration: one TOML file per endpoint.

Example::

    node_id = "tv-den"
    role = "consumer"          # producer | consumer
    kind = "hdmi"              # hdmi | usb
    name = "Den TV"
    psk_file = "/etc/yoke/psk"           # omit for open mode (labs only)
    state_file = "/var/lib/yoke/state.json"
    group = "239.121.111.107"            # optional override
    port = 9753                          # optional override

    [driver]
    type = "jap"               # sim | igmp | usbip | jap
    decoder_host = "10.0.0.20" # driver-specific keys follow the type

Fail-fast: every problem raises :class:`ConfigError` with the offending
key, before any socket is opened.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from yoke.core.types import (
    CONTROL_GROUP,
    CONTROL_PORT,
    Body,
    Kind,
    NodeConfig,
    Role,
)


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class AgentConfig:
    node: NodeConfig
    state_file: Path
    psk: bytes
    group: str = CONTROL_GROUP
    port: int = CONTROL_PORT
    driver: Body = field(default_factory=dict)


def _require_str(data: Body, key: str, path: Path) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{path}: missing or invalid {key!r}")
    return value


def load(path: Path) -> AgentConfig:
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"{path}: not found") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc

    node_id = _require_str(data, "node_id", path)
    try:
        role = Role(_require_str(data, "role", path))
        kind = Kind(_require_str(data, "kind", path))
    except ValueError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    name = data.get("name", node_id)
    if not isinstance(name, str):
        raise ConfigError(f"{path}: invalid 'name'")
    try:
        node = NodeConfig(node_id=node_id, role=role, kind=kind, name=name)
    except ValueError as exc:
        raise ConfigError(f"{path}: {exc}") from exc

    state_file = Path(_require_str(data, "state_file", path))

    psk = b""
    if "psk_file" in data:
        psk_path = Path(_require_str(data, "psk_file", path))
        try:
            psk = psk_path.read_bytes().strip()
        except OSError as exc:
            raise ConfigError(f"{path}: psk_file {psk_path}: {exc}") from exc
        if not psk:
            raise ConfigError(f"{path}: psk_file {psk_path} is empty")

    group = data.get("group", CONTROL_GROUP)
    port = data.get("port", CONTROL_PORT)
    if not isinstance(group, str):
        raise ConfigError(f"{path}: invalid 'group'")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ConfigError(f"{path}: invalid 'port'")

    driver = data.get("driver", {})
    if not isinstance(driver, dict):
        raise ConfigError(f"{path}: [driver] must be a table")
    if driver and not isinstance(driver.get("type"), str):
        raise ConfigError(f"{path}: [driver] needs a 'type'")

    return AgentConfig(
        node=node,
        state_file=state_file,
        psk=psk,
        group=group,
        port=port,
        driver=dict(driver),
    )
