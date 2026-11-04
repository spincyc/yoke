"""Pure-software USB data-path simulation (ADR 0022).

Importable building blocks, reused by ``benchmarks/usb_datapath.py``:

* :mod:`packets` - USB packetization and HCD/UDC transfer-completion rules.
* :mod:`build` - descriptor byte builders.
* :mod:`device` / :mod:`devices` - device models behind the simulated exporter.
* :mod:`streams` - in-memory byte streams and a TCP relay with latency,
  jitter and reset injection.
* :mod:`usbipd` - a USB/IP exporter that applies stock-stub semantics.
* :mod:`udc` - a simulated Raw Gadget kernel and UDC (the ioctl surface).
* :mod:`host` - a USB host that enumerates like Linux, Windows and macOS and
  enforces USB 2.0 §9.2.6.4 control deadlines.
* :mod:`harness` - wires host, gadget, proxy, USB/IP and device together.

Everything here is a model. Results are simulated software evidence, never
hardware or interoperability evidence.
"""
