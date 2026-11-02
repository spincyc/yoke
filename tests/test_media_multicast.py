"""Real IPv4 multicast fanout and a separate, receive-only debug listener."""

from __future__ import annotations

import asyncio
import contextlib
import socket
import time
from dataclasses import dataclass

import pytest

from tests.test_media_loopback import require_socket_support
from yoke.media.assembly import MediaConfig, build_receiver, build_sender
from yoke.media.runner import PassiveRunner, ReceiverRunner, SenderRunner, SenderStats
from yoke.media.snoop import MediaSnooper, SnoopRecord
from yoke.media.sockets import (
    MulticastOptions,
    open_receiver_socket,
    open_sender_socket,
)
from yoke.media.types import Body
from yoke.media.wire import rtp_header_body

GROUP = "239.255.42.77"
OPTIONS = MulticastOptions(GROUP, interface="127.0.0.1", ttl=0, loopback=True)
FRAMES = 36
FPS = 120


class _RecordingSender(SenderRunner):
    def __init__(self, config: MediaConfig, dest: tuple[str, int]) -> None:
        super().__init__(build_sender(config), dest=dest)
        self.sent: list[bytes] = []

    def emit_frame(self, now: float) -> list[bytes]:
        raw = super().emit_frame(now)
        self.sent.extend(raw)
        return raw


@dataclass
class _Run:
    stats: SenderStats
    sent: list[bytes]
    metrics: Body
    observer: PassiveRunner | None
    topology: Body
    records: list[SnoopRecord]


def _unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def _run_multicast(
    *, observe: bool, fec: str = "none", feedback: bool = False
) -> _Run:
    sockets: list[socket.socket] = []
    tasks: list[asyncio.Task[Body]] = []
    records: list[SnoopRecord] = []
    try:
        port = _unused_port()
        rx_sock = open_receiver_socket(port, multicast=OPTIONS)
        sockets.append(rx_sock)
        tx_sock = open_sender_socket(multicast=OPTIONS)
        sockets.append(tx_sock)
        # The sender must never share the group's destination port.
        assert tx_sock.getsockname()[1] != port
        config = MediaConfig(width=32, height=32, fps=FPS, fec=fec)
        sender = _RecordingSender(config, (GROUP, port))
        feedback_dest = ("127.0.0.1", tx_sock.getsockname()[1]) if feedback else None
        receiver = ReceiverRunner(build_receiver(config), feedback_dest=feedback_dest)
        duration = FRAMES / FPS + 0.35
        tasks.append(asyncio.create_task(receiver.run(rx_sock, duration=duration)))

        observer = None
        if observe:
            observer_sock = open_receiver_socket(port, multicast=OPTIONS)
            sockets.append(observer_sock)
            observer = PassiveRunner(
                MediaSnooper(rtp_header_body), on_record=records.append
            )
            tasks.append(
                asyncio.create_task(observer.run(observer_sock, duration=duration))
            )

        await sender.run(tx_sock, frames=FRAMES)
        results = await asyncio.gather(*tasks)
        assert receiver.dropped == 0
        return _Run(
            sender.stats,
            sender.sent,
            results[0],
            observer,
            results[1] if observer is not None else {},
            records,
        )
    except OSError as exc:
        require_socket_support(exc)
        raise AssertionError("unreachable") from exc
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for sock in sockets:
            sock.close()


@pytest.mark.parametrize("fec", ["none", "dup"])
async def test_real_multicast_receiver_and_independent_observer_fanout(
    fec: str,
) -> None:
    observed = await _run_multicast(observe=True, fec=fec)
    assert observed.metrics["frames_ok"] == FRAMES
    assert observed.metrics["frames_bad"] == 0
    assert observed.metrics["lost"] == 0
    assert observed.observer is not None
    assert observed.observer.packets == observed.stats.packets
    assert observed.observer.dropped == 0
    assert observed.stats.feedback_seen == 0
    assert len(observed.records) == observed.stats.packets
    # Real timestamps are useful for bitrate/arrival reconstruction.
    assert observed.records[0].now > 0
    assert observed.records[-1].now > observed.records[0].now
    streams = observed.topology["streams"]
    assert isinstance(streams, dict) and len(streams) == 1
    wire = next(iter(streams.values()))["wire"]
    assert wire["packets"] == observed.stats.packets
    assert wire["frames"] == FRAMES
    assert wire["marker_frames"] == FRAMES


async def test_independent_observer_does_not_change_sender_or_receiver() -> None:
    observed = await _run_multicast(observe=True)
    baseline = await _run_multicast(observe=False)
    assert observed.sent == baseline.sent
    assert observed.stats == baseline.stats
    counters = ("frames_ok", "frames_bad", "lost", "duplicated", "reordered")
    assert {k: observed.metrics[k] for k in counters} == {
        k: baseline.metrics[k] for k in counters
    }


async def test_only_designated_multicast_receiver_sends_unicast_feedback() -> None:
    observed = await _run_multicast(observe=True, feedback=True)
    baseline = await _run_multicast(observe=False, feedback=True)
    assert observed.stats.feedback_seen > 0
    assert observed.stats.feedback_seen == baseline.stats.feedback_seen
    assert observed.sent == baseline.sent
    assert observed.metrics["frames_ok"] == FRAMES


async def test_passive_listener_sends_no_application_datagrams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Record every asyncio UDP send: the observer's shared port sends none."""
    from asyncio.selector_events import _SelectorDatagramTransport

    original = _SelectorDatagramTransport.sendto
    sent_from: list[int] = []

    def record_send(
        transport: _SelectorDatagramTransport, data: bytes, addr: object = None
    ) -> None:
        sent_from.append(transport.get_extra_info("sockname")[1])
        original(transport, data, addr)

    monkeypatch.setattr(_SelectorDatagramTransport, "sendto", record_send)
    run = await _run_multicast(observe=True)
    assert len(sent_from) == run.stats.packets
    assert len(set(sent_from)) == 1  # sender's ephemeral port only


async def test_observer_cancellation_closes_membership_socket() -> None:
    try:
        sock = open_receiver_socket(_unused_port(), multicast=OPTIONS)
    except OSError as exc:
        require_socket_support(exc)
        raise AssertionError("unreachable") from exc
    observer = PassiveRunner(MediaSnooper(rtp_header_body))
    task = asyncio.create_task(observer.run(sock))
    try:
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert sock.fileno() == -1
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        sock.close()


def test_observer_drops_invalid_reference_packet_before_interpreting_rtp() -> None:
    observer = PassiveRunner(MediaSnooper(rtp_header_body))
    observer.on_datagram(b"\x80\x60" + bytes(10), time.monotonic())
    assert observer.dropped == 1
    assert observer.packets == 0
    assert observer.snapshot()["observer"] == {"packets": 0, "malformed": 1}


def test_multicast_configuration_validates_before_socket_creation() -> None:
    for group in ("127.0.0.1", "not-a-group", "ff02::1"):
        with pytest.raises(ValueError):
            MulticastOptions(group)
    with pytest.raises(ValueError):
        MulticastOptions(GROUP, interface="239.1.2.3")
    with pytest.raises(ValueError):
        MulticastOptions(GROUP, ttl=256)


async def test_multicast_listener_does_not_receive_other_local_group() -> None:
    sockets: list[socket.socket] = []
    try:
        port = _unused_port()
        selected = open_receiver_socket(port, multicast=OPTIONS)
        sockets.append(selected)
        other = open_receiver_socket(
            port, multicast=MulticastOptions("239.255.42.78", "127.0.0.1", ttl=0)
        )
        sockets.append(other)
        sender = open_sender_socket(multicast=OPTIONS)
        sockets.append(sender)
        loop = asyncio.get_running_loop()
        await loop.sock_sendto(sender, b"selected group only", (GROUP, port))
        raw, _addr = await asyncio.wait_for(loop.sock_recvfrom(selected, 1024), 1)
        assert raw == b"selected group only"
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(loop.sock_recvfrom(other, 1024), 0.05)
    except OSError as exc:
        require_socket_support(exc)
    finally:
        for sock in sockets:
            sock.close()


@pytest.mark.parametrize("sender", [False, True])
def test_socket_setup_failure_closes_allocated_socket(
    sender: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingSocket:
        closed = False

        def bind(self, addr: tuple[str, int]) -> None:
            pass

        def setsockopt(self, *args: object) -> None:
            raise OSError("injected setup failure")

        def close(self) -> None:
            self.closed = True

    sock = FailingSocket()
    monkeypatch.setattr("yoke.media.sockets.socket.socket", lambda *args: sock)
    with pytest.raises(OSError, match="injected setup failure"):
        if sender:
            open_sender_socket(multicast=OPTIONS)
        else:
            open_receiver_socket(5004, multicast=OPTIONS)
    assert sock.closed
