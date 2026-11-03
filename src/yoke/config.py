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
    type = "jap"               # sim | igmp | usbip | jap | stream
    decoder_host = "10.0.0.20" # driver-specific keys follow the type

Fail-fast: every problem raises :class:`ConfigError` with the offending
key, before any socket is opened.
"""

from __future__ import annotations

import os
import stat
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
from yoke.net.mcast import validate_multicast

DEFAULT_PSK_PATH = Path("/etc/yoke/psk")
OPEN_MODE_WARNING = "warning: no PSK; open mode is UNAUTHENTICATED (labs only)"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class AgentConfig:
    node: NodeConfig
    state_file: Path
    psk: bytes
    group: str = CONTROL_GROUP
    port: int = CONTROL_PORT
    interface: str = "0.0.0.0"
    driver: Body = field(default_factory=dict)
    allow_new_state: bool = False


def read_psk(path: Path) -> bytes:
    """Read bounded private key bytes from one verified regular descriptor."""
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("PSK must be a regular file")
        if info.st_mode & 0o077:
            raise ValueError(
                f"PSK is group/other-accessible (mode {info.st_mode & 0o777:o}); "
                "chmod 600 it"
            )
        if info.st_size > 4096:
            raise ValueError("PSK exceeds 4096 bytes")
        with os.fdopen(fd, "rb", closefd=False) as fh:
            raw = fh.read(4097)
        if len(raw) > 4096:
            raise ValueError("PSK exceeds 4096 bytes")
    finally:
        os.close(fd)
    psk = raw[:-1] if raw.endswith(b"\n") else raw
    if not psk:
        raise ValueError("PSK is empty")
    return psk


def resolve_psk(
    explicit: str | None, default_path: Path = DEFAULT_PSK_PATH
) -> tuple[bytes, str | None]:
    """The PSK plus an optional stderr warning. OSError propagates for an
    explicitly named file; the default path is simply skipped if absent."""
    if explicit is not None:
        psk = read_psk(Path(explicit))
    else:
        try:
            psk = read_psk(default_path)
        except FileNotFoundError:
            psk = b""
    warning = None if psk else OPEN_MODE_WARNING
    return psk, warning


def _require_str(data: Body, key: str, path: Path) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ConfigError(f"{path}: missing or invalid {key!r}")
    return value


def load(path: Path) -> AgentConfig:
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"{path}: not found") from exc
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read config: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{path}: config must be UTF-8") from exc
    except ValueError as exc:
        raise ConfigError(f"{path}: cannot read config: {exc}") from exc

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
            psk = read_psk(psk_path)
        except (OSError, ValueError) as exc:
            raise ConfigError(f"{path}: psk_file {psk_path}: {exc}") from exc

    group = data.get("group", CONTROL_GROUP)
    port = data.get("port", CONTROL_PORT)
    interface = data.get("interface", "0.0.0.0")
    try:
        validate_multicast(group, port, interface=interface)
    except ValueError as exc:
        raise ConfigError(f"{path}: {exc}") from exc

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
        interface=interface,
        driver=dict(driver),
    )
