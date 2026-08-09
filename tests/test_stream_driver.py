"""Offline tests for the stream media driver, using a fake process
supervisor so no pipeline is ever spawned."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from yoke.core.types import NodeState
from yoke.drivers.base import DriverError
from yoke.drivers.stream import (
    StreamDriver,
    StreamProducer,
    default_receiver_command,
)


class FakeHandle:
    def __init__(self) -> None:
        self._exit: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self._exit

    def terminate(self) -> None:
        self.terminated = True
        if self._exit is None:
            self._exit = -15  # SIGTERM

    def wait(self, timeout: float) -> int:
        return self._exit if self._exit is not None else 0

    def die(self, code: int) -> None:
        self._exit = code


class FakeSupervisor:
    def __init__(self) -> None:
        self.started: list[list[str]] = []
        self.handles: list[FakeHandle] = []
        self.fail: DriverError | None = None

    def start(self, argv: Sequence[str]) -> FakeHandle:
        if self.fail is not None:
            raise self.fail
        self.started.append(list(argv))
        h = FakeHandle()
        self.handles.append(h)
        return h


MEDIA = {
    "transport": "rtp",
    "group": "239.10.20.30",
    "port": 5004,
    "pt": 96,
    "encoding": "raw",
}


def make() -> tuple[StreamDriver, FakeSupervisor]:
    sup = FakeSupervisor()
    return StreamDriver(sup), sup


class TestAttachDetach:
    def test_attach_starts_pipeline_and_reports_active(self) -> None:
        driver, sup = make()
        driver.attach("cam-1", MEDIA)
        assert len(sup.started) == 1
        assert sup.started[0][0] == "gst-launch-1.0"
        assert "239.10.20.30" in " ".join(sup.started[0])
        assert driver.poll().state is NodeState.ACTIVE

    def test_reattach_replaces_running_pipeline(self) -> None:
        driver, sup = make()
        driver.attach("cam-1", MEDIA)
        first = sup.handles[0]
        driver.attach("cam-2", {**MEDIA, "port": 5006})
        assert first.terminated
        assert len(sup.started) == 2
        assert driver.poll().state is NodeState.ACTIVE

    def test_detach_terminates_and_is_idempotent(self) -> None:
        driver, sup = make()
        driver.attach("cam-1", MEDIA)
        driver.detach()
        assert sup.handles[0].terminated
        assert driver.poll().state is NodeState.IDLE
        driver.detach()  # must not raise

    def test_start_failure_raises_and_stays_idle(self) -> None:
        driver, sup = make()
        sup.fail = DriverError("pipeline command not found: gst-launch-1.0")
        with pytest.raises(DriverError, match="not found"):
            driver.attach("cam-1", MEDIA)
        assert driver.poll().state is NodeState.IDLE


class TestPoll:
    def test_pipeline_exit_while_paired_is_error(self) -> None:
        driver, sup = make()
        driver.attach("cam-1", MEDIA)
        sup.handles[0].die(1)
        status = driver.poll()
        assert status.state is NodeState.ERROR
        assert "code 1" in status.detail

    def test_idle_before_attach(self) -> None:
        driver, _ = make()
        assert driver.poll().state is NodeState.IDLE


class TestMediaValidation:
    @pytest.mark.parametrize(
        "bad",
        [
            {**MEDIA, "transport": "udp"},
            {**MEDIA, "group": 123},
            {**MEDIA, "group": "not-an-ip"},
            {**MEDIA, "port": 0},
            {**MEDIA, "port": True},
            {**MEDIA, "pt": 5},
            {**MEDIA, "encoding": "mpeg2"},
            {**MEDIA, "group": "0.0.0.0"},
        ],
    )
    def test_bad_media_rejected_before_start(self, bad: dict) -> None:
        driver, sup = make()
        with pytest.raises(DriverError):
            driver.attach("cam-1", bad)
        assert sup.started == []

    def test_encodings_map_to_pipelines(self) -> None:
        for enc, depay in [
            ("raw", "rtpvrawdepay"),
            ("jpegxs", "rtpjpegxsdepay"),
            ("vc2", "rtpvc2depay"),
            ("h265", "rtph265depay"),
        ]:
            argv = default_receiver_command("consumer", {**MEDIA, "encoding": enc})
            assert depay in argv


class TestProducer:
    def test_producer_media_dict_round_trips(self) -> None:
        prod = StreamProducer("239.1.2.3", 5004, encoding="jpegxs", width=1920)
        media = prod.media()
        assert media["transport"] == "rtp"
        assert media["group"] == "239.1.2.3"
        assert media["encoding"] == "jpegxs"
        assert media["width"] == 1920

    def test_producer_rejects_bad_params(self) -> None:
        with pytest.raises(DriverError):
            StreamProducer("not-an-ip", 5004)
