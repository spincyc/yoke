"""Comprehensive loopback (real-socket) verification of the media pipeline.

This is the SINGLE-HOST GATE. Every test here runs a real
:class:`~yoke.media.runner.SenderRunner` and
:class:`~yoke.media.runner.ReceiverRunner` against each other over genuine
UDP sockets on ``127.0.0.1`` inside one asyncio process, and verifies the
stream end-to-end (bit-exact where the codec is lossless). It is the
loopback half of the two-host media verification: ``make verify`` runs the
offline + loopback suite, and it MUST pass before kicking out to real
two-host verification (``make media-recv`` on the display host, ``make
media-send PEER=...`` on the source host). If the datapath is broken over
loopback it is broken on the wire; catching it here keeps the "one command
per host" two-host check honest.

Every test SKIPS (rather than fails) when the sandbox forbids UDP/loopback
sockets — bind or endpoint creation raising ``OSError`` becomes a
``pytest.skip`` — mirroring ``tests/test_mcast.py``, so the suite stays
truthful about what it could not check.

Tests are kept fast (<~2 s): high fps and small frame counts over tiny
model payloads, so the whole file settles well under the pytest budget.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import socket
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import cast

import pytest

from yoke.media.assembly import MediaConfig, build_receiver, build_sender
from yoke.media.runner import (
    FEEDBACK_MAGIC,
    ReceiverRunner,
    SenderRunner,
    SenderStats,
    _decode_feedback,
)
from yoke.media.snoop import MediaSnooper, SnoopRecord
from yoke.media.stages.jitter import JitterBuffer
from yoke.media.stages.packetize import RTP_HEADER_LEN, decode_rtp_header
from yoke.media.types import Body, EventKind, StageEvent

# High fps + few frames keeps every test tiny in wall-clock time.
FPS = 240
FRAMES = 90
# A datagram that is NOT a yoke media packet: the receiver must count it as
# dropped (WireError) and keep flowing.
GARBAGE = b"\xde\xad\xbe\xef not-a-yoke-media-packet \xff\xff\xff\xff"

UNSUPPORTED_SOCKET_ERRORS = {
    errno.EACCES,
    errno.EPERM,
    errno.EAFNOSUPPORT,
    errno.EPROTONOSUPPORT,
    errno.ENOPROTOOPT,
    errno.EOPNOTSUPP,
    errno.ENETUNREACH,
    errno.EADDRNOTAVAIL,
    errno.ENETDOWN,
}


def require_socket_support(exc: OSError) -> None:
    """Skip only proven environment restrictions; implementation errors fail."""
    if exc.errno in UNSUPPORTED_SOCKET_ERRORS:
        pytest.skip(f"loopback UDP unavailable in this environment: {exc}")
    raise exc


# --------------------------------------------------------------------------
# Real-socket harness
# --------------------------------------------------------------------------


def _bind_udp() -> socket.socket:
    """Bind a nonblocking UDP socket on an ephemeral 127.0.0.1 port.

    SO_REUSEADDR + nonblocking, ready to hand to asyncio's datagram
    endpoint. Raises ``OSError`` when the sandbox forbids sockets; callers
    turn that into a skip.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.setblocking(False)
        return sock
    except BaseException:
        sock.close()
        raise


def _rtp_header_body(raw: bytes) -> Body:
    """Black-box RTP header decoder for the snooper (``Callable[[bytes], Body]``).

    The production ``decode_rtp_header`` returns an ``RtpHeader`` dataclass;
    the snooper wants a plain dict, so adapt it here. Raises ``RtpError``
    (a ``ValueError``) on a malformed header, which the snooper treats as a
    malformed packet.
    """
    header = decode_rtp_header(raw)
    return {
        "seq": header.sequence,
        "timestamp": header.timestamp,
        "marker": header.marker,
        "pt": header.payload_type,
        "ssrc": header.ssrc,
    }


class _RecordingSnooper(MediaSnooper):
    """Passive snooper that also timestamps each sink INTEGRITY event.

    Non-interfering exactly like its base: it only observes. The extra
    ``integrity_at`` map (frame seq -> receiver clock at verification) lets
    the latency test match a frame's send time to its glass-to-glass
    arrival, using the one shared monotonic clock of this process.
    """

    def __init__(self, header_decoder: Callable[[bytes], Body]) -> None:
        super().__init__(header_decoder)
        self.integrity_at: dict[int, float] = {}

    def observe_event(self, event: StageEvent, now: float) -> SnoopRecord:
        record = super().observe_event(event, now)
        if event.stage == "sink" and event.kind is EventKind.INTEGRITY:
            self.integrity_at.setdefault(event.seq, now)
        return record


@dataclass
class Loopback:
    """What one real-socket run produced, for the test to assert on."""

    stats: SenderStats
    metrics: dict[str, object]
    dropped: int
    garbage_sent: int
    snooper: MediaSnooper | None


async def run_loopback(
    config: MediaConfig,
    *,
    frames: int = FRAMES,
    snooper: MediaSnooper | None = None,
    feedback: bool = True,
    on_send: Callable[[int, float], None] | None = None,
    garbage_every: int | None = None,
    pad: float = 0.5,
) -> Loopback:
    """Run a real sender and receiver over 127.0.0.1 for ``frames`` frames.

    Binds two UDP sockets, runs the ``ReceiverRunner`` as a task and the
    ``SenderRunner`` to completion, then drains and returns the sender
    stats, the receiver's sink metrics, its malformed-datagram count, and
    the optional snooper's topology source. Any ``OSError`` from binding or
    endpoint creation becomes a skip.
    """
    sockets: list[socket.socket] = []
    try:
        sockets.append(_bind_udp())
        sockets.append(_bind_udp())
    except OSError as exc:  # pragma: no cover - environment-dependent
        for opened in sockets:
            opened.close()
        require_socket_support(exc)
        raise AssertionError("unreachable") from exc
    rx_sock, tx_sock = sockets

    rx_addr = rx_sock.getsockname()
    tx_addr = tx_sock.getsockname()

    sender = SenderRunner(build_sender(config), dest=rx_addr)
    receiver = ReceiverRunner(
        build_receiver(config),
        feedback_dest=tx_addr if feedback else None,
        snooper=snooper,
    )
    duration = frames / config.fps + pad

    garbage_sock: socket.socket | None = None
    garbage_sent = 0

    def _on_send(seq: int, now: float) -> None:
        nonlocal garbage_sent
        if garbage_sock is not None and garbage_every and seq % garbage_every == 0:
            garbage_sock.sendto(GARBAGE, rx_addr)
            garbage_sent += 1
        if on_send is not None:
            on_send(seq, now)

    metrics: dict[str, object] = {}
    try:
        if garbage_every is not None:
            garbage_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        recv_task: asyncio.Task[dict[str, object]] = asyncio.create_task(
            receiver.run(rx_sock, duration=duration)
        )
        try:
            await sender.run(tx_sock, frames=frames, on_send=_on_send)
            metrics = await recv_task
        finally:
            if not recv_task.done():
                recv_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await recv_task
    except OSError as exc:  # pragma: no cover - environment-dependent
        for leaked in (rx_sock, tx_sock):
            with contextlib.suppress(OSError):
                leaked.close()
        require_socket_support(exc)
    finally:
        rx_sock.close()
        tx_sock.close()
        if garbage_sock is not None:
            garbage_sock.close()

    return Loopback(
        stats=sender.stats,
        metrics=metrics,
        dropped=receiver.dropped,
        garbage_sent=garbage_sent,
        snooper=snooper,
    )


# --------------------------------------------------------------------------
# metric accessors (sink.metrics() is dict[str, object])
# --------------------------------------------------------------------------


def _int(metrics: Mapping[str, object], key: str) -> int:
    value = metrics[key]
    assert isinstance(value, int), f"{key}={value!r} is not an int"
    return value


def _float(metrics: Mapping[str, object], key: str) -> float:
    value = metrics[key]
    assert isinstance(value, (int, float)), f"{key}={value!r} is not a number"
    return float(value)


def _assert_clean(metrics: Mapping[str, object], frames: int) -> None:
    """A clean run: every frame verified ok (allowing one still in flight),
    nothing bad, lost, duplicated, or reordered."""
    frames_ok = _int(metrics, "frames_ok")
    assert frames - 1 <= frames_ok <= frames, f"frames_ok={frames_ok} vs {frames}"
    assert _int(metrics, "frames_bad") == 0
    assert _int(metrics, "lost") == 0
    assert _int(metrics, "duplicated") == 0
    assert _int(metrics, "reordered") == 0


# --------------------------------------------------------------------------
# 1. Clean loopback: bit-exact over the real socket
# --------------------------------------------------------------------------


async def test_clean_loopback_is_bit_exact() -> None:
    result = await run_loopback(MediaConfig(width=32, height=32, fps=FPS))
    _assert_clean(result.metrics, FRAMES)
    assert result.dropped == 0
    # Raw path is lossless: frames_ok requires digest_of(payload) == source
    # digest, i.e. the frame survived the wire bit-for-bit.
    assert _int(result.metrics, "frames_ok") == FRAMES
    assert result.stats.frames == FRAMES
    assert result.stats.packets >= FRAMES  # at least one packet per frame


# --------------------------------------------------------------------------
# 2. Passive snooper reconstructs the stream without interfering
# --------------------------------------------------------------------------


async def test_snooper_reconstructs_stream_and_does_not_interfere() -> None:
    config = MediaConfig(width=32, height=32, fps=FPS)

    # Same run, once observed, once not: the metrics must be identical
    # (observation is non-interfering — the pipeline's forward output does
    # not depend on whether a snooper is attached).
    snooper = MediaSnooper(_rtp_header_body)
    observed = await run_loopback(config, snooper=snooper)
    plain = await run_loopback(config)

    counters = ("frames_ok", "frames_bad", "lost", "duplicated", "reordered")
    observed_counts = {k: _int(observed.metrics, k) for k in counters}
    plain_counts = {k: _int(plain.metrics, k) for k in counters}
    assert observed_counts == plain_counts, "snooper perturbed the stream"
    assert observed_counts["frames_ok"] == FRAMES
    assert plain_counts["frames_ok"] == FRAMES

    # The passive snooper reconstructs the whole stream from what it saw.
    topo = snooper.topology()
    streams = topo["streams"]
    assert isinstance(streams, dict)

    # Black-box: the wire view for this ssrc reconstructs every packet and
    # every frame, with zero loss, purely from the datagrams.
    stream_id = f"{config.ssrc & 0xFFFFFFFF:08x}"
    wire = streams[stream_id]["wire"]
    assert wire["packets"] == observed.stats.packets  # every packet seen
    assert wire["frames"] == FRAMES  # one RTP timestamp per frame
    assert wire["marker_frames"] == FRAMES  # one end-of-frame marker each
    assert wire["loss"] == 0
    assert wire["reorders"] == 0
    assert wire["dups"] == 0

    # White-box: per-stage counters were folded from the receiver taps —
    # the snooper reconstructs which stages the stream traversed.
    stages = topo["stages"]
    assert isinstance(stages, dict)
    for stage in ("jitter", "depacketize", "codec-decode", "sink"):
        assert stage in stages, f"missing stage {stage} in reconstruction"
    assert stages["sink"]["integrity_ok"] == FRAMES
    assert stages["sink"]["integrity_bad"] == 0


# --------------------------------------------------------------------------
# 3. Malformed datagrams are counted; good frames still arrive
# --------------------------------------------------------------------------


async def test_malformed_datagrams_counted_and_pipeline_flows() -> None:
    config = MediaConfig(width=32, height=32, fps=FPS)
    garbage_every = 10
    result = await run_loopback(config, garbage_every=garbage_every)

    expected_garbage = len(range(0, FRAMES, garbage_every))
    assert result.garbage_sent == expected_garbage
    # Every injected garbage datagram was counted as dropped (WireError),
    # and none was miscounted as a frame.
    assert result.dropped == expected_garbage
    # The good stream kept flowing right through the interleaved garbage.
    _assert_clean(result.metrics, FRAMES)
    assert _int(result.metrics, "frames_ok") == FRAMES


# --------------------------------------------------------------------------
# 4. Feedback loop: receiver -> sender YKFB reports arrive
# --------------------------------------------------------------------------


async def test_feedback_reports_reach_sender() -> None:
    config = MediaConfig(width=32, height=32, fps=FPS)
    result = await run_loopback(config, feedback=True)
    # The receiver emitted NetworkReports; the sender decoded YKFB
    # datagrams over the real socket and fed its rate controller.
    assert result.stats.feedback_seen > 0
    _assert_clean(result.metrics, FRAMES)


# --------------------------------------------------------------------------
# 5. jpegxs (RatioCodec) end-to-end over loopback
# --------------------------------------------------------------------------


async def test_jpegxs_encoding_end_to_end() -> None:
    config = MediaConfig(width=32, height=32, fps=FPS, encoding="jpegxs")
    result = await run_loopback(config)
    # Lossy path: integrity is structural (embedded frame id survives), so
    # frames verify ok and none are flagged bad despite the modelled codec.
    assert _int(result.metrics, "frames_ok") == FRAMES
    assert _int(result.metrics, "frames_bad") == 0
    assert _int(result.metrics, "lost") == 0
    assert result.dropped == 0


# --------------------------------------------------------------------------
# 6. dup FEC (ST 2022-7): both copies cross the wire, merger dedups
# --------------------------------------------------------------------------


async def test_dup_fec_dedups_over_loopback() -> None:
    config = MediaConfig(width=32, height=32, fps=FPS, fec="dup")
    result = await run_loopback(config)
    _assert_clean(result.metrics, FRAMES)
    assert _int(result.metrics, "frames_ok") == FRAMES
    # No duplicate leaked to the sink: the SeamlessMerger dropped the
    # second copy of every RTP sequence.
    assert _int(result.metrics, "duplicated") == 0

    # Both copies really traversed the socket: dup FEC doubles the packet
    # count versus the same config without FEC.
    plain = await run_loopback(MediaConfig(width=32, height=32, fps=FPS))
    assert result.stats.packets == 2 * plain.stats.packets


# --------------------------------------------------------------------------
# 7. Jumbo vs small MTU: multi-fragment slices reassemble bit-exact
# --------------------------------------------------------------------------


async def test_small_mtu_multifragment_is_bit_exact() -> None:
    # The model frame payload is a few hundred bytes, so jumbo/1500 MTUs
    # never fragment it. A deliberately small MTU forces several fragments
    # per slice across the wire; a lossless (raw) frames_ok proves the
    # depacketizer reassembled every fragment bit-for-bit.
    jumbo = await run_loopback(MediaConfig(width=32, height=32, fps=FPS, mtu=9000))
    small = await run_loopback(MediaConfig(width=32, height=32, fps=FPS, mtu=125))

    _assert_clean(jumbo.metrics, FRAMES)
    _assert_clean(small.metrics, FRAMES)
    assert _int(small.metrics, "frames_ok") == FRAMES
    assert _int(small.metrics, "frames_bad") == 0

    # More fragments per frame => strictly more packets on the wire than
    # the one-fragment-per-slice jumbo run.
    assert small.stats.packets > jumbo.stats.packets
    # And genuinely multi-fragment: well over one packet per slice.
    assert small.stats.packets > FRAMES * MediaConfig().n_slices


# --------------------------------------------------------------------------
# 8. Real glass-to-glass-ish latency over loopback (same clock)
# --------------------------------------------------------------------------


async def test_loopback_latency_is_small(
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = MediaConfig(width=32, height=32, fps=FPS)
    frames = 120

    send_at: dict[int, float] = {}

    def record_send(seq: int, now: float) -> None:
        send_at[seq] = now

    snooper = _RecordingSnooper(_rtp_header_body)
    wall_start = time.monotonic()
    result = await run_loopback(
        config, frames=frames, snooper=snooper, on_send=record_send
    )
    wall_elapsed = time.monotonic() - wall_start

    _assert_clean(result.metrics, frames)
    assert _int(result.metrics, "frames_ok") == frames

    # Match each frame's send time to its sink INTEGRITY time. Sender and
    # receiver share this process's monotonic clock, so the difference is a
    # real loopback glass-to-glass latency (not a simulated one).
    matched = sorted(send_at.keys() & snooper.integrity_at.keys())
    assert len(matched) >= frames - 1, "could not match per-frame timings"
    latencies = [snooper.integrity_at[seq] - send_at[seq] for seq in matched]
    mean_latency = sum(latencies) / len(latencies)
    max_latency = max(latencies)

    # Report the measured number (visible with `pytest -s`).
    with capsys.disabled():
        print(
            f"\nloopback latency: mean={mean_latency * 1e3:.2f} ms "
            f"max={max_latency * 1e3:.2f} ms over {len(matched)} frames"
        )

    assert 0.0 < mean_latency < 0.1, f"mean loopback latency {mean_latency:.4f}s"
    assert max_latency < 0.1, f"max loopback latency {max_latency:.4f}s"
    # Total wall time stays bounded (send cadence + a small drain pad).
    assert wall_elapsed < frames / FPS + 1.0


# A tiny guard so the RTP adapter/const import is exercised even if a
# reader greps for RTP_HEADER_LEN usage; keeps the wire contract explicit.
def test_rtp_header_adapter_roundtrips() -> None:
    from yoke.media.stages.packetize import encode_rtp_header

    raw = encode_rtp_header(
        marker=True, payload_type=96, sequence=7, timestamp=1234, ssrc=0x5A5A5A5A
    )
    assert len(raw) == RTP_HEADER_LEN
    body = _rtp_header_body(raw)
    assert body == {
        "seq": 7,
        "timestamp": 1234,
        "marker": True,
        "pt": 96,
        "ssrc": 0x5A5A5A5A,
    }


@pytest.mark.parametrize(
    "raw",
    [
        b"not-feedback",
        FEEDBACK_MAGIC + b"[]",
        FEEDBACK_MAGIC + b"null",
        FEEDBACK_MAGIC + b'{"loss":NaN,"jitter":0,"congestion":0}',
        FEEDBACK_MAGIC + b'{"loss":0,"jitter":Infinity,"congestion":0}',
        FEEDBACK_MAGIC + b'{"loss":0,"jitter":0,"congestion":-Infinity}',
        FEEDBACK_MAGIC + b'{"loss":1e1000,"jitter":0,"congestion":0}',
        FEEDBACK_MAGIC + b'{"loss":true,"jitter":0,"congestion":0}',
        FEEDBACK_MAGIC + b'{"loss":"0","jitter":0,"congestion":0}',
        FEEDBACK_MAGIC + b'{"loss":1.1,"jitter":0,"congestion":0}',
        FEEDBACK_MAGIC + b'{"loss":0,"jitter":-1,"congestion":0}',
        FEEDBACK_MAGIC + b'{"loss":0,"jitter":0,"congestion":-1}',
        FEEDBACK_MAGIC + b'{"loss":0,"jitter":0,"congestion":0,"frames_ok":true}',
        FEEDBACK_MAGIC + b'{"loss":0,"jitter":0,"congestion":0,"frames_bad":0.5}',
        FEEDBACK_MAGIC + b'{"loss":0,"jitter":0,"congestion":0,"frames_bad":-1}',
        FEEDBACK_MAGIC + b"\xff",
        FEEDBACK_MAGIC + b"[" * 2000,
    ],
)
def test_malformed_feedback_cannot_change_rate_control(raw: bytes) -> None:
    sender = build_sender(MediaConfig(width=32, height=32))
    runner = SenderRunner(sender, dest=("127.0.0.1", 5004))
    before = (sender.rate_state.target, runner.stats.feedback_seen)
    runner.on_feedback(raw, 0.0)
    assert (sender.rate_state.target, runner.stats.feedback_seen) == before
    assert _decode_feedback(raw, 0.0) is None


def test_feedback_valid_numeric_report_is_accepted() -> None:
    raw = (
        FEEDBACK_MAGIC
        + json.dumps(
            {
                "loss": 0.1,
                "jitter": 0.02,
                "congestion": 1,
                "frames_ok": 12,
                "frames_bad": 3,
            }
        ).encode()
    )
    report = _decode_feedback(raw, 123.0)
    assert report is not None
    assert report.now == 123.0 and report.loss == 0.1
    assert report.frames_ok == 12 and report.frames_bad == 3


@pytest.mark.parametrize("dest", [("239.1.2.3", 5004), ("0.0.0.0", 5004)])
def test_receiver_forbids_automatic_or_multicast_feedback(
    dest: tuple[str, int],
) -> None:
    with pytest.raises(ValueError):
        ReceiverRunner(build_receiver(MediaConfig()), feedback_dest=dest)


@pytest.mark.parametrize("duration", [0.0, -1.0, float("nan"), float("inf")])
async def test_invalid_duration_does_not_open_transport(duration: float) -> None:
    receiver = ReceiverRunner(build_receiver(MediaConfig()))
    with pytest.raises(ValueError, match="duration"):
        await receiver.run(cast(socket.socket, object()), duration=duration)


async def test_receiver_cancellation_closes_socket_and_clears_transport() -> None:
    try:
        sock = _bind_udp()
    except OSError as exc:
        require_socket_support(exc)
        raise AssertionError("unreachable") from exc
    receiver = ReceiverRunner(build_receiver(MediaConfig()))
    task = asyncio.create_task(receiver.run(sock, duration=60))
    try:
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert sock.fileno() == -1
        assert receiver._transport is None
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        sock.close()


async def test_sender_callback_failure_closes_socket_and_clears_transport() -> None:
    sockets: list[socket.socket] = []
    try:
        sockets.append(_bind_udp())
        sockets.append(_bind_udp())
        receiver_sock, sender_sock = sockets
        runner = SenderRunner(
            build_sender(MediaConfig()), dest=receiver_sock.getsockname()
        )

        def fail(seq: int, now: float) -> None:
            raise RuntimeError("injected send callback failure")

        with pytest.raises(RuntimeError, match="injected send callback failure"):
            await runner.run(sender_sock, frames=2, on_send=fail)
        assert sender_sock.fileno() == -1
        assert runner._transport is None
    except OSError as exc:
        require_socket_support(exc)
    finally:
        for sock in sockets:
            sock.close()


def test_receiver_feedback_jitter_uses_arrivals_without_source_clock() -> None:
    class RecordingTransport(asyncio.DatagramTransport):
        def __init__(self) -> None:
            super().__init__()
            self.reports: list[bytes] = []

        def sendto(self, data: bytes, addr: object = None) -> None:
            self.reports.append(data)

    config = MediaConfig(width=32, height=32, fps=30)
    receiver = build_receiver(config, report_every=1)
    runner = ReceiverRunner(receiver, feedback_dest=("127.0.0.1", 12345))
    transport = RecordingTransport()
    runner._transport = transport
    sender = SenderRunner(build_sender(config), dest=("127.0.0.1", 5004))
    for seq in range(30):
        now = seq / config.fps
        arrival = now + (0.015 if seq % 2 else 0)
        runner.tick(arrival)
        for raw in sender.emit_frame(now):
            runner.on_datagram(raw, arrival)
    runner.tick(1.5)
    reports = [_decode_feedback(raw, 1.5) for raw in transport.reports]
    assert reports and all(report is not None for report in reports)
    assert any(report is not None and report.jitter > 0 for report in reports)
    jitter = next(
        stage for stage in receiver.pipeline.stages if isinstance(stage, JitterBuffer)
    )
    assert reports[-1] is not None
    assert reports[-1].jitter == pytest.approx(jitter.jitter_estimate())
    assert receiver.sink.metrics()["mean_latency"] is None
    assert receiver.sink.metrics()["last_latency"] is None


async def test_finite_runners_finish_pipelines_and_ignore_late_arrivals() -> None:
    sockets: list[socket.socket] = []
    try:
        sockets.append(_bind_udp())
        sockets.append(_bind_udp())
        rx_sock, tx_sock = sockets
        source = build_sender(MediaConfig(width=32, height=32))
        sink = build_receiver(source.config)
        sender = SenderRunner(source, dest=rx_sock.getsockname())
        receiver = ReceiverRunner(sink)
        receive_task = asyncio.create_task(receiver.run(rx_sock, duration=0.05))
        try:
            await sender.run(tx_sock, frames=1)
            await receive_task
        finally:
            if not receive_task.done():
                receive_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await receive_task
        assert source.pipeline.finished and sink.pipeline.finished
        assert sender.finish(1) == []
        before = sink.sink.metrics()
        assert receiver.on_datagram(GARBAGE, 1).emitted == []
        assert sink.sink.metrics() == before
    except OSError as exc:
        require_socket_support(exc)
    finally:
        for sock in sockets:
            sock.close()
