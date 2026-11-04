"""Minimal userspace usbipd for the real-kernel VM test (needs root).

The host has the kernel USB/IP drivers but not the ``usbip``/``usbipd``
tools. This follows ``tools/usb/usbip`` exactly where it touches the
kernel, so the in-kernel ``usbip-host`` stub carries every URB:

* bind: unbind the device's driver, ``add <busid>`` to usbip-host's
  ``match_busid``, then write the busid to usbip-host's ``bind``;
* import: check ``usbip_status`` is 1 (available), write the accepted
  socket's descriptor to ``usbip_sockfd`` (the stub takes the socket), then
  send OP_REP_IMPORT on it and close the userspace descriptor.

Usage: ``usbipd_lite.py BUSID [PORT]`` (binds, then serves until killed).
"""

from __future__ import annotations

import socket
import struct
import sys
from pathlib import Path

from yoke.usb import usbip

DEVICES = Path("/sys/bus/usb/devices")
HOST_DRIVER = Path("/sys/bus/usb/drivers/usbip-host")
SPEEDS = {"1.5": 1, "12": 2, "480": 3, "5000": 5, "10000": 6}


def attr(busid: str, name: str) -> str:
    return (DEVICES / busid / name).read_text().strip()


def bind(busid: str) -> None:
    driver = DEVICES / busid / "driver"
    if driver.exists() and driver.resolve().name == "usbip-host":
        return
    if driver.exists():
        (driver / "unbind").write_text(busid)
    (HOST_DRIVER / "match_busid").write_text(f"add {busid}")
    (HOST_DRIVER / "bind").write_text(busid)


def device_info(busid: str) -> usbip.DeviceInfo:
    return usbip.DeviceInfo(
        path=str((DEVICES / busid).resolve()),
        busid=busid,
        busnum=int(attr(busid, "busnum")),
        devnum=int(attr(busid, "devnum")),
        speed=SPEEDS.get(attr(busid, "speed"), 0),
        vendor=int(attr(busid, "idVendor"), 16),
        product=int(attr(busid, "idProduct"), 16),
        bcd_device=int(attr(busid, "bcdDevice"), 16),
        device_class=int(attr(busid, "bDeviceClass"), 16),
        device_subclass=int(attr(busid, "bDeviceSubClass"), 16),
        device_protocol=int(attr(busid, "bDeviceProtocol"), 16),
        configuration_value=int(attr(busid, "bConfigurationValue") or "0"),
        num_configurations=int(attr(busid, "bNumConfigurations")),
        num_interfaces=int(attr(busid, "bNumInterfaces") or "0"),
    )


def recv_exact(conn: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = conn.recv(size - len(data))
        if not chunk:
            raise ConnectionError("peer closed")
        data += chunk
    return data


def serve(busid: str, port: int) -> None:
    bind(busid)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen(4)
    print(f"usbipd-lite: serving {busid} on {port}", flush=True)
    while True:
        conn, _ = listener.accept()
        # usbipd: usbip_net_set_nodelay() and usbip_net_set_keepalive(); the
        # stub sends replies on this socket, so Nagle would add ~40 ms each.
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        try:
            version, code, _status = struct.unpack(">HHI", recv_exact(conn, 8))
            if version != usbip.USBIP_VERSION or code != usbip.OP_REQ_IMPORT:
                conn.close()
                continue
            wanted = usbip.decode_req_import(recv_exact(conn, usbip.BUSID_SIZE))
            if wanted != busid or attr(busid, "usbip_status") != "1":
                conn.sendall(usbip.encode_rep_import(None))
                conn.close()
                continue
            info = device_info(busid)
            (DEVICES / busid / "usbip_sockfd").write_text(str(conn.fileno()))
            conn.sendall(usbip.encode_rep_import(info))
            print(f"usbipd-lite: exported {busid} to a client", flush=True)
        except (OSError, ConnectionError, ValueError) as exc:
            print(f"usbipd-lite: {exc}", flush=True)
        finally:
            conn.close()  # the stub holds its own reference to the socket


if __name__ == "__main__":
    serve(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 3240)
