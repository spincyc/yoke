"""Offline tests for the stream media driver, using a fake process
supervisor so no pipeline is ever spawned."""

from __future__ import annotations

import subprocess
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
        self.killed = False

    def poll(self) -> int | None:
        return self._exit

    def terminate(self) -> None:
        self.terminated = True
        if self._exit is None:
            self._exit = -15  # SIGTERM

    def wait(self, timeout: float) -> int:
        return self._exit if self._exit is not None else 0

    def kill(self) -> None:
        self.killed = True
        self._exit = -9

    def die(self, code: int) -> None:
        self._exit = code


class FakeSupervisor:
    def __init__(self) -> None:
        self.started: list[list[str]] = []
        self.handles: list[FakeHandle] = []
        self.fail: DriverError | None = None
        self.next_handle: FakeHandle | None = None

    def start(self, argv: Sequence[str]) -> FakeHandle:
        if self.fail is not None:
            raise self.fail
        self.started.append(list(argv))
        h = self.next_handle or FakeHandle()
        self.next_handle = None
        self.handles.append(h)
        return h


MEDIA = {
    "transport": "rtp",
    "framing": "standard-rtp",
    "group": "239.10.20.30",
    "port": 5004,
    "pt": 96,
    "encoding": "raw",
    "clock_rate": 90000,
    "encoding_name": "RAW",
    "sampling": "RGB",
    "depth": 8,
    "width": 1920,
    "height": 1080,
    "colorimetry": "BT709-2",
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

    def test_start_failure_raises_and_reports_error(self) -> None:
        driver, sup = make()
        sup.fail = DriverError("pipeline command not found: gst-launch-1.0")
        with pytest.raises(DriverError, match="not found"):
            driver.attach("cam-1", MEDIA)
        assert driver.poll().state is NodeState.ERROR

    def test_running_status_states_what_was_observed(self) -> None:
        driver, _ = make()
        driver.attach("cam-1", MEDIA)
        assert driver.poll().detail == "pipeline running; media flow unverified"


class StubbornHandle(FakeHandle):
    def __init__(self, *, kill_works: bool = True) -> None:
        super().__init__()
        self.kill_works = kill_works
        self.actions: list[str] = []
        self.terminate_error: OSError | None = None

    def terminate(self) -> None:
        self.actions.append("terminate")
        if self.terminate_error:
            raise self.terminate_error
        self.terminated = True

    def kill(self) -> None:
        self.actions.append("kill")
        self.killed = True
        if self.kill_works:
            self._exit = -9

    def wait(self, timeout: float) -> int:
        self.actions.append("wait")
        if self._exit is None:
            raise subprocess.TimeoutExpired("fake-pipeline", timeout)
        return self._exit


class TestTeardown:
    def test_escalates_termination_and_reaps_before_replacement(self) -> None:
        driver, sup = make()
        first = StubbornHandle()
        sup.next_handle = first
        driver.attach("cam-1", MEDIA)
        driver.attach("cam-2", MEDIA)
        assert first.actions == ["terminate", "wait", "kill", "wait"]
        assert len(sup.started) == 2

    def test_unconfirmed_teardown_blocks_replacement_and_can_be_retried(self) -> None:
        driver, sup = make()
        first = StubbornHandle(kill_works=False)
        sup.next_handle = first
        driver.attach("cam-1", MEDIA)
        with pytest.raises(DriverError, match="teardown unconfirmed"):
            driver.detach()
        assert driver.poll().state is NodeState.ERROR
        with pytest.raises(DriverError, match="teardown unconfirmed"):
            driver.attach("cam-2", MEDIA)
        assert len(sup.started) == 1
        first.die(-9)
        driver.detach()
        assert driver.poll().state is NodeState.IDLE
        driver.attach("cam-2", MEDIA)
        assert len(sup.started) == 2

    def test_os_failure_retains_the_owned_handle(self) -> None:
        driver, sup = make()
        first = StubbornHandle()
        first.terminate_error = PermissionError("cannot signal")
        sup.next_handle = first
        driver.attach("cam-1", MEDIA)
        with pytest.raises(DriverError, match="cannot signal"):
            driver.attach("cam-2", MEDIA)
        assert len(sup.started) == 1
        assert driver.poll().state is NodeState.ERROR
        first.terminate_error = None
        driver.detach()
        assert first.killed
        assert driver.poll().state is NodeState.IDLE

    def test_already_exited_pipeline_is_still_reaped(self) -> None:
        driver, sup = make()
        first = StubbornHandle()
        first.die(1)
        sup.next_handle = first
        driver.attach("cam-1", MEDIA)
        driver.detach()
        assert first.actions == ["terminate", "wait"]

    @pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf")])
    def test_timeout_must_be_finite_and_positive(self, timeout: float) -> None:
        with pytest.raises(DriverError, match="finite and positive"):
            StreamDriver(FakeSupervisor(), stop_timeout=timeout)


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
            {**MEDIA, "encoding": []},
            {**MEDIA, "group": "0.0.0.0"},
        ],
    )
    def test_bad_media_rejected_before_start(self, bad: dict) -> None:
        driver, sup = make()
        with pytest.raises(DriverError):
            driver.attach("cam-1", bad)
        assert sup.started == []

    @pytest.mark.parametrize(
        ("encoding", "name", "depay"),
        [
            ("raw", "RAW", "rtpvrawdepay"),
            ("jpegxs", "JXSV", "rtpjxsvdepay"),
            ("h265", "H265", "rtph265depay"),
        ],
    )
    def test_supported_encodings_have_typed_complete_caps(
        self, encoding: str, name: str, depay: str
    ) -> None:
        media = {
            **MEDIA,
            "encoding": encoding,
            "encoding_name": name,
            "packetmode": 0,
            "exactframerate": "60000/1001",
            "sampling": "YCbCr-4:2:2" if encoding == "jpegxs" else "RGB",
        }
        argv = default_receiver_command("consumer", media)
        assert depay in argv
        caps = next(arg for arg in argv if arg.startswith("caps="))
        assert "media=(string)video" in caps
        assert "payload=(int)96" in caps
        assert "clock-rate=(int)90000" in caps
        assert f"encoding-name=(string){name}" in caps
        if encoding in ("raw", "jpegxs"):
            assert "width=(string)1920" in caps
            assert "height=(string)1080" in caps
            assert "depth=(string)8" in caps
            assert f"sampling=(string){media['sampling']}" in caps
        if encoding == "raw":
            assert "colorimetry=(string)BT709-2" in caps
        if encoding == "jpegxs":
            assert "packetmode=(string)0" in caps
            assert "exactframerate=(string)60000/1001" in caps

    def test_vc2_has_no_guessed_default_plugin(self) -> None:
        with pytest.raises(DriverError, match="no verified VC-2 depayloader"):
            default_receiver_command(
                "consumer", {**MEDIA, "encoding": "vc2", "encoding_name": "vc2"}
            )

    @pytest.mark.parametrize(
        "field", ["sampling", "depth", "width", "height", "colorimetry"]
    )
    def test_raw_requires_explicit_format_before_start(self, field: str) -> None:
        media = dict(MEDIA)
        media.pop(field)
        driver, sup = make()
        with pytest.raises(DriverError, match=field):
            driver.attach("cam", media)
        assert sup.started == []

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("clock_rate", True),
            ("clock_rate", 8000),
            ("encoding_name", "RAW,!fakesink"),
            ("sampling", "RGB,foo=(string)bar"),
            ("depth", "8"),
            ("depth", 12),
            ("width", True),
            ("width", "1920"),
            ("width", 0),
            ("height", 32768),
            ("colorimetry", "BT709-2;application/x-anything"),
        ],
    )
    def test_invalid_metadata_cannot_enter_gstreamer_caps(
        self, field: str, value: object
    ) -> None:
        driver, sup = make()
        with pytest.raises(DriverError):
            driver.attach("cam", {**MEDIA, field: value})
        assert sup.started == []

    @pytest.mark.parametrize("framing", [None, "yoke-reference-v1", "rtp", True])
    def test_standard_receiver_requires_explicit_compatible_framing(
        self, framing: object
    ) -> None:
        driver, sup = make()
        with pytest.raises(DriverError, match="framing must be 'standard-rtp'"):
            driver.attach("cam", {**MEDIA, "framing": framing})
        assert sup.started == []

    @pytest.mark.parametrize("encoding", ["raw", "jpegxs", "vc2"])
    def test_actual_reference_sender_is_not_a_standard_rtp_input(
        self, encoding: str
    ) -> None:
        from yoke.media.assembly import MediaConfig, build_sender

        media = build_sender(MediaConfig(encoding=encoding)).producer_media(
            "239.1.2.3", 5004
        )
        driver, sup = make()
        with pytest.raises(DriverError, match="framing must be 'standard-rtp'"):
            driver.attach("reference-sender", media)
        assert sup.started == []

    @pytest.mark.parametrize(
        "field", ["width", "height", "depth", "sampling", "packetmode"]
    )
    def test_jpegxs_requires_format_parameters(self, field: str) -> None:
        media = {
            **MEDIA,
            "encoding": "jpegxs",
            "encoding_name": "JXSV",
            "packetmode": 0,
            "sampling": "YCbCr-4:2:2",
        }
        media.pop(field)
        with pytest.raises(DriverError, match=field):
            default_receiver_command("consumer", media)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("sampling", "RGB"),
            ("depth", 16),
            ("packetmode", 1),
            ("width", 16385),
            ("height", 15),
            ("exactframerate", "60,!fakesink"),
        ],
    )
    def test_jpegxs_uses_the_configured_decoder_subset(
        self, field: str, value: object
    ) -> None:
        media = {
            **MEDIA,
            "encoding": "jpegxs",
            "encoding_name": "JXSV",
            "sampling": "YCbCr-4:2:2",
            "packetmode": 0,
            field: value,
        }
        with pytest.raises(DriverError):
            default_receiver_command("consumer", media)

    def test_raw_sampling_depth_and_geometry_match_supported_plugin(self) -> None:
        argv = default_receiver_command(
            "consumer", {**MEDIA, "sampling": "YCbCr-4:2:2", "depth": 10}
        )
        assert any("depth=(string)10" in arg for arg in argv)
        with pytest.raises(DriverError, match="align"):
            default_receiver_command(
                "consumer", {**MEDIA, "sampling": "YCbCr-4:2:2", "width": 1919}
            )


class TestProducer:
    def test_producer_media_dict_round_trips(self) -> None:
        prod = StreamProducer(
            "239.1.2.3",
            5004,
            encoding="jpegxs",
            width=1920,
            height=1080,
            sampling="YCbCr-4:2:2",
            depth=10,
            packetmode=0,
        )
        media = prod.media()
        assert media["transport"] == "rtp"
        assert media["group"] == "239.1.2.3"
        assert media["encoding"] == "jpegxs"
        assert media["width"] == 1920
        assert media["framing"] == "standard-rtp"
        assert media["clock_rate"] == 90000
        assert media["encoding_name"] == "JXSV"

    def test_vc2_metadata_is_rfc_named_without_claiming_a_default_decoder(self) -> None:
        media = StreamProducer("239.1.2.3", 5004, encoding="vc2").media()
        assert media["encoding_name"] == "vc2"
        assert media["clock_rate"] == 90000
        assert media["profile"] == "HQ"

    def test_producer_rejects_bad_params(self) -> None:
        with pytest.raises(DriverError):
            StreamProducer("not-an-ip", 5004)
