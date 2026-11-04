"""USB 2.0 chapter 9 vocabulary shared by the proxy and its simulation.

Values follow the USB 2.0 specification and the Linux ``usb_device_speed``
enumeration, which both USB/IP (``speed``) and Raw Gadget (``usb_raw_init``)
use on the wire. Pure data and arithmetic only.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

SETUP_SIZE = 8
_SETUP = struct.Struct("<BBHHH")


class Speed(IntEnum):
    """Linux ``enum usb_device_speed``."""

    UNKNOWN = 0
    LOW = 1
    FULL = 2
    HIGH = 3
    WIRELESS = 4
    SUPER = 5
    SUPER_PLUS = 6


SPEED_NAMES = {
    Speed.UNKNOWN: "UNKNOWN",
    Speed.LOW: "low-speed",
    Speed.FULL: "full-speed",
    Speed.HIGH: "high-speed",
    Speed.WIRELESS: "wireless",
    Speed.SUPER: "super-speed",
    Speed.SUPER_PLUS: "super-speed-plus",
}
"""Linux ``usb_speed_string()`` spellings, as in UDC sysfs ``current_speed``."""


def speed_from_name(name: str) -> Speed:
    for speed, text in SPEED_NAMES.items():
        if text == name.strip():
            return speed
    return Speed.UNKNOWN


# bmRequestType fields
DIR_IN = 0x80
TYPE_MASK = 0x60
TYPE_STANDARD = 0x00
TYPE_CLASS = 0x20
TYPE_VENDOR = 0x40
RECIP_MASK = 0x1F
RECIP_DEVICE = 0x00
RECIP_INTERFACE = 0x01
RECIP_ENDPOINT = 0x02
RECIP_OTHER = 0x03

# Standard requests
GET_STATUS = 0
CLEAR_FEATURE = 1
SET_FEATURE = 3
SET_ADDRESS = 5
GET_DESCRIPTOR = 6
SET_DESCRIPTOR = 7
GET_CONFIGURATION = 8
SET_CONFIGURATION = 9
GET_INTERFACE = 10
SET_INTERFACE = 11
SYNCH_FRAME = 12

# Feature selectors
FEATURE_ENDPOINT_HALT = 0
FEATURE_DEVICE_REMOTE_WAKEUP = 1
FEATURE_TEST_MODE = 2
PORT_FEAT_RESET = 4
RT_PORT = 0x23  # class, other recipient, host-to-device (hub port request)

# Descriptor types
DT_DEVICE = 1
DT_CONFIG = 2
DT_STRING = 3
DT_INTERFACE = 4
DT_ENDPOINT = 5
DT_DEVICE_QUALIFIER = 6
DT_OTHER_SPEED_CONFIG = 7
DT_INTERFACE_POWER = 8
DT_OTG = 9
DT_DEBUG = 10
DT_INTERFACE_ASSOCIATION = 11
DT_BOS = 15
DT_DEVICE_CAPABILITY = 16
DT_HID = 0x21
DT_HID_REPORT = 0x22
DT_SS_ENDPOINT_COMP = 0x30

# Endpoint transfer types (bmAttributes & 3)
XFER_CONTROL = 0
XFER_ISOC = 1
XFER_BULK = 2
XFER_INT = 3

CLASS_PER_INTERFACE = 0x00
CLASS_HID = 0x03
CLASS_MASS_STORAGE = 0x08
CLASS_HUB = 0x09
CLASS_CDC_DATA = 0x0A
CLASS_MISC = 0xEF
CLASS_VENDOR = 0xFF

# §9.2.6.4 device response limits, enforced by the simulated host.
NO_DATA_DEADLINE = 0.050
DATA_STAGE_DEADLINE = 0.500
STATUS_STAGE_DEADLINE = 0.050


@dataclass(frozen=True, slots=True)
class Setup:
    """An 8-byte SETUP packet (``struct usb_ctrlrequest``, little endian)."""

    request_type: int
    request: int
    value: int = 0
    index: int = 0
    length: int = 0

    def __post_init__(self) -> None:
        for name, bound in (
            ("request_type", 0xFF),
            ("request", 0xFF),
            ("value", 0xFFFF),
            ("index", 0xFFFF),
            ("length", 0xFFFF),
        ):
            field = getattr(self, name)
            if type(field) is not int or not 0 <= field <= bound:
                raise ValueError(f"setup {name} out of range: {field!r}")

    @classmethod
    def unpack(cls, raw: bytes | bytearray | memoryview) -> Setup:
        if len(raw) != SETUP_SIZE:
            raise ValueError(f"setup packet must be 8 bytes, got {len(raw)}")
        return cls(*_SETUP.unpack(raw))

    def pack(self) -> bytes:
        return _SETUP.pack(
            self.request_type, self.request, self.value, self.index, self.length
        )

    @property
    def is_in(self) -> bool:
        return bool(self.request_type & DIR_IN)

    @property
    def has_in_data(self) -> bool:
        """Raw Gadget answers these with EP0_WRITE; everything else EP0_READ."""
        return self.is_in and self.length > 0

    @property
    def kind(self) -> int:
        return self.request_type & TYPE_MASK

    @property
    def recipient(self) -> int:
        return self.request_type & RECIP_MASK

    @property
    def is_standard(self) -> bool:
        return self.kind == TYPE_STANDARD

    @property
    def descriptor_type(self) -> int:
        return self.value >> 8

    @property
    def descriptor_index(self) -> int:
        return self.value & 0xFF


def get_descriptor(
    dtype: int, index: int = 0, length: int = 0xFF, *, lang: int = 0, recip: int = 0
) -> Setup:
    """Build a standard GET_DESCRIPTOR request."""
    return Setup(DIR_IN | recip, GET_DESCRIPTOR, (dtype << 8) | index, lang, length)


def endpoint_packet_size(max_packet: int) -> int:
    """wMaxPacketSize bits 10..0."""
    return max_packet & 0x7FF


def endpoint_transfer_size(max_packet: int) -> int:
    """Bytes per (micro)frame: size times (1 + high-bandwidth mult)."""
    return endpoint_packet_size(max_packet) * (1 + ((max_packet >> 11) & 3))


def urb_interval(speed: Speed, transfer_type: int, b_interval: int) -> int:
    """The positive ``urb->interval`` Linux ``usb_fill_int_urb`` would use.

    USB/IP forwards this value and the device-side ``usb_submit_urb`` rejects
    a non-positive interrupt interval, which makes the stock stub drop the
    connection. High speed uses microframes (2**(bInterval-1)); full and low
    speed use frames. Bulk and control return 0.
    """
    if transfer_type not in (XFER_INT, XFER_ISOC):
        return 0
    if speed >= Speed.HIGH:
        return 1 << (min(max(b_interval, 1), 16) - 1)
    return max(b_interval, 1)
