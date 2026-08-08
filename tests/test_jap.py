"""Offline tests for the Just Add Power decoder driver.

Every device interaction is a scripted transcript on a fake link, so the
assumptions about the JAP CLI live here as executable statements to be
corrected after bench verification.
"""

from __future__ import annotations

import pytest

from yoke.core.types import NodeState
from yoke.drivers.base import DriverError
from yoke.drivers.jap import (
    JapDecoderDriver,
    JapEncoderConfig,
    JapLink,
    jap_group,
)


class FakeLink:
    def __init__(self, script: dict[str, str | Exception], log: list[str]) -> None:
        self._script = script
        self._log = log
        self.closed = False

    def send_command(self, command: str) -> str:
        self._log.append(command)
        result = self._script.get(command)
        if result is None:
            raise AssertionError(f"unexpected command: {command!r}")
        if isinstance(result, Exception):
            raise result
        return result

    def close(self) -> None:
        self.closed = True


class LinkRig:
    def __init__(self) -> None:
        self.script: dict[str, str | Exception] = {}
        self.log: list[str] = []
        self.links: list[FakeLink] = []
        self.connect_error: DriverError | None = None

    def factory(self, host: str) -> JapLink:
        if self.connect_error is not None:
            raise self.connect_error
        link = FakeLink(self.script, self.log)
        self.links.append(link)
        return link


@pytest.fixture
def rig() -> LinkRig:
    return LinkRig()


def make_driver(rig: LinkRig) -> JapDecoderDriver:
    return JapDecoderDriver("10.0.0.20", rig.factory)


class TestJapGroup:
    def test_channel_maps_into_last_two_octets(self) -> None:
        assert jap_group(1) == "239.92.0.1"
        assert jap_group(100) == "239.92.0.100"
        assert jap_group(256) == "239.92.1.0"
        assert jap_group(300) == "239.92.1.44"

    @pytest.mark.parametrize("channel", [0, -1, 65536])
    def test_out_of_range(self, channel: int) -> None:
        with pytest.raises(DriverError):
            jap_group(channel)


class TestAttach:
    def test_sets_channel(self, rig: LinkRig) -> None:
        rig.script["channel 100"] = "channel set to 100"
        driver = make_driver(rig)
        driver.attach("enc-den", {"channel": 100})
        assert rig.log == ["channel 100"]
        assert all(link.closed for link in rig.links)

    def test_error_reply_raises(self, rig: LinkRig) -> None:
        rig.script["channel 100"] = "ERROR: invalid channel"
        driver = make_driver(rig)
        with pytest.raises(DriverError, match="refused"):
            driver.attach("enc-den", {"channel": 100})
        assert driver.poll().state is NodeState.IDLE

    def test_connect_failure_raises(self, rig: LinkRig) -> None:
        rig.connect_error = DriverError("jap connect 10.0.0.20:23: refused")
        driver = make_driver(rig)
        with pytest.raises(DriverError, match="connect"):
            driver.attach("enc-den", {"channel": 100})

    @pytest.mark.parametrize(
        "media",
        [{}, {"channel": "100"}, {"channel": True}, {"channel": 0}, {"channel": 70000}],
    )
    def test_bad_media_rejected_without_io(self, rig: LinkRig, media: dict) -> None:
        driver = make_driver(rig)
        with pytest.raises(DriverError):
            driver.attach("enc-den", media)
        assert rig.log == []


class TestDetach:
    def test_detach_sends_channel_zero(self, rig: LinkRig) -> None:
        rig.script["channel 100"] = "ok"
        rig.script["channel 0"] = "ok"
        driver = make_driver(rig)
        driver.attach("enc-den", {"channel": 100})
        driver.detach()
        assert rig.log == ["channel 100", "channel 0"]

    def test_detach_is_idempotent_and_quiet(self, rig: LinkRig) -> None:
        driver = make_driver(rig)
        driver.detach()
        assert rig.log == []

    def test_detach_swallows_device_error(self, rig: LinkRig) -> None:
        rig.script["channel 100"] = "ok"
        rig.script["channel 0"] = DriverError("link died")
        driver = make_driver(rig)
        driver.attach("enc-den", {"channel": 100})
        driver.detach()  # must not raise
        assert driver.poll().state is NodeState.IDLE


class TestPoll:
    def test_idle_before_attach(self, rig: LinkRig) -> None:
        assert make_driver(rig).poll().state is NodeState.IDLE

    def test_active_when_channel_matches(self, rig: LinkRig) -> None:
        rig.script["channel 100"] = "ok"
        rig.script["channel"] = "current channel: 100"
        driver = make_driver(rig)
        driver.attach("enc-den", {"channel": 100})
        assert driver.poll().state is NodeState.ACTIVE

    def test_pending_when_channel_differs(self, rig: LinkRig) -> None:
        rig.script["channel 100"] = "ok"
        rig.script["channel"] = "current channel: 42"
        driver = make_driver(rig)
        driver.attach("enc-den", {"channel": 100})
        status = driver.poll()
        assert status.state is NodeState.PENDING
        assert "42" in status.detail

    def test_error_on_link_failure(self, rig: LinkRig) -> None:
        rig.script["channel 100"] = "ok"
        rig.script["channel"] = DriverError("timeout")
        driver = make_driver(rig)
        driver.attach("enc-den", {"channel": 100})
        assert driver.poll().state is NodeState.ERROR

    def test_error_on_unparseable_reply(self, rig: LinkRig) -> None:
        rig.script["channel 100"] = "ok"
        rig.script["channel"] = "huh?"
        driver = make_driver(rig)
        driver.attach("enc-den", {"channel": 100})
        status = driver.poll()
        assert status.state is NodeState.ERROR
        assert "unparseable" in status.detail


class TestEncoderConfig:
    def test_media_advertises_channel_and_group(self) -> None:
        media = JapEncoderConfig(300).media()
        assert media == {"channel": 300, "group": "239.92.1.44"}

    def test_rejects_bad_channel(self) -> None:
        with pytest.raises(DriverError):
            JapEncoderConfig(0)
