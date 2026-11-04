"""The gadget worker: a separate payload process supervised by the agent.

Run as ``python -m yoke.usb.worker --udc fcc00000.usb`` (see
``examples/yoke-gadget.service``). The worker owns ``/dev/raw-gadget`` and
the USB/IP TCP connection; the agent's ``usb-gadget`` driver sends it
metadata-only routes over :mod:`yoke.usb.control` and reads status back
(ADR 0022). It reconnects after remote loss with capped backoff and keeps
routes in memory only.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import signal
import socket
import stat
import threading
import time
from collections.abc import Callable
from types import FrameType

from yoke.usb import control
from yoke.usb.client import ImportRejectedError, UsbipError
from yoke.usb.control import ControlError, Route, Target, WorkerStatus
from yoke.usb.descriptors import DescriptorError, UdcBudget, UnsupportedError
from yoke.usb.gadget import GadgetError
from yoke.usb.policy import ProxyPolicy
from yoke.usb.proxy import Proxy, TeardownError
from yoke.usb.usbip import ProtocolError

LOG = logging.getLogger("yoke.usb.worker")
SessionFactory = Callable[[Target], Proxy]
RETRYABLE = (
    OSError,
    UsbipError,
    ProtocolError,
    DescriptorError,
    GadgetError,
    TimeoutError,
)


class Worker:
    """Follow routes; keep at most one proxy session alive."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        udc: str = "",
        retry_min: float = 0.25,
        retry_max: float = 10.0,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._factory = session_factory
        self._udc = udc
        self._retry_min = retry_min
        self._retry_max = retry_max
        self._clock = clock
        self._monotonic = monotonic
        self._op = threading.Lock()  # serializes route changes and attempts
        self._state_lock = threading.Lock()
        self._wake = threading.Event()
        self._generation = 0
        self._target: Target | None = None
        self._proxy: Proxy | None = None
        self._state = "idle"
        self._detail = ""
        self._backoff = retry_min
        self._next_attempt = 0.0
        self._reconnects = 0
        self._last: dict[str, object] = {}

    # --- status ----------------------------------------------------------

    def _set(self, state: str, detail: str = "") -> None:
        with self._state_lock:
            self._state = state
            self._detail = detail[:500]

    def status(self) -> WorkerStatus:
        with self._state_lock:
            snapshot = dict(self._last)
            state, detail = self._state, self._detail
            generation, target = self._generation, self._target
        counters = snapshot.get("counters", {})
        device: dict[str, str] = {
            key: str(snapshot[key])
            for key in ("vendor", "product", "remote_speed", "presented_speed")
            if key in snapshot
        }
        return WorkerStatus(
            generation=generation,
            state=state,
            detail=detail,
            target=target,
            udc=self._udc,
            link_speed=str(snapshot.get("link_speed", "")),
            device=device,
            counters=dict(counters) if isinstance(counters, dict) else {},
            reconnects=self._reconnects,
            updated=self._clock(),
        )

    # --- route changes ------------------------------------------------------

    def _teardown(self) -> None:
        proxy = self._proxy
        if proxy is None:
            return
        try:
            proxy.stop()
        except TeardownError as exc:
            self._set("error", str(exc))
            raise
        self._proxy = None
        with self._state_lock:
            self._last = {}

    def apply(self, route: Route) -> WorkerStatus:
        """Adopt ``route`` after confirmed teardown of any older session."""
        with self._op:
            if route.generation <= self._generation:
                return self.status()  # idempotent repeat or stale
            try:
                self._teardown()
            except TeardownError:
                return self.status()  # previous generation still owns the UDC
            with self._state_lock:
                self._generation = route.generation
                self._target = route.target
            self._backoff = self._retry_min
            self._next_attempt = 0.0
            self._set("idle" if route.target is None else "connecting")
        self._wake.set()
        return self.status()

    # --- supervision ------------------------------------------------------------

    def step(self) -> None:
        """One supervision pass: start, observe or restart the session."""
        with self._op:
            target = self._target
            proxy = self._proxy
            if target is None:
                return
            if proxy is not None:
                snapshot = proxy.snapshot()
                with self._state_lock:
                    self._last = snapshot
                if proxy.ended.is_set():
                    reason = proxy.end_reason or "ended"
                    with contextlib.suppress(TeardownError):
                        self._teardown()
                    if self._proxy is None:
                        self._reconnects += 1
                        if proxy.rejected:
                            self._schedule("rejected", proxy.rejected, slow=True)
                        else:
                            self._set("connecting", f"session ended: {reason}")
                            self._next_attempt = self._monotonic() + self._retry_min
                    return
                state = str(snapshot.get("state", "presenting"))
                if state not in control.STATES:
                    state = "presenting"
                if proxy.rejected:
                    self._set("rejected", proxy.rejected)
                else:
                    self._set(state, str(snapshot.get("error") or ""))
                return
            if self._monotonic() < self._next_attempt:
                return
            try:
                self._proxy = self._factory(target)
            except UnsupportedError as exc:
                self._schedule("rejected", f"unsupported device: {exc}", slow=True)
                return
            except ImportRejectedError as exc:
                self._schedule("connecting", str(exc))
                return
            except RETRYABLE as exc:
                self._schedule("connecting", f"{type(exc).__name__}: {exc}")
                return
            self._backoff = self._retry_min
            self._set("presenting")

    def _schedule(self, state: str, detail: str, *, slow: bool = False) -> None:
        LOG.warning("session attempt failed: %s", detail)
        self._set(state, detail)
        delay = self._retry_max if slow else self._backoff
        self._next_attempt = self._monotonic() + delay
        self._backoff = min(self._backoff * 2, self._retry_max)

    def run(self, stop: threading.Event, interval: float = 0.1) -> None:
        while not stop.is_set():
            self.step()
            self._wake.wait(interval)
            self._wake.clear()

    def shutdown(self) -> None:
        with self._op:
            self._teardown()
            with self._state_lock:
                self._target = None
            self._set("idle")


class ControlServer:
    """Serve :mod:`yoke.usb.control` requests on a Unix stream socket."""

    def __init__(self, path: str, worker: Worker, *, mode: int = 0o660) -> None:
        self.path = path
        self.worker = worker
        self.mode = mode
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if not self.path.startswith("\0"):
            with contextlib.suppress(FileNotFoundError):
                if stat.S_ISSOCK(os.lstat(self.path).st_mode):
                    os.unlink(self.path)
        sock.bind(self.path)
        if not self.path.startswith("\0"):
            os.chmod(self.path, self.mode)
        sock.listen(4)
        self._sock = sock
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        assert self._sock is not None
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with conn:
                self._handle(conn)

    def _handle(self, conn: socket.socket) -> None:
        conn.settimeout(2.0)
        try:
            route = control.parse_request(control.decode(control.read_line(conn)))
            status = self.worker.status() if route is None else self.worker.apply(route)
            reply: dict[str, object] = {"ok": True, "status": status.to_json()}
        except ControlError as exc:
            reply = {"ok": False, "error": str(exc)[:200]}
        except OSError:
            return
        with contextlib.suppress(OSError):
            conn.sendall(control.encode(reply))

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        if not self.path.startswith("\0"):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self.path)


def connect_usbip(target: Target, *, timeout: float) -> socket.socket:
    """Open the USB/IP TCP connection the producer's admission will accept.

    IPv4 only, bound to ``target.source`` when set: the producer admits
    TCP 3240 only from the IPv4 address its consumer announces from (⚠).
    Keepalive detects a powered-off peer within a few seconds.
    """
    address = socket.getaddrinfo(
        target.host, target.port, socket.AF_INET, socket.SOCK_STREAM
    )[0][4]
    sock = socket.create_connection(
        (str(address[0]), int(address[1])),
        timeout=timeout,
        source_address=(target.source, 0) if target.source else None,
    )
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    for name, value in (("TCP_KEEPIDLE", 2), ("TCP_KEEPINTVL", 1), ("TCP_KEEPCNT", 3)):
        option = getattr(socket, name, None)
        if option is not None:
            sock.setsockopt(socket.IPPROTO_TCP, option, value)
    return sock


def production_factory(
    udc: str, policy: ProxyPolicy, *, device_path: str = "/dev/raw-gadget"
) -> SessionFactory:
    """Real TCP to the device-side usbipd, Raw Gadget on the local UDC (⚠)."""
    from yoke.usb.rawgadget import RawGadgetBackend
    from yoke.usb.session import open_session

    def factory(target: Target) -> Proxy:
        sock = connect_usbip(target, timeout=policy.import_timeout)
        backend = RawGadgetBackend(udc, udc, path=device_path)
        return open_session(sock, target.busid, backend, policy, log=LOG.info)

    return factory


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m yoke.usb.worker",
        description="Present one remote USB/IP device through the local UDC.",
    )
    parser.add_argument("--udc", required=True, help="UDC name, e.g. fcc00000.usb")
    parser.add_argument("--socket", default=control.DEFAULT_SOCKET)
    parser.add_argument("--raw-gadget", default="/dev/raw-gadget")
    parser.add_argument("--power-ma", type=int, default=500)
    parser.add_argument("--max-in-endpoints", type=int, default=7)
    parser.add_argument("--max-out-endpoints", type=int, default=7)
    parser.add_argument("--in-depth", type=int, default=16)
    parser.add_argument("--out-window", type=int, default=1)
    parser.add_argument(
        "--target", help="standalone bench mode: HOST:PORT/BUSID, no agent needed"
    )
    parser.add_argument(
        "--source", help="standalone bench mode: local IPv4 to connect from"
    )
    parser.add_argument("--log-level", default="INFO")
    return parser


def _parse_target(text: str, source: str | None = None) -> Target:
    hostport, _, busid = text.rpartition("/")
    host, _, port = hostport.rpartition(":")
    try:
        return Target(host.strip("[]"), int(port), busid, source)
    except (ValueError, ControlError) as exc:
        raise SystemExit(f"bad --target {text!r}: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=args.log_level, format="%(levelname)s %(name)s: %(message)s"
    )
    policy = ProxyPolicy(
        power_ma=args.power_ma,
        budget=UdcBudget(max_in=args.max_in_endpoints, max_out=args.max_out_endpoints),
        in_depth_bulk=args.in_depth,
        out_window=args.out_window,
    )
    stop = threading.Event()

    def interrupt_handler(_signum: int, _frame: FrameType | None) -> None:
        """No-op: SIGUSR1 only interrupts blocked Raw Gadget ioctls (EINTR)."""

    def terminate(_signum: int, _frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGUSR1, interrupt_handler)
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    worker = Worker(
        production_factory(args.udc, policy, device_path=args.raw_gadget), udc=args.udc
    )
    server = None
    if args.target:
        worker.apply(Route(1, _parse_target(args.target, args.source)))
    else:
        server = ControlServer(args.socket, worker)
        server.start()
    try:
        worker.run(stop)
    finally:
        if server is not None:
            server.close()
    try:
        worker.shutdown()
    except TeardownError as exc:
        LOG.error("teardown unconfirmed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
