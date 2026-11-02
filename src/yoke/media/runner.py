"""Asyncio shell that runs a media sender/receiver over real UDP sockets.

This is the thin, imperative edge of the media plane — the analogue of
``yoke.net.runner`` for the control plane. All the logic lives in the
sans-I/O pipeline; this file only supplies sockets and a clock. Frame
production, wire (de)serialization, and feedback are exposed as small
methods the loopback tests and the debug CLI drive directly, so the socket
loop stays trivial.

The reference datapath is Python with synthetic frames and codec models.
Real codec/capture/display processes are separate integrations via the
``stream`` driver; this runner makes no codec or hardware-performance claim.
"""

from __future__ import annotations

import asyncio
import json
import math
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass

from yoke.media.assembly import Receiver, Sender
from yoke.media.feedback import receiver_report
from yoke.media.snoop import MediaSnooper, SnoopRecord
from yoke.media.sockets import ipv4_address, valid_port
from yoke.media.types import Body, EventKind, NetworkReport, PipelineOutput, StageEvent
from yoke.media.wire import WireError, pack_packet, unpack_packet

Clock = Callable[[], float]
FEEDBACK_MAGIC = b"YKFB"
MAX_FEEDBACK_BYTES = 1024
MAX_UDP_BYTES = 65507


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
        allow_nan=False,
    ).encode("utf-8")


def _decode_feedback(raw: bytes, now: float) -> NetworkReport | None:
    if not raw.startswith(FEEDBACK_MAGIC) or len(raw) > MAX_FEEDBACK_BYTES:
        return None
    try:
        obj = json.loads(raw[len(FEEDBACK_MAGIC) :].decode("utf-8"))
        if not isinstance(obj, dict):
            return None
        values = [obj["loss"], obj["jitter"], obj["congestion"]]
        if any(type(v) not in (int, float) for v in values):
            return None
        loss, jitter, congestion = map(float, values)
        if not all(math.isfinite(v) for v in (now, loss, jitter, congestion)):
            return None
        if not 0 <= loss <= 1 or jitter < 0 or not 0 <= congestion <= 1:
            return None
        frames_ok, frames_bad = obj.get("frames_ok", 0), obj.get("frames_bad", 0)
        if any(
            type(v) is not int or not 0 <= v <= (1 << 64) - 1
            for v in (frames_ok, frames_bad)
        ):
            return None
        return NetworkReport(
            now=now,
            loss=loss,
            jitter=jitter,
            congestion=congestion,
            frames_ok=frames_ok,
            frames_bad=frames_bad,
        )
    except (ValueError, KeyError, TypeError, OverflowError, RecursionError):
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
                raw = pack_packet(pkt)
                if len(raw) > MAX_UDP_BYTES:
                    raise ValueError("reference packet exceeds IPv4 UDP payload limit")
                datagrams.append(raw)
        self.stats.frames += 1
        self.stats.packets += len(datagrams)
        return datagrams

    def finish(self, now: float) -> list[bytes]:
        """Emit terminal sender output without sourcing another frame."""
        out = self._sender.pipeline.finish(now)
        datagrams = [pack_packet(packet) for packet in out.emitted]
        if any(len(raw) > MAX_UDP_BYTES for raw in datagrams):
            raise ValueError("reference packet exceeds IPv4 UDP payload limit")
        self.stats.packets += len(datagrams)
        return datagrams

    def on_feedback(self, raw: bytes, now: float) -> None:
        report = _decode_feedback(raw, now)
        if report is not None:
            self.stats.feedback_seen += 1
            self._sender.pipeline.feed_back(report, now)

    async def run(
        self,
        sock: socket.socket,
        *,
        frames: int,
        on_send: Callable[[int, float], None] | None = None,
    ) -> SenderStats:
        if type(frames) is not int or frames < 1:
            raise ValueError("frames must be a positive integer")
        loop = asyncio.get_running_loop()
        protocol = _RxProtocol(self.on_feedback, self._clock)
        transport, _ = await loop.create_datagram_endpoint(
            lambda: protocol,
            sock=sock,
        )
        self._transport = transport
        try:
            interval = 1.0 / self._sender.config.fps
            next_at = self._clock()
            for seq in range(frames):
                protocol.raise_if_error()
                now = self._clock()
                for datagram in self.emit_frame(now):
                    transport.sendto(datagram, self._dest)
                if on_send is not None:
                    on_send(seq, now)
                next_at += interval
                await asyncio.sleep(max(0.0, next_at - self._clock()))
            for datagram in self.finish(self._clock()):
                transport.sendto(datagram, self._dest)
        finally:
            self._transport = None
            await _close_transport(transport, protocol)
        protocol.raise_if_error()
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
        if feedback_dest is not None:
            ipv4_address(feedback_dest[0])
            if feedback_dest[0] == "0.0.0.0":
                raise ValueError("feedback destination must be a unicast host")
            valid_port(feedback_dest[1])
        if not math.isfinite(tick_interval) or tick_interval <= 0:
            raise ValueError("tick interval must be finite and positive")
        self._receiver = receiver
        self._feedback_dest = feedback_dest
        self._snooper = snooper
        self._clock = clock
        self._tick_interval = tick_interval
        self._transport: asyncio.DatagramTransport | None = None
        self.dropped = 0

    def on_datagram(self, raw: bytes, now: float) -> PipelineOutput:
        """Ingest one wire datagram; malformed ones are counted, not fatal."""
        if self._receiver.pipeline.finished:
            self.dropped += 1
            return PipelineOutput()
        try:
            unit = unpack_packet(raw)
        except (WireError, ValueError):
            self.dropped += 1
            return PipelineOutput()
        if self._snooper is not None:
            self._snooper.observe_event(
                StageEvent(
                    "wire", EventKind.IN, unit.seq, unit.pts, now, {"bytes": len(raw)}
                ),
                now,
            )
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
                report = receiver_report(report, self._receiver.pipeline)
                self._transport.sendto(_encode_feedback(report), self._feedback_dest)

    async def run(self, sock: socket.socket, *, duration: float) -> dict[str, object]:
        _valid_duration(duration)
        loop = asyncio.get_running_loop()
        protocol = _RxProtocol(self._ingest, self._clock)
        transport, _ = await loop.create_datagram_endpoint(
            lambda: protocol,
            sock=sock,
        )
        self._transport = transport
        deadline = self._clock() + duration
        try:
            while self._clock() < deadline:
                protocol.raise_if_error()
                self.tick(self._clock())
                await asyncio.sleep(self._tick_interval)
            self.tick(self._clock())  # final flush of due packets
            finished_at = self._clock()
            out = self._receiver.pipeline.finish(finished_at)
            self._route_events(out, finished_at)
        finally:
            self._transport = None
            await _close_transport(transport, protocol)
        protocol.raise_if_error()
        return self._receiver.sink.metrics()

    def _ingest(self, raw: bytes, now: float) -> None:
        self.on_datagram(raw, now)


class PassiveRunner:
    """Independent receive-only observer: no feedback or application send path."""

    def __init__(
        self,
        snooper: MediaSnooper,
        *,
        clock: Clock = time.monotonic,
        on_record: Callable[[SnoopRecord], None] | None = None,
    ) -> None:
        self._snooper = snooper
        self._clock = clock
        self._on_record = on_record
        self.packets = 0
        self.dropped = 0

    def on_datagram(self, raw: bytes, now: float) -> None:
        try:
            unpack_packet(raw)
        except ValueError:
            self.dropped += 1
            return
        self.packets += 1
        record = self._snooper.observe_packet(raw, now)
        if self._on_record is not None:
            self._on_record(record)

    def snapshot(self) -> Body:
        topology = self._snooper.topology()
        topology["observer"] = {
            "packets": self.packets,
            "malformed": self.dropped,
        }
        return topology

    async def run(
        self, sock: socket.socket, *, duration: float | None = None
    ) -> dict[str, object]:
        if duration is not None:
            _valid_duration(duration)
        protocol = _RxProtocol(self.on_datagram, self._clock)
        transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
            lambda: protocol, sock=sock
        )
        try:
            if duration is None:
                await asyncio.Future[None]()
            else:
                await asyncio.sleep(duration)
        finally:
            await _close_transport(transport, protocol)
        protocol.raise_if_error()
        return self.snapshot()


def _valid_duration(duration: float) -> None:
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("duration must be finite and positive")


async def _close_transport(
    transport: asyncio.DatagramTransport, protocol: _RxProtocol
) -> None:
    transport.close()
    await protocol.closed


class _RxProtocol(asyncio.DatagramProtocol):
    """Delivers inbound datagrams to a callback with a timestamp."""

    def __init__(self, on_data: Callable[[bytes, float], object], clock: Clock) -> None:
        self._on_data = on_data
        self._clock = clock
        self.closed: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.error: Exception | None = None

    def error_received(self, exc: Exception) -> None:
        self.error = exc

    def raise_if_error(self) -> None:
        if self.error is not None:
            raise self.error

    def connection_lost(self, exc: Exception | None) -> None:
        if exc is not None:
            self.error = exc
        if not self.closed.done():
            self.closed.set_result(None)

    def datagram_received(self, data: bytes, addr: object) -> None:
        self._on_data(data, self._clock())
