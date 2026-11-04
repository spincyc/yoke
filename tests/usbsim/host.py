"""A simulated USB host (the user's computer) on the simulated UDC.

Enumeration profiles approximate the request sequences commonly observed
from Linux, Windows and macOS hosts (first device-descriptor read of 64 or
8 bytes, second reset, SET_ADDRESS, configuration read as header then full or
as 255 bytes, string reads with a language ID and with short then exact
lengths, the Microsoft OS string probe, DEVICE_QUALIFIER for USB 2.0 devices
at full speed). They are models, not captured traces.

Every control transfer is timed against USB 2.0 §9.2.6.4 (50 ms without a
data stage, 500 ms for a data stage). Those are device requirements; real
host stacks wait about 5 s, so a miss is recorded in ``deadline_misses`` (an
advisory metric) and only the 5 s host timeout fails the transfer. A reply
longer than wLength, or a short reply that ends on a packet boundary without
a zero-length packet, is a hard violation, as it would be on a real host.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from tests.usbsim.udc import FakeRawGadgetKernel
from yoke.usb import ch9
from yoke.usb.ch9 import Setup, Speed

CLASS_TIMEOUT = 5.0
HOST_TIMEOUT = 5.0  # Linux USB_CTRL_GET_TIMEOUT; Windows and macOS are similar


class StallError(Exception):
    """The device answered with STALL."""


class HostTimeoutError(Exception):
    """The device missed the host's deadline."""


class HostProtocolError(Exception):
    """The device broke a USB rule the host enforces."""


@dataclass
class Enumeration:
    profile: str
    device: bytes = b""
    configurations: list[bytes] = field(default_factory=list)
    strings: dict[tuple[int, int], bytes | None] = field(default_factory=dict)
    qualifier: bytes | None = None
    bos: bytes | None = None
    speed: Speed = Speed.UNKNOWN


@dataclass
class ControlRecord:
    setup: Setup
    outcome: str
    elapsed: float
    deadline: float


class UsbHost:
    def __init__(
        self,
        kernel: FakeRawGadgetKernel,
        *,
        max_speed: Speed = Speed.HIGH,
        ep0_size: int = 64,
    ) -> None:
        self.kernel = kernel
        self.max_speed = max_speed
        self.ep0_size = ep0_size
        self.speed = Speed.UNKNOWN
        self.violations: list[str] = []
        self.deadline_misses: list[str] = []
        self.records: list[ControlRecord] = []
        self.address = 0

    # --- bus -----------------------------------------------------------------

    def attach(self, timeout: float = 5.0) -> Speed:
        self.speed = self.kernel.host_attach(self.max_speed, timeout)
        return self.speed

    def reset(self) -> None:
        self.kernel.host_reset()

    def suspend(self) -> None:
        self.kernel.host_suspend()

    def resume(self) -> None:
        self.kernel.host_resume()

    def disconnect(self) -> None:
        self.kernel.host_disconnect()

    # --- control -------------------------------------------------------------

    @staticmethod
    def deadline(setup: Setup) -> float:
        if not setup.is_standard:
            return CLASS_TIMEOUT
        if setup.length:
            return ch9.DATA_STAGE_DEADLINE
        return ch9.NO_DATA_DEADLINE

    def control(self, setup: Setup, data: bytes = b"") -> bytes:
        """Run a control transfer; raise StallError/HostTimeoutError."""
        if not setup.is_in and len(data) != setup.length:
            raise ValueError("OUT data must match wLength")
        deadline = self.deadline(setup)
        result = self.kernel.host_control(setup, data, HOST_TIMEOUT)
        self.records.append(
            ControlRecord(setup, result.outcome, result.elapsed, deadline)
        )
        if result.elapsed > deadline:
            self.deadline_misses.append(
                f"{setup} took {result.elapsed * 1000:.1f} ms "
                f"(§9.2.6.4 limit {deadline * 1000:.0f} ms)"
            )
        if result.outcome == "timeout":
            self.violations.append(f"{setup}: no answer within {HOST_TIMEOUT} s")
            raise HostTimeoutError(str(setup))
        if result.outcome == "stall":
            raise StallError(str(setup))
        if result.outcome == "data":
            reply = result.data
            if len(reply) > setup.length:
                self.violations.append(f"{setup}: {len(reply)} bytes > wLength")
                raise HostProtocolError("babble on endpoint zero")
            if (
                len(reply) < setup.length
                and len(reply) % self.ep0_size == 0
                and not result.zero
            ):
                self.violations.append(f"{setup}: short reply without ZLP")
                raise HostTimeoutError("missing zero-length packet")
            return reply
        return b""

    def get_descriptor(
        self, dtype: int, index: int = 0, length: int = 0xFF, *, lang: int = 0
    ) -> bytes | None:
        try:
            return self.control(ch9.get_descriptor(dtype, index, length, lang=lang))
        except StallError:
            return None

    def set_address(self, address: int) -> None:
        self.control(Setup(0x00, ch9.SET_ADDRESS, address))
        self.address = address

    def set_configuration(self, value: int) -> None:
        self.control(Setup(0x00, ch9.SET_CONFIGURATION, value))

    def set_interface(self, interface: int, alternate: int) -> None:
        self.control(Setup(0x01, ch9.SET_INTERFACE, alternate, interface))

    def clear_halt(self, address: int) -> None:
        self.control(
            Setup(
                ch9.RECIP_ENDPOINT,
                ch9.CLEAR_FEATURE,
                ch9.FEATURE_ENDPOINT_HALT,
                address,
            )
        )

    def get_status(self, recipient: int = ch9.RECIP_DEVICE, index: int = 0) -> bytes:
        return self.control(Setup(0x80 | recipient, ch9.GET_STATUS, 0, index, 2))

    # --- data ---------------------------------------------------------------

    def transfer_in(self, address: int, length: int, timeout: float = 5.0) -> bytes:
        outcome, data = self.kernel.host_in(address, length, timeout)
        if outcome == "stall":
            raise StallError(f"IN {address:#04x}")
        if outcome == "timeout":
            raise HostTimeoutError(f"IN {address:#04x}")
        if outcome != "ok":
            raise HostProtocolError(f"IN {address:#04x}: {outcome}")
        return data

    def transfer_out(
        self, address: int, data: bytes, *, zlp: bool = False, timeout: float = 5.0
    ) -> None:
        outcome = self.kernel.host_out(address, data, timeout, zlp=zlp)
        if outcome == "stall":
            raise StallError(f"OUT {address:#04x}")
        if outcome == "timeout":
            raise HostTimeoutError(f"OUT {address:#04x}")
        if outcome != "ok":
            raise HostProtocolError(f"OUT {address:#04x}: {outcome}")

    # --- enumeration --------------------------------------------------------

    def _read_config(self, index: int, first: int) -> bytes:
        head = self.get_descriptor(ch9.DT_CONFIG, index, first)
        if head is None or len(head) < 4:
            raise HostProtocolError("configuration descriptor unavailable")
        total = struct.unpack_from("<H", head, 2)[0]
        if total <= len(head):
            return head[:total]
        full = self.get_descriptor(ch9.DT_CONFIG, index, total)
        if full is None or len(full) != total:
            raise HostProtocolError("configuration descriptor truncated")
        return full

    def _string_indices(self, device: bytes, configs: list[bytes]) -> list[int]:
        indices = {device[14], device[15], device[16]}
        for config in configs:
            indices.add(config[6])
            offset = config[0]
            while offset + 1 < len(config):
                length = config[offset]
                if length < 2:
                    break
                if config[offset + 1] == ch9.DT_INTERFACE and length >= 9:
                    indices.add(config[offset + 8])
                offset += length
        indices.discard(0)
        return sorted(indices)

    def enumerate(
        self, profile: str = "linux", *, configure: bool = True
    ) -> Enumeration:
        """Reset, address and read descriptors the way ``profile`` does."""
        if self.speed == Speed.UNKNOWN:
            self.attach()
        result = Enumeration(profile, speed=self.speed)
        first = {"linux": 64, "windows": 64, "macos": 8}[profile]
        self.reset()
        initial = self.get_descriptor(ch9.DT_DEVICE, 0, first)
        if initial is None or len(initial) < 8:
            raise HostProtocolError("initial device descriptor read failed")
        self.ep0_size = initial[7] if initial[7] in (8, 16, 32, 64) else 64
        self.reset()
        self.set_address(7)
        device = self.get_descriptor(ch9.DT_DEVICE, 0, 18)
        if device is None or len(device) != 18:
            raise HostProtocolError("device descriptor read failed")
        result.device = device
        bcd_usb = struct.unpack_from("<H", device, 2)[0]
        configs: list[bytes] = []
        if profile == "windows":
            configs.append(self._read_config(0, 255))
            result.strings[(0xEE, 0)] = self.get_descriptor(ch9.DT_STRING, 0xEE, 0x12)
        if profile == "linux" and bcd_usb >= 0x0201:
            head = self.get_descriptor(ch9.DT_BOS, 0, 5)
            if head is not None:
                total = struct.unpack_from("<H", head, 2)[0]
                result.bos = self.get_descriptor(ch9.DT_BOS, 0, total)
        if self.speed == Speed.FULL and bcd_usb >= 0x0200:
            result.qualifier = self.get_descriptor(ch9.DT_DEVICE_QUALIFIER, 0, 10)
        for index in range(device[17]):
            configs_read = self._read_config(index, 9)
            if profile == "windows" and index == 0:
                if configs_read != configs[0]:
                    self.violations.append("configuration changed between reads")
                continue
            configs.append(configs_read)
        result.configurations = configs
        indices = self._string_indices(device, configs)
        if indices:
            if profile == "macos":
                head = self.get_descriptor(ch9.DT_STRING, 0, 2)
                langs = self.get_descriptor(ch9.DT_STRING, 0, head[0]) if head else None
            else:
                langs = self.get_descriptor(ch9.DT_STRING, 0, 255)
            result.strings[(0, 0)] = langs
            lang = (
                struct.unpack_from("<H", langs, 2)[0]
                if langs and len(langs) >= 4
                else 0
            )
            for index in indices:
                if profile == "macos":
                    head = self.get_descriptor(ch9.DT_STRING, index, 2, lang=lang)
                    text = (
                        self.get_descriptor(ch9.DT_STRING, index, head[0], lang=lang)
                        if head
                        else None
                    )
                else:
                    text = self.get_descriptor(ch9.DT_STRING, index, 255, lang=lang)
                result.strings[(index, lang)] = text
        if profile != "linux":
            status = self.get_status()
            if status[0] & 1:
                self.violations.append("presented device claims self power")
        if configure:
            config_value = configs[0][5]
            self.set_configuration(config_value)
        return result
