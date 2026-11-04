"""Construct endpoint backends from validated agent configuration.

Backend selection and backend-specific configuration belong here. Construction
performs no device calls; the agent runner owns driver execution and lifetimes.
"""

from __future__ import annotations

import math

from yoke.config import AgentConfig, ConfigError
from yoke.core.types import Kind, Role
from yoke.drivers.base import Driver, DriverError
from yoke.drivers.gadget import build_usb_gadget_driver
from yoke.drivers.igmp import IgmpJoinDriver
from yoke.drivers.jap import JapDecoderDriver, JapEncoderConfig, telnet_link_factory
from yoke.drivers.shellcmd import SubprocessRunner
from yoke.drivers.sim import SimDriver
from yoke.drivers.stream import StreamDriver, SubprocessSupervisor
from yoke.drivers.usbip import UsbipAttachDriver, UsbipBindDriver


def build_driver(cfg: AgentConfig) -> Driver:
    """Construct the media-plane driver named by ``cfg.driver["type"]``.

    Raises :class:`~yoke.config.ConfigError` naming the offending key on
    an unknown type or a missing/invalid driver-specific key.
    """
    table = cfg.driver
    driver_type = table.get("type")
    if not isinstance(driver_type, str) or not driver_type:
        raise ConfigError("driver.type: missing")
    if driver_type == "sim":
        media = table.get("media")
        if media is not None and not isinstance(media, dict):
            raise ConfigError(f"driver.media: expected a table, got {media!r}")
        return SimDriver(media=media)
    if driver_type == "igmp":
        return IgmpJoinDriver()
    if driver_type == "usbip":
        return _build_usbip(cfg, table)
    if driver_type == "usb-gadget":
        return build_usb_gadget_driver(cfg)
    if driver_type == "jap":
        return _build_jap(cfg, table)
    if driver_type == "stream":
        if cfg.node.role is not Role.CONSUMER or cfg.node.kind is not Kind.HDMI:
            raise ConfigError("driver.type: stream requires an hdmi consumer")
        timeout = table.get("stop_timeout", 5.0)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ConfigError("driver.stop_timeout: expected a finite positive number")
        try:
            timeout_seconds = float(timeout)
        except OverflowError as exc:
            raise ConfigError("driver.stop_timeout: number is too large") from exc
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ConfigError("driver.stop_timeout: expected a finite positive number")
        return StreamDriver(SubprocessSupervisor(), stop_timeout=timeout_seconds)
    raise ConfigError(f"driver.type: unknown driver type {driver_type!r}")


def _build_usbip(cfg: AgentConfig, table: dict[str, object]) -> Driver:
    if cfg.node.role is Role.CONSUMER:
        return UsbipAttachDriver(
            SubprocessRunner(),
            ownership_file=cfg.state_file.with_name(
                cfg.state_file.name + ".usbip.json"
            ),
        )
    host = table.get("host")
    if not isinstance(host, str) or not host:
        raise ConfigError("driver.host: required for a usbip producer")
    port = table.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
        raise ConfigError("driver.port: required for a usbip producer")
    raw_busids = table.get("busids")
    if not isinstance(raw_busids, list) or not raw_busids:
        raise ConfigError("driver.busids: required for a usbip producer")
    busids = [b for b in raw_busids if isinstance(b, str) and b]
    if len(busids) != len(raw_busids):
        raise ConfigError("driver.busids: entries must be non-empty strings")
    try:
        return UsbipBindDriver(SubprocessRunner(), host=host, port=port, busids=busids)
    except DriverError as exc:
        raise ConfigError(f"driver: {exc}") from exc


def _build_jap(cfg: AgentConfig, table: dict[str, object]) -> Driver:
    if cfg.node.role is Role.CONSUMER:
        decoder_host = table.get("decoder_host")
        if not isinstance(decoder_host, str) or not decoder_host:
            raise ConfigError("driver.decoder_host: required for a jap consumer")
        return JapDecoderDriver(decoder_host, telnet_link_factory)
    # JAP encoders need no runtime driving — they always transmit on their
    # channel. An announce-only warden with the derived media dict is the
    # honest producer shape, and SimDriver provides exactly that.
    channel = table.get("channel")
    if not isinstance(channel, int) or isinstance(channel, bool):
        raise ConfigError("driver.channel: required for a jap producer")
    try:
        media = JapEncoderConfig(channel).media()
    except DriverError as exc:
        raise ConfigError(f"driver.channel: {exc}") from exc
    return SimDriver(media=media)
