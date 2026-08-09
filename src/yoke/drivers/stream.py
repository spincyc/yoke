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
import shutil
import subprocess
from collections.abc import Callable, Sequence
from typing import Protocol

from yoke.core.types import Body, DriverStatus, NodeState
from yoke.drivers.base import DriverError

ENCODINGS = frozenset({"raw", "jpegxs", "vc2", "h265"})
DEFAULT_START_TIMEOUT = 5.0


class ProcessHandle(Protocol):
    """An opaque handle to a running media pipeline process."""

    def poll(self) -> int | None:
        """Return the exit code, or None if still running."""

    def terminate(self) -> None: ...

    def wait(self, timeout: float) -> int: ...


class ProcessSupervisor(Protocol):
    """Starts and stops long-running pipeline processes. Injected so the
    driver is testable without spawning anything."""

    def start(self, argv: Sequence[str]) -> ProcessHandle: ...


class SubprocessSupervisor:
    """Real supervisor: launches the pipeline with ``subprocess.Popen``."""

    def start(self, argv: Sequence[str]) -> ProcessHandle:
        if not argv:
            raise DriverError("empty pipeline command")
        exe = shutil.which(argv[0])
        if exe is None:
            raise DriverError(f"pipeline command not found: {argv[0]}")
        try:
            return subprocess.Popen(
                [exe, *argv[1:]],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise DriverError(f"failed to start pipeline: {exc}") from exc


CommandBuilder = Callable[[str, Body], list[str]]


def _validate_media(media: Body) -> tuple[str, int, int, str]:
    """Validate the advertised media dict; return (group, port, pt, enc)."""
    if media.get("transport") != "rtp":
        raise DriverError(f"stream media transport must be 'rtp': {media!r}")
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
    if encoding not in ENCODINGS:
        raise DriverError(f"stream media 'encoding' unknown: {encoding!r}")
    # unicast group is allowed; multicast is the common case. Only reject
    # obviously wrong (loopback/unspecified) addresses.
    if addr.is_unspecified:
        raise DriverError("stream media 'group' is unspecified")
    return group, port, pt, encoding


def default_receiver_command(role: str, media: Body) -> list[str]:
    """Reference GStreamer receive pipeline for a consumer. Bench-tunable;
    the exact caps/decoder per encoding are confirmed on hardware (see
    doc/research/prototype/BUILD-PLAN.md). Isolated here so a firmware/
    tooling change touches one function."""
    group, port, pt, encoding = _validate_media(media)
    depay = {
        "raw": "rtpvrawdepay",
        "jpegxs": "rtpjpegxsdepay",
        "vc2": "rtpvc2depay",
        "h265": "rtph265depay",
    }[encoding]
    decode = {
        "raw": "identity",
        "jpegxs": "svtjpegxsdec",
        "vc2": "avdec_vc2",
        "h265": "avdec_h265",
    }[encoding]
    return [
        "gst-launch-1.0",
        "-q",
        "udpsrc",
        f"address={group}",
        f"port={port}",
        f"caps=application/x-rtp,media=video,payload={pt}",
        "!",
        "rtpjitterbuffer",
        "latency=10",
        "!",
        depay,
        "!",
        decode,
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
        self._stop_timeout = stop_timeout
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
        self._handle = None
        if handle is None:
            return
        if handle.poll() is None:
            handle.terminate()
            # Best-effort; detach must never raise.
            with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                handle.wait(self._stop_timeout)

    def poll(self) -> DriverStatus:
        if not self._want or self._handle is None:
            return DriverStatus(NodeState.IDLE, self._last_error or "")
        code = self._handle.poll()
        if code is None:
            return DriverStatus(NodeState.ACTIVE)
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
            "group": group,
            "port": port,
            "pt": pt,
            "encoding": encoding,
            **extra,
        }
        _validate_media(media)  # fail fast on bad params
        self._media = media

    def media(self) -> Body:
        return dict(self._media)
