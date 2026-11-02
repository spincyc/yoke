"""Offline tests for the Just Add Power decoder driver.

Every device interaction is a scripted transcript on a fake link, so the
assumptions about the JAP CLI live here as executable statements to be
corrected after bench verification.
"""

from __future__ import annotations

import socket

import pytest

from yoke.core.types import NodeState
from yoke.drivers.base import DriverError
from yoke.drivers.jap import (
    JapDecoderDriver,
    JapEncoderConfig,
    JapLink,
    TelnetLink,
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
    """Give transcript tests a decoder whose initial teardown is confirmed.

    Fresh process/device recovery is covered separately below. Keeping its
    setup transcript separate lets each case script just the operation it tests.
    """
    preparing = True

    def factory(host: str) -> JapLink:
        if preparing:
            return FakeLink({"channel 0": "ok", "channel": "channel: 0"}, [])
        return rig.factory(host)

    driver = JapDecoderDriver("10.0.0.20", factory)
    driver.detach()
    preparing = False
    rig.script.setdefault("channel", "channel: 0")
    return driver


class TestJapGroup:
    def test_channel_maps_into_last_two_octets(self) -> None:
        assert jap_group(1) == "239.92.0.1"
        assert jap_group(100) == "239.92.0.100"
        assert jap_group(256) == "239.92.1.0"
        assert jap_group(300) == "239.92.1.44"

    @pytest.mark.parametrize("channel", [0, -1, 65536, True, 1.5, "100"])
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
        assert driver.poll().state is NodeState.ERROR

    def test_connect_failure_raises(self, rig: LinkRig) -> None:
        rig.connect_error = DriverError("jap connect 10.0.0.20:23: refused")
        driver = make_driver(rig)
        with pytest.raises(DriverError, match="connect"):
            driver.attach("enc-den", {"channel": 100})

    def test_lost_set_reply_requires_cleanup_before_retry(self, rig: LinkRig) -> None:
        rig.script["channel 100"] = DriverError("reply lost")
        driver = make_driver(rig)
        with pytest.raises(DriverError, match="reply lost"):
            driver.attach("enc-den", {"channel": 100})
        assert driver.poll().state is NodeState.ERROR
        rig.script["channel 100"] = "ok"
        rig.script["channel 0"] = "ok"
        rig.script["channel"] = "current channel: 0"
        driver.attach("enc-den", {"channel": 100})
        assert rig.log == ["channel 100", "channel 0", "channel", "channel 100"]

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
        rig.script["channel"] = "current channel: 0"
        driver = make_driver(rig)
        driver.attach("enc-den", {"channel": 100})
        driver.detach()
        assert rig.log == ["channel 100", "channel 0", "channel"]
        assert driver.poll().state is NodeState.IDLE
        driver.detach()
        assert rig.log == ["channel 100", "channel 0", "channel", "channel"]

    def test_detach_is_idempotent_and_quiet(self, rig: LinkRig) -> None:
        driver = make_driver(rig)
        driver.detach()
        assert rig.log == []

    def test_failed_detach_is_visible_and_retries_the_owned_route(
        self, rig: LinkRig
    ) -> None:
        rig.script["channel 100"] = "ok"
        rig.script["channel 0"] = DriverError("link died")
        driver = make_driver(rig)
        driver.attach("enc-den", {"channel": 100})
        with pytest.raises(DriverError, match="link died"):
            driver.detach()
        assert driver.poll().state is NodeState.ERROR
        rig.script["channel 0"] = "ok"
        rig.script["channel"] = "current channel: 0"
        driver.detach()
        assert driver.poll().state is NodeState.IDLE
        assert rig.log == [
            "channel 100",
            "channel 0",
            "channel 0",
            "channel",
            "channel",
        ]

    @pytest.mark.parametrize("reply", ["ERROR: no channel zero", "invalid channel"])
    def test_refused_detach_does_not_claim_idle(self, rig: LinkRig, reply: str) -> None:
        rig.script["channel 100"] = "ok"
        rig.script["channel 0"] = reply
        driver = make_driver(rig)
        driver.attach("enc-den", {"channel": 100})
        with pytest.raises(DriverError, match="refused"):
            driver.detach()
        assert driver.poll().state is NodeState.ERROR

    def test_ack_without_channel_zero_confirmation_blocks_replacement(
        self, rig: LinkRig
    ) -> None:
        rig.script["channel 100"] = "ok"
        rig.script["channel 0"] = "ok"
        rig.script["channel"] = "current channel: 100"
        driver = make_driver(rig)
        driver.attach("enc-den", {"channel": 100})
        with pytest.raises(DriverError, match="teardown unconfirmed"):
            driver.attach("enc-office", {"channel": 200})
        assert "channel 200" not in rig.log
        assert driver.poll().state is NodeState.ERROR


class TestPoll:
    def test_idle_before_attach(self, rig: LinkRig) -> None:
        assert make_driver(rig).poll().state is NodeState.IDLE

    def test_active_when_channel_matches(self, rig: LinkRig) -> None:
        rig.script["channel 100"] = "ok"
        rig.script["channel"] = "current channel: 100"
        driver = make_driver(rig)
        driver.attach("enc-den", {"channel": 100})
        assert driver.poll().state is NodeState.ACTIVE

    def test_retryable_error_when_channel_differs(self, rig: LinkRig) -> None:
        rig.script["channel 100"] = "ok"
        rig.script["channel"] = "current channel: 42"
        driver = make_driver(rig)
        driver.attach("enc-den", {"channel": 100})
        status = driver.poll()
        assert status.state is NodeState.ERROR
        assert "42" in status.detail
        assert "retry required" in status.detail

    def test_core_retry_repairs_acknowledged_but_unapplied_channel(
        self, rig: LinkRig
    ) -> None:
        from yoke.core.frames import decode, encode
        from yoke.core.node import Node
        from yoke.core.types import (
            AttachMedia,
            FrameType,
            Kind,
            NodeConfig,
            Pairing,
            Persist,
            PersistedState,
            Role,
        )

        node = Node(
            NodeConfig("decoder", Role.CONSUMER, Kind.HDMI),
            state=PersistedState(pairing=Pairing("enc-den", (1, "cli"))),
            now=0.0,
        )
        save = next(effect for effect in node.start(0.0) if isinstance(effect, Persist))
        node.on_persist_result(save.request_id, True, 0.0)
        raw = encode(
            FrameType.ANN,
            "enc-den",
            1,
            0.0,
            {
                "role": "producer",
                "kind": "hdmi",
                "name": "Encoder",
                "state": "active",
                "err": None,
                "pairing": None,
                "media": {"channel": 100},
            },
            b"",
        )
        effects = node.on_frame(decode(raw, b""), raw, 0.1)
        attach = next(effect for effect in effects if isinstance(effect, AttachMedia))
        rig.script.update({"channel 100": "ok", "channel": "current channel: 0"})
        driver = make_driver(rig)
        driver.attach(attach.producer, attach.media)
        node.on_driver_status(driver.poll(), 0.1)
        assert node.node_state is NodeState.ERROR
        assert not any(isinstance(effect, AttachMedia) for effect in node.poll(1.09))
        effects = node.poll(1.1)
        retry = next(effect for effect in effects if isinstance(effect, AttachMedia))
        rig.script["channel 0"] = "ok"
        driver.attach(retry.producer, retry.media)
        rig.script["channel"] = "current channel: 100"
        node.on_driver_status(driver.poll(), 1.1)
        assert node.node_state is NodeState.ACTIVE
        assert rig.log == [
            "channel 100",
            "channel",
            "channel 0",
            "channel",
            "channel 100",
            "channel",
        ]

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

    def test_unrelated_digits_do_not_become_the_channel(self, rig: LinkRig) -> None:
        rig.script["channel 100"] = "ok"
        rig.script["channel"] = "firmware 2\ncurrent channel: 100\nuptime 123"
        driver = make_driver(rig)
        driver.attach("enc-den", {"channel": 100})
        status = driver.poll()
        assert status.state is NodeState.ACTIVE
        assert "channel 100; media flow unverified" in status.detail

    @pytest.mark.parametrize(
        "reply",
        ["firmware 100", "channel: 10\nchannel: 0", "channel: 100 version 2", "99999"],
    )
    def test_ambiguous_or_unrecognized_reply_is_error(
        self, rig: LinkRig, reply: str
    ) -> None:
        rig.script["channel 100"] = "ok"
        rig.script["channel"] = reply
        driver = make_driver(rig)
        driver.attach("enc-den", {"channel": 100})
        assert driver.poll().state is NodeState.ERROR

    @pytest.mark.parametrize(
        "bad_line", ["channel: 99999", "channel: 100000", "channel: -1", "channel: ?"]
    )
    def test_invalid_channel_line_cannot_be_ignored_beside_a_valid_line(
        self, rig: LinkRig, bad_line: str
    ) -> None:
        rig.script["channel 100"] = "ok"
        rig.script["channel"] = f"current channel: 100\n{bad_line}"
        driver = make_driver(rig)
        driver.attach("enc-den", {"channel": 100})
        assert driver.poll().state is NodeState.ERROR


class TestEncoderConfig:
    def test_media_advertises_channel_and_group(self) -> None:
        media = JapEncoderConfig(300).media()
        assert media == {"channel": 300, "group": "239.92.1.44"}

    def test_rejects_bad_channel(self) -> None:
        with pytest.raises(DriverError):
            JapEncoderConfig(0)


class TestFreshDecoder:
    def test_route_is_unknown_until_confirmed_detachment(self, rig: LinkRig) -> None:
        driver = JapDecoderDriver("10.0.0.20", rig.factory)
        assert driver.poll().state is NodeState.PENDING
        rig.script.update({"channel 0": "ok", "channel": "channel: 0"})
        driver.detach()
        assert driver.poll().state is NodeState.IDLE
        assert rig.log == ["channel 0", "channel", "channel"]
        driver.detach()
        assert rig.log == ["channel 0", "channel", "channel"]

    @pytest.mark.parametrize("reply", ["ERROR: channel zero unsupported", "ok"])
    def test_unknown_route_survives_failed_teardown_and_blocks_attach(
        self, rig: LinkRig, reply: str
    ) -> None:
        rig.script.update({"channel 0": reply, "channel": "channel: 100"})
        driver = JapDecoderDriver("10.0.0.20", rig.factory)
        with pytest.raises(DriverError):
            driver.detach()
        assert driver.poll().state is NodeState.ERROR
        with pytest.raises(DriverError):
            driver.attach("enc-den", {"channel": 200})
        assert rig.log.count("channel 0") == 2
        assert "channel 200" not in rig.log
        rig.script.update(
            {"channel 0": "ok", "channel": "channel: 0", "channel 200": "ok"}
        )
        driver.attach("enc-den", {"channel": 200})
        assert rig.log[-3:] == ["channel 0", "channel", "channel 200"]

    @pytest.mark.parametrize("readback", [0, 100])
    def test_restored_unpair_drives_fresh_decoder_through_startup_barrier(
        self, monkeypatch: pytest.MonkeyPatch, readback: int
    ) -> None:
        from yoke.core.node import Node
        from yoke.core.types import (
            DetachMedia,
            Kind,
            NodeConfig,
            Pairing,
            Persist,
            PersistedState,
            Role,
        )

        # A durable unpair can precede the old process's appliance teardown.
        # Neither its process identity nor its old in-memory channel is needed.
        saved = Pairing(None, (7, "cli"))
        node = Node(
            NodeConfig("decoder", Role.CONSUMER, Kind.HDMI),
            state=PersistedState(pairing=saved),
            now=0.0,
        )
        startup = node.start(0.0)
        assert not any(isinstance(effect, DetachMedia) for effect in startup)
        save = next(effect for effect in startup if isinstance(effect, Persist))
        released = node.on_persist_result(save.request_id, True, 0.0)
        assert any(isinstance(effect, DetachMedia) for effect in released)
        sockets = [
            TelnetSocket([b"ok\r\n"]),
            TelnetSocket([f"channel: {readback}\r\n".encode()]),
        ]
        pending = list(sockets)
        monkeypatch.setattr(
            socket, "create_connection", lambda *a, **kw: pending.pop(0)
        )
        driver = JapDecoderDriver("10.0.0.20", lambda host: TelnetLink(host))
        assert driver.poll().state is NodeState.PENDING
        if readback:
            with pytest.raises(DriverError, match="teardown unconfirmed"):
                driver.detach()
            assert driver.poll().state is NodeState.ERROR
            node.on_driver_status(driver.poll(), 0.0)
            assert node.node_state is NodeState.ERROR
            retry = node.poll(1.0)
            assert any(isinstance(effect, DetachMedia) for effect in retry)
            recovery = [TelnetSocket([b"ok\r\n"]), TelnetSocket([b"channel: 0\r\n"])]
            sockets.extend(recovery)
            pending.extend(recovery)
        driver.detach()
        health = TelnetSocket([b"channel: 0\r\n"])
        sockets.append(health)
        pending.append(health)
        status = driver.poll()
        node.on_driver_status(status, 1.0 if readback else 0.0)
        assert status.state is NodeState.IDLE
        assert node.node_state is NodeState.IDLE
        assert node.pairing == saved
        assert all(sock.closed for sock in sockets)
        assert not pending
        assert [sock.sent for sock in sockets] == (
            [[b"channel 0\r\n"], [b"channel\r\n"]] * (2 if readback else 1)
        ) + [[b"channel\r\n"]]
        driver.detach()  # confirmed detachment requires no further connection

    def test_unpaired_warden_repairs_decoder_reboot_drift(self, rig: LinkRig) -> None:
        driver = make_driver(rig)
        assert driver.poll().state is NodeState.IDLE
        rig.script["channel"] = "channel: 100"  # appliance reboot default
        status = driver.poll()
        assert status.state is NodeState.ERROR
        assert "want 0" in status.detail
        rig.script.update({"channel 0": "ok", "channel": "channel: 0"})
        driver.detach()
        assert driver.poll().state is NodeState.IDLE
        assert rig.log == ["channel", "channel", "channel 0", "channel", "channel"]


class TelnetSocket:
    def __init__(self, chunks: list[bytes | OSError]) -> None:
        self.chunks = list(chunks)
        self.sent: list[bytes] = []
        self.timeouts: list[float] = []
        self.closed = False

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def settimeout(self, timeout: float) -> None:
        self.timeouts.append(timeout)

    def recv(self, size: int) -> bytes:
        assert self.chunks, "unexpected receive"
        chunk = self.chunks.pop(0)
        if isinstance(chunk, OSError):
            raise chunk
        if len(chunk) > size:
            self.chunks.insert(0, chunk[size:])
        return chunk[:size]

    def close(self) -> None:
        self.closed = True


def telnet_link(
    monkeypatch: pytest.MonkeyPatch, sock: TelnetSocket, **kw: float
) -> TelnetLink:
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: sock)
    return TelnetLink("10.0.0.20", **kw)


class TestTelnetTranscript:
    @pytest.mark.parametrize(
        "chunks",
        [
            [b"channel: 0\r\nchannel: 100\r\n"],
            [b"chan", b"nel: 0\r\nchannel: 100\r\n"],
            [b"channel: 0\r\nchan", b"nel: 100\r\n"],
            [b"channel: 0\r\n   ", b"channel: 100\r\n"],
        ],
    )
    def test_every_already_received_line_reaches_channel_validation(
        self, monkeypatch: pytest.MonkeyPatch, chunks: list[bytes]
    ) -> None:
        sock = TelnetSocket(chunks)
        link = telnet_link(monkeypatch, sock)
        assert link.send_command("channel") == "channel: 0\nchannel: 100"

    @pytest.mark.parametrize(
        "chunks",
        [
            [b"channel: 0\r\nchannel: 100\r\n"],
            [b"channel: 0\r\nchannel: 99999\r\n"],
            [b"channel: 0\r\nchannel: -1\r\n"],
            [b"channel: 0\r\nchan", b"nel: 100\r\n"],
            [b"channel: 0\r\n   ", b"channel: 100\r\n"],
        ],
    )
    def test_actual_telnet_driver_seam_rejects_ambiguous_teardown(
        self, monkeypatch: pytest.MonkeyPatch, chunks: list[bytes]
    ) -> None:
        # Each command uses a new real TelnetLink; only its socket is fake.
        sockets = [
            TelnetSocket([b"ok\r\n"]),
            TelnetSocket([b"channel: 0\r\n"]),
            TelnetSocket([b"ok\r\n"]),
            TelnetSocket([b"ok\r\n"]),
            TelnetSocket(chunks),
        ]
        pending = iter(sockets)
        monkeypatch.setattr(socket, "create_connection", lambda *a, **kw: next(pending))
        driver = JapDecoderDriver("10.0.0.20", lambda host: TelnetLink(host))
        driver.attach("enc-den", {"channel": 100})
        with pytest.raises(DriverError, match=r"unparseable|invalid channel reply"):
            driver.detach()
        assert driver.poll().state is NodeState.ERROR
        assert all(sock.closed for sock in sockets)
        assert [sock.sent for sock in sockets] == [
            [b"channel 0\r\n"],
            [b"channel\r\n"],
            [b"channel 100\r\n"],
            [b"channel 0\r\n"],
            [b"channel\r\n"],
        ]

    @pytest.mark.parametrize("tail", [b"chan", b"   "])
    def test_complete_first_line_does_not_hide_known_incomplete_tail(
        self, monkeypatch: pytest.MonkeyPatch, tail: bytes
    ) -> None:
        sock = TelnetSocket([b"channel: 0\r\n" + tail, b""])
        link = telnet_link(monkeypatch, sock)
        with pytest.raises(DriverError, match="closed before complete reply"):
            link.send_command("channel")

    def test_complete_blank_lines_do_not_require_more_input(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sock = TelnetSocket([b"channel: 0\r\n   \r\n\r\n"])
        link = telnet_link(monkeypatch, sock)
        assert link.send_command("channel") == "channel: 0"

    def test_fragmented_options_subnegotiation_echo_and_reply(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # RFC 854 WILL/DO are refused; WONT/DONT do not start reply loops.
        sock = TelnetSocket(
            [
                b"\xff",
                b"\xfb",
                b"\x01\xff\xfd",
                b"\x03\xff\xfc\x01\xff\xfe\x03\xff\xfa\x18abc\xff",
                b"\xf0chan",
                b"nel\r\ncurrent chan",
                b"nel: 100\r\n",
            ]
        )
        link = telnet_link(monkeypatch, sock)
        assert link.send_command("channel") == "current channel: 100"
        assert sock.sent == [b"channel\r\n", b"\xff\xfe\x01", b"\xff\xfc\x03"]
        link.close()
        assert sock.closed

    @pytest.mark.parametrize("tail", [b"", TimeoutError("timed out")])
    def test_partial_reply_is_never_success(
        self, monkeypatch: pytest.MonkeyPatch, tail: bytes | OSError
    ) -> None:
        sock = TelnetSocket([b"current channel: 1", tail])
        link = telnet_link(monkeypatch, sock)
        with pytest.raises(DriverError, match=r"closed|timed out"):
            link.send_command("channel")

    def test_reply_size_is_bounded_including_telnet_controls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sock = TelnetSocket([b"\xff\xfa" + b"x" * 4095])
        link = telnet_link(monkeypatch, sock)
        with pytest.raises(DriverError, match="exceeds 4096"):
            link.send_command("channel")

    def test_total_deadline_bounds_a_drip_fed_reply(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sock = TelnetSocket([b"c"])
        link = telnet_link(monkeypatch, sock, timeout=1.0)
        times = iter([0.0, 0.4, 1.1])
        monkeypatch.setattr("yoke.drivers.jap.time.monotonic", lambda: next(times))
        with pytest.raises(DriverError, match="deadline exceeded"):
            link.send_command("channel")
        assert sock.timeouts == [1.0, 0.6]

    def test_option_responses_share_the_command_deadline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sock = TelnetSocket([b"\xff\xfb\x01\xff\xfd\x03"])
        link = telnet_link(monkeypatch, sock, timeout=1.0)
        times = iter([0.0, 0.1, 0.2, 1.1])
        monkeypatch.setattr("yoke.drivers.jap.time.monotonic", lambda: next(times))
        with pytest.raises(DriverError, match="deadline exceeded"):
            link.send_command("channel")
        assert sock.sent == [b"channel\r\n", b"\xff\xfe\x01"]

    @pytest.mark.parametrize("command", ["channel\r\nreboot", "", "chännél"])
    def test_invalid_command_never_sent(
        self, monkeypatch: pytest.MonkeyPatch, command: str
    ) -> None:
        sock = TelnetSocket([])
        link = telnet_link(monkeypatch, sock)
        with pytest.raises(DriverError):
            link.send_command(command)
        assert sock.sent == []

    @pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf")])
    def test_invalid_timeout_does_not_connect(
        self, monkeypatch: pytest.MonkeyPatch, timeout: float
    ) -> None:
        def forbidden(*args: object, **kwargs: object) -> None:
            raise AssertionError("must validate before connecting")

        monkeypatch.setattr(socket, "create_connection", forbidden)
        with pytest.raises(DriverError, match="finite and positive"):
            TelnetLink("10.0.0.20", timeout=timeout)
