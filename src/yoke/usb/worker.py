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
import sys
import threading
import time
from collections.abc import Callable
from types import FrameType

from yoke.usb import control
from yoke.usb.client import (
    ByteStream,
    ConnectionLostError,
    ImportRejectedError,
    UsbipError,
    abort_stream,
)
from yoke.usb.control import ControlError, Route, Target, WorkerStatus
from yoke.usb.descriptors import DescriptorError, UdcBudget, UnsupportedError
from yoke.usb.gadget import GadgetError
from yoke.usb.policy import ProxyPolicy
from yoke.usb.proxy import Proxy, TeardownError
from yoke.usb.usbip import ProtocolError

LOG = logging.getLogger("yoke.usb.worker")
RETRYABLE = (
    OSError,
    UsbipError,
    ProtocolError,
    DescriptorError,
    GadgetError,
    TimeoutError,
)


class BuildTicket:
    """Lets a route change abort a session that is still being built.

    Factories register each socket as soon as it exists; :meth:`abort`
    shuts them down, which also aborts a connect in progress, so the
    import and descriptor fetch fail fast instead of running to their
    timeouts while the agent waits for an acknowledgment.
    """

    def __init__(self, generation: int) -> None:
        self.generation = generation
        self.aborted = False
        self._lock = threading.Lock()
        self._streams: list[ByteStream] = []

    def register(self, stream: ByteStream) -> None:
        with self._lock:
            self._streams.append(stream)
            aborted = self.aborted
        if aborted:
            abort_stream(stream)
            raise ConnectionLostError("route changed during session setup")

    def abort(self) -> None:
        with self._lock:
            self.aborted = True
            streams = list(self._streams)
        for stream in streams:
            abort_stream(stream)


SessionFactory = Callable[[Target, BuildTicket], Proxy]


class Worker:
    """Follow routes; keep at most one proxy session alive.

    Sessions are built outside the route lock, so :meth:`apply` never waits
    for a slow import: it aborts the build and then waits only for that
    build's (bounded) cleanup. A generation is adopted only after every older
    session is confirmed stopped.
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        udc: str = "",
        retry_min: float = 0.25,
        retry_max: float = 2.0,
        retry_rejected: float = 30.0,
        build_wait: float = 6.0,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._factory = session_factory
        self._udc = udc
        self._retry_min = retry_min
        self._retry_max = retry_max
        self._retry_rejected = retry_rejected
        self._build_wait = build_wait
        self._clock = clock
        self._monotonic = monotonic
        self._apply_lock = threading.Lock()  # serializes route changes
        self._lock = threading.Lock()  # guards the fields below; never held long
        self._idle = threading.Condition(self._lock)
        self._wake = threading.Event()
        self._generation = 0
        self._target: Target | None = None
        self._proxy: Proxy | None = None
        self._building: BuildTicket | None = None
        self._changing = False
        self._state = "idle"
        self._detail = ""
        self._backoff = retry_min
        self._next_attempt = 0.0
        self._reconnects = 0
        self._last: dict[str, object] = {}

    # --- status ----------------------------------------------------------

    def _set(self, state: str, detail: str = "") -> None:
        with self._lock:
            self._state = state
            self._detail = detail[:500]

    def status(self) -> WorkerStatus:
        with self._lock:
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

    def _stop(self, proxy: Proxy) -> bool:
        """Stop ``proxy``; on unconfirmed teardown keep it and report error."""
        try:
            proxy.stop()
        except TeardownError as exc:
            self._set("error", str(exc))
            return False
        with self._lock:
            if self._proxy is proxy:
                self._proxy = None
                self._last = {}
        return True

    def apply(self, route: Route) -> WorkerStatus:
        """Adopt ``route`` after confirmed teardown of any older session."""
        with self._apply_lock:
            with self._lock:
                if route.generation <= self._generation:
                    return self.status_locked()
                self._changing = True
                if self._building is not None:
                    self._building.abort()
                deadline = self._monotonic() + self._build_wait
                while self._building is not None:
                    remaining = deadline - self._monotonic()
                    if remaining <= 0:
                        self._changing = False
                        self._state, self._detail = (
                            "error",
                            "session build did not stop",
                        )
                        return self.status_locked()
                    self._idle.wait(remaining)
                proxy = self._proxy
            if proxy is not None and not self._stop(proxy):
                with self._lock:
                    self._changing = False
                return self.status()  # previous generation still owns the UDC
            with self._lock:
                self._generation = route.generation
                self._target = route.target
                self._backoff = self._retry_min
                self._next_attempt = 0.0
                self._state = "idle" if route.target is None else "connecting"
                self._detail = ""
                self._changing = False
        self._wake.set()
        return self.status()

    def status_locked(self) -> WorkerStatus:
        """:meth:`status` for callers that hold the state lock."""
        self._lock.release()
        try:
            return self.status()
        finally:
            self._lock.acquire()

    # --- supervision ------------------------------------------------------------

    def step(self) -> None:
        """One supervision pass: start, observe or restart the session."""
        with self._lock:
            if self._changing or self._target is None or self._building is not None:
                return
            target, generation, proxy = self._target, self._generation, self._proxy
            if proxy is None and self._monotonic() < self._next_attempt:
                return
            ticket = None
            if proxy is None:
                ticket = self._building = BuildTicket(generation)
        if proxy is not None:
            self._observe(proxy)
            return
        assert ticket is not None
        self._build(target, ticket)

    def _observe(self, proxy: Proxy) -> None:
        snapshot = proxy.snapshot()
        with self._lock:
            if self._proxy is not proxy:
                return
            self._last = snapshot
        if proxy.ended.is_set():
            reason = proxy.end_reason or "ended"
            if not self._stop(proxy):
                return
            self._reconnects += 1
            if proxy.rejected:
                self._schedule("rejected", proxy.rejected, slow=True)
            else:
                self._set("connecting", f"session ended: {reason}")
                with self._lock:
                    self._next_attempt = self._monotonic() + self._retry_min
            return
        state = str(snapshot.get("state", "presenting"))
        if state not in control.STATES:
            state = "presenting"
        if proxy.rejected:
            self._set("rejected", proxy.rejected)
        else:
            self._set(state, str(snapshot.get("error") or ""))

    def _build(self, target: Target, ticket: BuildTicket) -> None:
        proxy: Proxy | None = None
        failure: tuple[str, str, bool] | None = None
        try:
            proxy = self._factory(target, ticket)
        except UnsupportedError as exc:
            failure = ("rejected", f"unsupported device: {exc}", True)
        except ImportRejectedError as exc:
            failure = ("connecting", str(exc), False)
        except RETRYABLE as exc:
            failure = ("connecting", f"{type(exc).__name__}: {exc}", False)
        with self._lock:
            stale = ticket.aborted or self._generation != ticket.generation
        if stale and proxy is not None:
            with contextlib.suppress(TeardownError):
                proxy.stop()  # the route changed while it was being built
            proxy = None
        with self._lock:
            self._building = None
            if not stale and proxy is not None:
                self._proxy = proxy
                self._backoff = self._retry_min
                self._state, self._detail = "presenting", ""
            self._idle.notify_all()
        if not stale and failure is not None:
            self._schedule(failure[0], failure[1], slow=failure[2])

    def _schedule(self, state: str, detail: str, *, slow: bool = False) -> None:
        LOG.warning("session attempt failed: %s", detail)
        with self._lock:
            self._state, self._detail = state, detail[:500]
            delay = self._retry_rejected if slow else self._backoff
            self._next_attempt = self._monotonic() + delay
            self._backoff = min(self._backoff * 2, self._retry_max)

    def run(self, stop: threading.Event, interval: float = 0.1) -> None:
        while not stop.is_set():
            self.step()
            self._wake.wait(interval)
            self._wake.clear()

    def shutdown(self) -> None:
        """Stop the session for good; raise TeardownError if unconfirmed."""
        with self._apply_lock:
            with self._lock:
                self._changing = True
                if self._building is not None:
                    self._building.abort()
                deadline = self._monotonic() + self._build_wait
                while self._building is not None and self._monotonic() < deadline:
                    self._idle.wait(deadline - self._monotonic())
                proxy, self._target = self._proxy, None
            if proxy is not None:
                proxy.stop()
                with self._lock:
                    self._proxy = None
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


def connect_usbip(
    target: Target,
    *,
    timeout: float,
    register: Callable[[socket.socket], None] | None = None,
) -> socket.socket:
    """Open the USB/IP TCP connection the producer's admission will accept.

    IPv4 only, bound to ``target.source`` when set: the producer admits
    TCP 3240 only from the IPv4 address its consumer announces from (⚠).
    ``register`` sees the socket before it connects, so a route change can
    abort the connect. Keepalive detects a powered-off peer within seconds.
    """
    address = socket.getaddrinfo(
        target.host, target.port, socket.AF_INET, socket.SOCK_STREAM
    )[0][4]
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if register is not None:
            register(sock)
        if target.source:
            sock.bind((target.source, 0))
        sock.settimeout(timeout)
        sock.connect((str(address[0]), int(address[1])))
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for name, value in (
            ("TCP_KEEPIDLE", 2),
            ("TCP_KEEPINTVL", 1),
            ("TCP_KEEPCNT", 3),
        ):
            option = getattr(socket, name, None)
            if option is not None:
                sock.setsockopt(socket.IPPROTO_TCP, option, value)
    except BaseException:
        sock.close()
        raise
    return sock


def production_factory(
    udc: str,
    policy: ProxyPolicy,
    *,
    device_path: str = "/dev/raw-gadget",
    gadget_driver: str | None = None,
) -> SessionFactory:
    """Real TCP to the device-side usbipd, Raw Gadget on the local UDC (⚠)."""
    from yoke.usb.rawgadget import RawGadgetBackend
    from yoke.usb.session import open_session

    def factory(target: Target, ticket: BuildTicket) -> Proxy:
        sock = connect_usbip(
            target, timeout=min(policy.import_timeout, 3.0), register=ticket.register
        )
        backend = RawGadgetBackend(udc, driver_name=gadget_driver, path=device_path)
        LOG.info("Raw Gadget on UDC %s as gadget %s", udc, backend.driver_name)
        return open_session(sock, target.busid, backend, policy, log=LOG.info)

    return factory


NATIVE_BINARY = "/usr/local/libexec/yoke/yoke-gadget-worker"
"""Where ``packaging/zero3e/install.sh computer --native-worker`` installs it."""

EXIT_CONFIG = 78
"""sysexits EX_CONFIG: the native worker binary is missing or not executable.
A restart cannot fix it, so the unit can list it in RestartPreventExitStatus."""


def native_command(args: argparse.Namespace) -> list[str]:
    """The same options for ``yoke-gadget-worker`` (ADR 0027, native/README.md)."""
    command = [args.native_binary, "--udc", args.udc, "--socket", args.socket]
    command += ["--raw-gadget", args.raw_gadget, "--power-ma", str(args.power_ma)]
    command += ["--max-in-endpoints", str(args.max_in_endpoints)]
    command += ["--max-out-endpoints", str(args.max_out_endpoints)]
    command += ["--in-depth", str(args.in_depth), "--out-window", str(args.out_window)]
    command += ["--log-level", args.log_level]
    for flag, value in (
        ("--gadget-driver", args.gadget_driver),
        ("--target", args.target),
        ("--source", args.source),
    ):
        if value:
            command += [flag, value]
    return command


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m yoke.usb.worker",
        description="Present one remote USB/IP device through the local UDC.",
    )
    parser.add_argument("--udc", required=True, help="UDC name, e.g. fcc00000.usb")
    parser.add_argument("--socket", default=control.DEFAULT_SOCKET)
    parser.add_argument("--raw-gadget", default="/dev/raw-gadget")
    parser.add_argument(
        "--gadget-driver",
        help="UDC gadget name for Raw Gadget INIT (default: inferred from "
        "/sys/class/udc/UDC/device/driver; dwc3 -> dwc3-gadget)",
    )
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
    parser.add_argument(
        "--implementation",
        choices=("python", "native"),
        default=os.environ.get("YOKE_GADGET_WORKER", "python"),
        help="native: exec the C worker with the same options (default: "
        "$YOKE_GADGET_WORKER or python)",
    )
    parser.add_argument("--native-binary", default=NATIVE_BINARY)
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
    if args.implementation == "native":
        command = native_command(args)
        try:
            os.execv(command[0], command)  # replaces this process; same unit
        except OSError as exc:
            print(
                f"yoke.usb.worker: cannot run native worker {command[0]!r} "
                f"(--native-binary): {exc.strerror or exc}",
                file=sys.stderr,
            )
            return EXIT_CONFIG
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
        production_factory(
            args.udc,
            policy,
            device_path=args.raw_gadget,
            gadget_driver=args.gadget_driver,
        ),
        udc=args.udc,
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
