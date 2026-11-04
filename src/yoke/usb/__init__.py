"""USB data path for the computer-side box (ADRs 0021, 0022).

This package is the payload plane. A separate worker process
(:mod:`yoke.usb.worker`) imports one remote device with the documented
USB/IP protocol and presents it through the local UDC with a gadget
backend. The agent never imports the payload modules: its
``usb-gadget`` driver (:mod:`yoke.drivers.gadget`) exchanges only route
and status metadata with the worker through :mod:`yoke.usb.control`.

Pure modules (no sockets, clocks or files): :mod:`ch9`, :mod:`usbip`,
:mod:`descriptors`, :mod:`policy`. I/O lives in :mod:`client`,
:mod:`rawgadget`, :mod:`proxy`, :mod:`control` and :mod:`worker`.
"""
