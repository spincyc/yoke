"""Endpoint zero races the native worker must survive (reviews of 2026-11-04).

- The UDC completes an OUT-with-data request's status stage when the data
  arrives, so the host's next SETUP can reach the worker before its EP0_READ
  returns. The data must still reach the device (CDC line coding, vendor
  writes; HID SET_REPORT is the same path).
- A bus reset or disconnect while a forwarded request is still owed its
  answer: Raw Gadget keeps that SETUP pending and refuses later ones until
  ep0 answers, so the worker must release it or every re-enumeration fails.

The Python worker runs the same scenarios as the reference. Simulated
software evidence only.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from tests.test_usb_proxy import rig_for
from tests.usbsim.classes import CdcHost
from tests.usbsim.devices import (
    GET_SCRATCH,
    SET_SCRATCH,
    VENDOR_IN_DEVICE,
    VENDOR_OUT_DEVICE,
    CdcAcm,
    CompositeDevice,
)
from tests.usbsim.host import HostProtocolError
from tests.usbsim.native import make_native_rig

from yoke.usb.ch9 import Setup

DELAY = 0.02  # EP0_READ returns this long after the host's status stage


@contextmanager
def enumerated(worker: str, device: Any) -> Iterator[Any]:
    if worker == "native":
        rig = make_native_rig(device)
        try:
            rig.host.attach()
            rig.host.enumerate("linux")
            yield rig
        finally:
            rig.close()
    else:
        with rig_for(device) as rig:
            rig.host.attach()
            rig.host.enumerate("linux")
            yield rig


WORKERS = ["native", "python"]


@pytest.mark.parametrize("worker", WORKERS)
def test_line_coding_survives_the_next_setup_racing_ep0_read(worker: str) -> None:
    with enumerated(worker, CdcAcm()) as rig:
        rig.kernel.ep0_read_delay = DELAY
        cdc = CdcHost(rig.host)
        for baud in (9600, 19200, 57600, 115200, 230400):
            cdc.set_line_coding(baud, bits=7)
            assert cdc.get_line_coding() == (baud, 0, 0, 7)


@pytest.mark.parametrize("worker", WORKERS)
def test_vendor_write_survives_the_next_setup_racing_ep0_read(worker: str) -> None:
    device = CompositeDevice()
    with enumerated(worker, device) as rig:
        rig.kernel.ep0_read_delay = DELAY
        for n in range(5):
            data = bytes([n]) * 16
            rig.host.control(Setup(VENDOR_OUT_DEVICE, SET_SCRATCH, 0, 0, 16), data)
            got = rig.host.control(Setup(VENDOR_IN_DEVICE, GET_SCRATCH, 0, 0, 64))
            assert got == data


VENDOR_IN = Setup(0xC0, 0x01, 0, 0, 64)


def _abandon_then(rig: Any, event: Callable[[], None], attach: bool) -> list[str]:
    """A forwarded request the host gives up on, then a bus event."""
    rig.usbipd.service_delay = 0.3  # the remote answers slowly
    result = rig.kernel.host_control(VENDOR_IN, b"", 0.02)
    assert result.outcome == "timeout"  # the host gave up; the SETUP is owed
    event()
    time.sleep(0.05)
    rig.usbipd.service_delay = 0.0
    time.sleep(0.8)  # the remote answer arrives late
    outcomes = []
    for _ in range(3):
        if attach:
            rig.host.attach()
        try:
            rig.host.enumerate("linux")
            outcomes.append("ok")
            break
        except HostProtocolError as exc:
            outcomes.append(f"fail: {exc}")
    return outcomes


@pytest.mark.parametrize("event", ["reset", "disconnect"])
def test_native_reenumerates_at_once_after_a_bus_event_mid_forward(event: str) -> None:
    with enumerated("native", CompositeDevice()) as rig:
        outcomes = _abandon_then(rig, getattr(rig.host, event), event == "disconnect")
        assert outcomes == ["ok"], outcomes
