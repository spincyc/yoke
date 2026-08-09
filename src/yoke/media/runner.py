"""Asyncio shell that runs a media sender/receiver over real UDP sockets.

This is the thin, imperative edge of the media plane — the analogue of
``yoke.net.runner`` for the control plane. All the logic lives in the
sans-I/O pipeline; this file only supplies sockets and a clock. Frame
production, wire (de)serialization, and feedback are exposed as small
methods the loopback tests and the debug CLI drive directly, so the socket
loop stays trivial.

The reference datapath is Python; production media moves through
GStreamer/MTL (the ``stream`` driver). This runner exists to exercise and
verify the whole pipeline over loopback and two hosts.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass

from yoke.media.assembly import Receiver, Sender
from yoke.media.snoop import MediaSnooper
from yoke.media.types import NetworkReport, PipelineOutput
from yoke.media.wire import WireError, pack_packet, unpack_packet

Clock = Callable[[], float]
FEEDBACK_MAGIC = b"YKFB"


def _encode_feedback(report: NetworkReport) -> bytes:
    return FEEDBACK_MAGIC + json.dumps(
        {
            "loss": report.loss,
            "jitter": report.jitter,
            "congestion": report.congestion,
            "frames_ok": report.frames_ok,
            "frames_bad": report.frames_bad,
        },
        separators=(",", ":"),
    ).encode("utf-8")


def _decode_feedback(raw: bytes, now: float) -> NetworkReport | None:
    if not raw.startswith(FEEDBACK_MAGIC):
        return None
    try:
        obj = json.loads(raw[len(FEEDBACK_MAGIC) :].decode("utf-8"))
        return NetworkReport(
            now=now,
            loss=float(obj["loss"]),
            jitter=float(obj["jitter"]),
            congestion=float(obj["congestion"]),
            frames_ok=int(obj.get("frames_ok", 0)),
            frames_bad=int(obj.get("frames_bad", 0)),
        )
    except (ValueError, KeyError, TypeError):
        return None


@dataclass
class SenderStats:
    frames: int = 0
    packets: int = 0
    feedback_seen: int = 0


class SenderRunner:
    """Produces frames at the source's cadence and sends packets to
    ``dest``; consumes feedback datagrams to drive rate control."""

    def __init__(
        self,
        sender: Sender,
        *,
        dest: tuple[str, int],
        clock: Clock = time.monotonic,
    ) -> None:
        self._sender = sender
        self._dest = dest
        self._clock = clock
        self._transport: asyncio.DatagramTransport | None = None
        self.stats = SenderStats()

    def emit_frame(self, now: float) -> list[bytes]:
        """Produce one frame's worth of wire datagrams (pure)."""
        datagrams: list[bytes] = []
        fo = self._sender.source.frame(now)
        for frame in fo.emit:
            out = self._sender.pipeline.push(frame, now)
            for pkt in out.emitted:
                datagrams.append(pack_packet(pkt))
        self.stats.frames += 1
        self.stats.packets += len(datagrams)
        return datagrams

    def on_feedback(self, raw: bytes, now: float) -> None:
        report = _decode_feedback(raw, now)
        if report is not None:
            self.stats.feedback_seen += 1
            self._sender.pipeline.feed_back(report, now)

    async def run(
        self,
        sock: object,
        *,
        frames: int,
        on_send: Callable[[int, float], None] | None = None,
    ) -> SenderStats:
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _RxProtocol(self.on_feedback, self._clock),
            sock=sock,  # type: ignore[arg-type]
        )
        self._transport = transport
        try:
            interval = 1.0 / self._sender.config.fps
            next_at = self._clock()
            for seq in range(frames):
                now = self._clock()
                for datagram in self.emit_frame(now):
                    transport.sendto(datagram, self._dest)
                if on_send is not None:
                    on_send(seq, now)
                next_at += interval
                await asyncio.sleep(max(0.0, next_at - self._clock()))
        finally:
            transport.close()
        return self.stats


class ReceiverRunner:
    """Receives packets, runs them through the receiver pipeline, ticks the
    jitter buffer, and returns metrics; emits feedback to ``feedback_dest``
    and optionally feeds a passive snooper."""

    def __init__(
        self,
        receiver: Receiver,
        *,
        feedback_dest: tuple[str, int] | None = None,
        snooper: MediaSnooper | None = None,
        clock: Clock = time.monotonic,
        tick_interval: float = 0.005,
    ) -> None:
        self._receiver = receiver
        self._feedback_dest = feedback_dest
        self._snooper = snooper
        self._clock = clock
        self._tick_interval = tick_interval
        self._transport: asyncio.DatagramTransport | None = None
        self.dropped = 0

    def on_datagram(self, raw: bytes, now: float) -> PipelineOutput:
        """Ingest one wire datagram; malformed ones are counted, not fatal."""
        try:
            unit = unpack_packet(raw)
        except (WireError, ValueError):
            self.dropped += 1
            return PipelineOutput()
        if self._snooper is not None:
            self._snooper.observe_packet(unit, now)
        out = self._receiver.pipeline.push(unit, now)
        self._route_events(out, now)
        return out

    def tick(self, now: float) -> PipelineOutput:
        out = self._receiver.pipeline.tick(now)
        self._route_events(out, now)
        return out

    def _route_events(self, out: PipelineOutput, now: float) -> None:
        if self._snooper is not None:
            for event in out.events:
                self._snooper.observe_event(event, now)
        if self._transport is not None and self._feedback_dest is not None:
            for report in out.feedback:
                self._transport.sendto(_encode_feedback(report), self._feedback_dest)

    async def run(self, sock: object, *, duration: float) -> dict[str, object]:
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _RxProtocol(self._ingest, self._clock),
            sock=sock,  # type: ignore[arg-type]
        )
        self._transport = transport
        deadline = self._clock() + duration
        try:
            while self._clock() < deadline:
                self.tick(self._clock())
                await asyncio.sleep(self._tick_interval)
            self.tick(self._clock())  # final flush of due packets
        finally:
            transport.close()
        return self._receiver.sink.metrics()

    def _ingest(self, raw: bytes, now: float) -> None:
        self.on_datagram(raw, now)


class _RxProtocol(asyncio.DatagramProtocol):
    """Delivers inbound datagrams to a callback with a timestamp."""

    def __init__(self, on_data: Callable[[bytes, float], object], clock: Clock) -> None:
        self._on_data = on_data
        self._clock = clock

    def datagram_received(self, data: bytes, addr: object) -> None:
        self._on_data(data, self._clock())
