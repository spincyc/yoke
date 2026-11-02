"""The ``stream`` media driver: pair a consumer to a producer's RTP media.

This is the bridge between yoke's control plane and the AV-over-IP media
plane. Where ``igmp`` joins a multicast group and ``jap`` calls a decoder
API, ``stream`` starts (and stops) a long-running media pipeline process —
GStreamer, Intel MTL, or the software ``yoke.media`` pipeline — configured
from the producer's advertised media parameters.

Because a media pipeline is long-running (unlike the one-shot device calls
the other drivers make), the process lifecycle lives behind a
:class:`ProcessSupervisor` seam, mocked in tests exactly like the
``CommandRunner`` seam. The driver never touches media payload (ADR 0002).
"""

from __future__ import annotations

import contextlib
import ipaddress
import os
import re
import shutil
import signal
import subprocess
import time
from collections.abc import Callable, Sequence
from typing import Protocol

from yoke.core.types import Body, DriverStatus, NodeState
from yoke.drivers.base import DriverError, positive_timeout
from yoke.drivers.shellcmd import command_args

ENCODINGS = frozenset({"raw", "jpegxs", "vc2", "h265"})
RTP_NAMES = {"raw": "RAW", "jpegxs": "JXSV", "vc2": "vc2", "h265": "H265"}
RAW_SAMPLINGS = frozenset(
    {
        "RGB",
        "RGBA",
        "BGR",
        "BGRA",
        "YCbCr-4:4:4",
        "YCbCr-4:2:2",
        "YCbCr-4:2:0",
        "YCbCr-4:1:1",
    }
)
JXS_SAMPLINGS = frozenset({"YCbCr-4:4:4", "YCbCr-4:2:2", "YCbCr-4:2:0"})
DEFAULT_START_TIMEOUT = 5.0


class ProcessHandle(Protocol):
    """An opaque handle to a running media pipeline process."""

    def poll(self) -> int | None:
        """Return the exit code, or None if still running."""

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float) -> int: ...


class ProcessSupervisor(Protocol):
    """Starts and stops long-running pipeline processes. Injected so the
    driver is testable without spawning anything."""

    def start(self, argv: Sequence[str]) -> ProcessHandle: ...


class _PipelineProcess:
    """Own the spawned process and, on POSIX, its isolated process group."""

    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        self._proc = proc

    def _group_alive(self) -> bool:
        if os.name != "posix":
            return False
        try:
            os.killpg(self._proc.pid, 0)
        except ProcessLookupError:
            return False
        return True

    def poll(self) -> int | None:
        # A surviving helper must not make an exited pipeline look healthy.
        # wait() separately proves that the entire owned group drained.
        return self._proc.poll()

    def _signal(self, sig: int) -> None:
        try:
            if os.name == "posix":
                os.killpg(self._proc.pid, sig)
            elif sig == signal.SIGTERM:
                self._proc.terminate()
            else:
                self._proc.kill()
        except ProcessLookupError:
            pass  # already quiescent; wait still reaps the owned child

    def terminate(self) -> None:
        self._signal(signal.SIGTERM)

    def kill(self) -> None:
        if os.name == "posix":
            self._signal(signal.SIGKILL)
        else:
            with contextlib.suppress(ProcessLookupError):
                self._proc.kill()

    def wait(self, timeout: float) -> int:
        deadline = time.monotonic() + timeout
        code = self._proc.wait(timeout=timeout)
        while self._group_alive():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(self._proc.args, timeout)
            time.sleep(min(remaining, 0.01))
        return code


class SubprocessSupervisor:
    """Launch an isolated pipeline; teardown includes its POSIX group.

    Pipelines must not daemonize or escape that group. No subprocess API
    can prove termination of a worker that deliberately relinquishes it.
    """

    def start(self, argv: Sequence[str]) -> ProcessHandle:
        args = command_args(argv)
        exe = shutil.which(args[0])
        if exe is None:
            raise DriverError(f"pipeline command not found: {args[0]}")
        try:
            return _PipelineProcess(
                subprocess.Popen(
                    [exe, *args[1:]],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=os.name == "posix",
                )
            )
        except OSError as exc:
            raise DriverError(f"failed to start pipeline: {exc}") from exc


CommandBuilder = Callable[[str, Body], list[str]]


def _integer(media: Body, key: str, low: int, high: int) -> int:
    value = media.get(key)
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not low <= value <= high
    ):
        raise DriverError(f"stream media {key!r} must be an integer in {low}..{high}")
    return value


def _choice(media: Body, key: str, choices: frozenset[str]) -> str:
    value = media.get(key)
    if not isinstance(value, str) or value not in choices:
        raise DriverError(f"stream media {key!r} must be one of {sorted(choices)!r}")
    return value


def _format_caps(media: Body, encoding: str) -> list[str]:
    """Validate advertised fmtp values and render only typed, bounded values."""
    if encoding == "vc2":
        if media.get("profile", "HQ") != "HQ":
            raise DriverError("stream VC-2 profile must be 'HQ' (RFC 8450)")
        return ["profile=(string)HQ"]
    if encoding == "h265":
        return []
    minimum, maximum = (1, 32767) if encoding == "raw" else (16, 16384)
    width = _integer(media, "width", minimum, maximum)
    height = _integer(media, "height", minimum, maximum)
    sampling = _choice(
        media, "sampling", RAW_SAMPLINGS if encoding == "raw" else JXS_SAMPLINGS
    )
    depth = _integer(media, "depth", 8, 16)
    if encoding == "raw":
        if depth != 8 and not (depth == 10 and sampling == "YCbCr-4:2:2"):
            raise DriverError(
                "rtpvrawdepay supports depth 8, or depth 10 with YCbCr-4:2:2"
            )
    elif depth not in (8, 10, 12):
        raise DriverError("svtjpegxsdec supports JPEG XS depth 8, 10 or 12")
    divisor = (
        4
        if sampling.endswith("4:1:1")
        else 2
        if sampling.endswith(("4:2:2", "4:2:0"))
        else 1
    )
    if width % divisor or (sampling.endswith("4:2:0") and height % 2):
        raise DriverError(
            "stream width/height do not align with the advertised sampling"
        )
    caps = [
        f"sampling=(string){sampling}",
        f"depth=(string){depth}",
        f"width=(string){width}",
        f"height=(string){height}",
    ]
    if encoding == "raw":
        colorimetry = _choice(
            media, "colorimetry", frozenset({"BT601-5", "BT709-2", "SMPTE240M"})
        )
        caps.append(f"colorimetry=(string){colorimetry}")
    else:
        _integer(media, "packetmode", 0, 0)
        caps.append("packetmode=(string)0")
        if "exactframerate" in media:
            rate = media["exactframerate"]
            if (
                not isinstance(rate, str)
                or re.fullmatch(r"[1-9][0-9]{0,5}(?:/[1-9][0-9]{0,5})?", rate) is None
            ):
                raise DriverError(
                    "stream exactframerate must be a positive rate "
                    "such as '60' or '60000/1001'"
                )
            caps.append(f"exactframerate=(string){rate}")
    return caps


def _validate_media(media: Body) -> tuple[str, int, int, str]:
    """Validate the advertised media dict; return (group, port, pt, enc)."""
    if media.get("transport") != "rtp":
        raise DriverError(f"stream media transport must be 'rtp': {media!r}")
    if media.get("framing") != "standard-rtp":
        raise DriverError(
            "stream media framing must be 'standard-rtp'; "
            "the Yoke reference framing needs its own receiver"
        )
    group = media.get("group")
    if not isinstance(group, str):
        raise DriverError("stream media needs a string 'group'")
    try:
        addr = ipaddress.IPv4Address(group)
    except ipaddress.AddressValueError as exc:
        raise DriverError(f"stream media 'group' invalid: {group}") from exc
    port = media.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise DriverError(f"stream media 'port' invalid: {port!r}")
    pt = media.get("pt", 96)
    if not isinstance(pt, int) or isinstance(pt, bool) or not 96 <= pt <= 127:
        raise DriverError(f"stream media 'pt' invalid: {pt!r}")
    encoding = media.get("encoding", "raw")
    if not isinstance(encoding, str) or encoding not in ENCODINGS:
        raise DriverError(f"stream media 'encoding' unknown: {encoding!r}")
    _integer({"clock_rate": media.get("clock_rate", 90000)}, "clock_rate", 90000, 90000)
    if media.get("encoding_name", RTP_NAMES[encoding]) != RTP_NAMES[encoding]:
        raise DriverError(
            f"stream media encoding_name must be {RTP_NAMES[encoding]!r} for {encoding}"
        )
    _format_caps(media, encoding)
    # unicast group is allowed; multicast is the common case. Only reject
    # obviously wrong (loopback/unspecified) addresses.
    if addr.is_unspecified:
        raise DriverError("stream media 'group' is unspecified")
    return group, port, pt, encoding


def default_receiver_command(role: str, media: Body) -> list[str]:
    """Generate a standard RTP GStreamer command from validated metadata.

    Caps match upstream plugin contracts (ADR 0017). Plugin availability,
    actual decoding and display remain bench-unverified; process liveness
    is the only runtime health evidence this driver obtains.
    """
    group, port, pt, encoding = _validate_media(media)
    if encoding == "vc2":
        raise DriverError(
            "default receiver has no verified VC-2 depayloader; "
            "configure a validated custom command builder"
        )
    depay = {
        "raw": "rtpvrawdepay",
        "jpegxs": "rtpjxsvdepay",
        "h265": "rtph265depay",
    }[encoding]
    decode = {
        "raw": "identity",
        "jpegxs": "svtjpegxsdec",
        "h265": "avdec_h265",
    }[encoding]
    return [
        "gst-launch-1.0",
        "-q",
        "udpsrc",
        f"address={group}",
        f"port={port}",
        "caps="
        + ",".join(
            [
                "application/x-rtp",
                "media=(string)video",
                f"payload=(int){pt}",
                "clock-rate=(int)90000",
                f"encoding-name=(string){RTP_NAMES[encoding]}",
                *_format_caps(media, encoding),
            ]
        ),
        "!",
        "rtpjitterbuffer",
        "latency=10",
        "!",
        depay,
        "!",
        decode,
        "!",
        "videoconvert",
        "!",
        "kmssink" if role == "consumer" else "fakesink",
    ]


class StreamDriver:
    """Consumer-side media driver: start a receive pipeline for the
    producer's stream, stop it on detach."""

    def __init__(
        self,
        supervisor: ProcessSupervisor,
        *,
        role: str = "consumer",
        command_builder: CommandBuilder = default_receiver_command,
        stop_timeout: float = DEFAULT_START_TIMEOUT,
    ) -> None:
        self._sup = supervisor
        self._role = role
        self._build = command_builder
        self._stop_timeout = positive_timeout(stop_timeout, "pipeline stop timeout")
        self._handle: ProcessHandle | None = None
        self._want = False
        self._last_error: str | None = None

    def attach(self, producer: str, media: Body) -> None:
        argv = self._build(self._role, media)  # validates media, may raise
        self.detach()  # re-attach replaces any running pipeline
        try:
            self._handle = self._sup.start(argv)
        except DriverError as exc:
            self._last_error = str(exc)
            raise
        self._want = True
        self._last_error = None

    def detach(self) -> None:
        self._want = False
        handle = self._handle
        if handle is None:
            self._last_error = None
            return
        try:
            # Signal the owned group even if its leader has already exited.
            with contextlib.suppress(ProcessLookupError):
                handle.terminate()
            try:
                handle.wait(self._stop_timeout)
            except subprocess.TimeoutExpired:
                handle.kill()
                handle.wait(self._stop_timeout)
        except (subprocess.TimeoutExpired, OSError) as exc:
            self._last_error = f"pipeline teardown unconfirmed: {exc}"
            raise DriverError(self._last_error) from exc
        self._handle = None
        self._last_error = None

    def poll(self) -> DriverStatus:
        if self._last_error:
            return DriverStatus(NodeState.ERROR, self._last_error)
        if not self._want or self._handle is None:
            return DriverStatus(NodeState.IDLE)
        try:
            code = self._handle.poll()
        except OSError as exc:
            return DriverStatus(NodeState.ERROR, f"pipeline status failed: {exc}")
        if code is None:
            return DriverStatus(
                NodeState.ACTIVE, "pipeline running; media flow unverified"
            )
        # the pipeline exited on its own — that is an error while paired.
        self._last_error = f"pipeline exited with code {code}"
        return DriverStatus(NodeState.ERROR, self._last_error)

    def describe(self) -> Body | None:
        return None


class StreamProducer:
    """Producer-side helper: advertise the media params a consumer needs.
    A producer runs its own send pipeline (out of scope for the control
    plane); this just supplies the ``media`` dict for announces."""

    def __init__(
        self,
        group: str,
        port: int,
        *,
        pt: int = 96,
        encoding: str = "raw",
        **extra: object,
    ) -> None:
        media: Body = {
            "transport": "rtp",
            "framing": "standard-rtp",
            "group": group,
            "port": port,
            "pt": pt,
            "encoding": encoding,
            **extra,
        }
        _validate_media(media)  # fail fast on bad params
        media.setdefault("clock_rate", 90000)
        media.setdefault("encoding_name", RTP_NAMES[encoding])
        if encoding == "vc2":
            media.setdefault("profile", "HQ")
        self._media = media

    def media(self) -> Body:
        return dict(self._media)
