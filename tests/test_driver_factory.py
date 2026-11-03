"""Offline construction and validation of configured endpoint backends."""

from pathlib import Path

import pytest

from yoke.config import AgentConfig, ConfigError
from yoke.core.types import Body, Kind, NodeConfig, Role
from yoke.drivers.factory import build_driver
from yoke.drivers.igmp import IgmpJoinDriver
from yoke.drivers.jap import JapDecoderDriver
from yoke.drivers.sim import SimDriver
from yoke.drivers.stream import StreamDriver
from yoke.drivers.usbip import UsbipAttachDriver, UsbipBindDriver

# --- build_driver factory ---------------------------------------------------


def make_cfg(role: Role, driver: Body, kind: Kind = Kind.HDMI) -> AgentConfig:
    return AgentConfig(
        node=NodeConfig(node_id="n-1", role=role, kind=kind, name="n"),
        state_file=Path("/nonexistent/state.json"),
        psk=b"",
        driver=driver,
    )


def test_build_driver_sim_consumer() -> None:
    driver = build_driver(make_cfg(Role.CONSUMER, {"type": "sim"}))
    assert isinstance(driver, SimDriver)
    assert driver.describe() is None


def test_build_driver_sim_producer_media() -> None:
    media = {"group": "239.1.2.3", "port": 5004}
    driver = build_driver(make_cfg(Role.PRODUCER, {"type": "sim", "media": media}))
    assert isinstance(driver, SimDriver)
    assert driver.describe() == media


def test_build_driver_sim_bad_media() -> None:
    with pytest.raises(ConfigError, match=r"driver\.media"):
        build_driver(make_cfg(Role.PRODUCER, {"type": "sim", "media": "nope"}))


def test_build_driver_igmp() -> None:
    driver = build_driver(make_cfg(Role.CONSUMER, {"type": "igmp"}))
    assert isinstance(driver, IgmpJoinDriver)


def test_build_driver_usbip_consumer() -> None:
    driver = build_driver(make_cfg(Role.CONSUMER, {"type": "usbip"}, kind=Kind.USB))
    assert isinstance(driver, UsbipAttachDriver)


def test_build_driver_usbip_producer() -> None:
    table: Body = {
        "type": "usbip",
        "host": "10.0.0.9",
        "port": 3240,
        "busids": ["1-1", "1-1.2"],
    }
    driver = build_driver(make_cfg(Role.PRODUCER, table, kind=Kind.USB))
    assert isinstance(driver, UsbipBindDriver)
    assert driver.describe() == {
        "host": "10.0.0.9",
        "port": 3240,
        "busids": ["1-1", "1-1.2"],
    }


@pytest.mark.parametrize("invalid", [{"host": "-bad"}, {"busids": ["../device"]}])
def test_build_driver_usbip_producer_reports_driver_validation_as_config_error(
    invalid: Body,
) -> None:
    table: Body = {
        "type": "usbip",
        "host": "10.0.0.9",
        "port": 3240,
        "busids": ["1-1"],
        **invalid,
    }
    with pytest.raises(ConfigError, match="driver:"):
        build_driver(make_cfg(Role.PRODUCER, table, kind=Kind.USB))


@pytest.mark.parametrize("missing", ["host", "port", "busids"])
def test_build_driver_usbip_producer_missing_key(missing: str) -> None:
    table: Body = {"type": "usbip", "host": "h", "port": 3240, "busids": ["1-1"]}
    del table[missing]
    with pytest.raises(ConfigError, match=f"driver.{missing}"):
        build_driver(make_cfg(Role.PRODUCER, table, kind=Kind.USB))


def test_build_driver_jap_consumer() -> None:
    driver = build_driver(
        make_cfg(Role.CONSUMER, {"type": "jap", "decoder_host": "10.0.0.20"})
    )
    assert isinstance(driver, JapDecoderDriver)


def test_build_driver_jap_consumer_missing_decoder_host() -> None:
    with pytest.raises(ConfigError, match=r"driver\.decoder_host"):
        build_driver(make_cfg(Role.CONSUMER, {"type": "jap"}))


def test_build_driver_jap_producer_is_announce_only() -> None:
    driver = build_driver(make_cfg(Role.PRODUCER, {"type": "jap", "channel": 5}))
    assert isinstance(driver, SimDriver)
    assert driver.describe() == {"channel": 5, "group": "239.92.0.5"}


def test_build_driver_jap_producer_missing_channel() -> None:
    with pytest.raises(ConfigError, match=r"driver\.channel"):
        build_driver(make_cfg(Role.PRODUCER, {"type": "jap"}))


def test_build_driver_jap_producer_channel_out_of_range() -> None:
    with pytest.raises(ConfigError, match=r"driver\.channel"):
        build_driver(make_cfg(Role.PRODUCER, {"type": "jap", "channel": 70000}))


def test_build_driver_stream_consumer() -> None:
    driver = build_driver(
        make_cfg(Role.CONSUMER, {"type": "stream", "stop_timeout": 2})
    )
    assert isinstance(driver, StreamDriver)
    assert driver.describe() is None


@pytest.mark.parametrize(
    "timeout", [True, 0, -1, "5", float("inf"), float("nan"), 10**1000]
)
def test_build_driver_stream_rejects_invalid_stop_timeout(timeout: object) -> None:
    with pytest.raises(ConfigError, match=r"driver\.stop_timeout"):
        build_driver(
            make_cfg(Role.CONSUMER, {"type": "stream", "stop_timeout": timeout})
        )


@pytest.mark.parametrize(
    ("role", "kind"),
    [(Role.PRODUCER, Kind.HDMI), (Role.CONSUMER, Kind.USB)],
)
def test_build_driver_stream_rejects_unsupported_roles(role: Role, kind: Kind) -> None:
    with pytest.raises(ConfigError, match="hdmi consumer"):
        build_driver(make_cfg(role, {"type": "stream"}, kind=kind))


def test_build_driver_unknown_type() -> None:
    with pytest.raises(ConfigError, match="frobnicator"):
        build_driver(make_cfg(Role.CONSUMER, {"type": "frobnicator"}))


def test_build_driver_missing_type() -> None:
    with pytest.raises(ConfigError, match=r"driver\.type"):
        build_driver(make_cfg(Role.CONSUMER, {}))
