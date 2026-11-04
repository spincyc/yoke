"""Assemble one proxy session: import, fetch descriptors, present.

Shared by the worker (real TCP socket, Raw Gadget) and the simulation
(in-memory stream or loopback TCP, simulated kernel), so tests exercise the
production assembly path.
"""

from __future__ import annotations

from collections.abc import Callable

from yoke.usb.client import (
    ByteStream,
    ConnectionLostError,
    UsbipConnection,
    abort_stream,
    import_device,
)
from yoke.usb.descriptors import build_presentation
from yoke.usb.gadget import GadgetBackend
from yoke.usb.policy import ProxyPolicy
from yoke.usb.proxy import Proxy


def open_session(
    stream: ByteStream,
    busid: str,
    backend: GadgetBackend,
    policy: ProxyPolicy,
    *,
    log: Callable[[str], None] | None = None,
) -> Proxy:
    """Import ``busid`` over ``stream`` and start presenting it.

    Raises :class:`~yoke.usb.client.ImportRejectedError`,
    :class:`~yoke.usb.descriptors.UnsupportedError`,
    :class:`~yoke.usb.descriptors.DescriptorError` or a USB/IP/gadget error;
    the stream is closed on failure.
    """
    try:
        info = import_device(stream, busid, timeout=policy.import_timeout)
    except BaseException:
        abort_stream(stream)
        raise
    holder: list[Proxy] = []

    def lost(exc: BaseException) -> None:
        if holder:
            holder[0]._remote_lost(exc)

    conn = UsbipConnection(stream, info, on_lost=lost)
    conn.start()
    try:
        presentation = build_presentation(
            lambda setup: conn.control(setup, timeout=policy.import_timeout),
            info.speed,
            power_ma=policy.power_ma,
            ep0_size=policy.ep0_size,
            budget=policy.budget,
        )
        proxy = Proxy(conn, backend, presentation, policy, log=log)
        holder.append(proxy)
        if conn.lost is not None:
            raise ConnectionLostError(str(conn.lost))
        proxy.start()
    except BaseException:
        conn.close()
        raise
    return proxy
