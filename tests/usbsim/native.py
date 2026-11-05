"""Run the native gadget worker (ADR 0027) inside the usbsim rig.

The worker's simulation build links ``native/yusb/sim/sys_rpc.c``, so its
Raw Gadget ioctls and UDC sysfs reads reach a :class:`FakeRawGadgetKernel`
through :mod:`tests.usbsim.rpc`; USB/IP goes over real TCP to
:class:`FakeUsbipd`; the agent's control socket schema (ADR 0022) drives it.
Host, device and exporter models are the ones the Python worker is tested
against, so both implementations face the same oracle. Simulated evidence.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tests.usbsim.device import DeviceModel
from tests.usbsim.host import UsbHost
from tests.usbsim.rpc import RawGadgetRpcServer
from tests.usbsim.streams import TcpRelay
from tests.usbsim.udc import FakeRawGadgetKernel
from tests.usbsim.usbipd import FakeUsbipd
from yoke.usb.ch9 import Speed
from yoke.usb.control import ControlClient, Route, Target, WorkerStatus
from yoke.usb.gadget import UdcEndpoint

ROOT = Path(__file__).resolve().parents[2]
USB = ROOT / "native" / "yusb"
LZ4 = ROOT / "native" / "third_party" / "lz4"
OUT = ROOT / ".scratch" / "native"
BUSID = "1-1"
FLAGS = ("-std=c11", "-Wall", "-Wextra", "-Werror", "-Wshadow", "-Wstrict-prototypes")
SANITIZE = ("-O1", "-g", "-fno-omit-frame-pointer", "-fsanitize=address,undefined")
TSAN = ("-O1", "-g", "-fno-omit-frame-pointer", "-fsanitize=thread")


def worker_sanitizer() -> tuple[str, ...]:
    """Sanitizer flags for the worker: ASan/UBSan, or TSan when
    ``$YOKE_NATIVE_SANITIZER`` is ``thread`` (``make tsan-usb``)."""
    choice = os.environ.get("YOKE_NATIVE_SANITIZER", "address")
    if choice not in ("address", "thread"):
        raise ValueError(f"YOKE_NATIVE_SANITIZER must be address or thread: {choice!r}")
    return TSAN if choice == "thread" else SANITIZE


def sources(kind: str) -> list[Path]:
    """Library + Linux worker; ``sim`` swaps libc touchpoints for the RPC shim."""
    files = [*sorted((USB / "src").glob("*.c")), LZ4 / "lz4.c"]
    files += sorted((USB / "linux").glob("*.c"))
    if kind == "sim":
        files = [f for f in files if f.name != "sys_linux.c"] + [USB / "sim/sys_rpc.c"]
    return files


def build(kind: str = "sim", *, sanitize: bool = True, cc: str | None = None) -> Path:
    """Compile once per content hash; returns the binary path."""
    compiler = cc or os.environ.get("CC") or shutil.which("cc") or "cc"
    files = sources(kind)
    flags = [*FLAGS, *(worker_sanitizer() if sanitize else ("-O2", "-g"))]
    includes = ["-I", str(USB / "include"), "-I", str(USB / "linux"), "-I", str(LZ4)]
    digest = hashlib.sha256(" ".join([compiler, *flags]).encode())
    for path in [*files, *sorted((USB / "include").glob("*.h"))]:
        digest.update(path.read_bytes())
    for path in sorted((USB / "src").glob("*.h")) + sorted((USB / "linux").glob("*.h")):
        digest.update(path.read_bytes())
    OUT.mkdir(parents=True, exist_ok=True)
    name = "yoke-gadget-worker" + ("-sim" if kind == "sim" else "")
    target = OUT / f"{name}-{digest.hexdigest()[:12]}"
    if not target.exists():
        subprocess.run(
            [
                compiler,
                *flags,
                *includes,
                *map(str, files),
                "-o",
                str(target),
                "-lpthread",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    return target


def build_exporter(*, sanitize: bool = True) -> Path:
    """``yusb-export-sim``: the export core serving a simulated BOT disk."""
    compiler = os.environ.get("CC") or shutil.which("cc") or "cc"
    files = [
        *sorted((USB / "src").glob("*.c")),
        LZ4 / "lz4.c",
        USB / "sim/simdev.c",
        USB / "sim/export_server.c",
    ]
    flags = [*FLAGS, *(SANITIZE if sanitize else ("-O2", "-g"))]
    includes = ["-I", str(USB / "include"), "-I", str(USB / "sim"), "-I", str(LZ4)]
    digest = hashlib.sha256(" ".join([compiler, *flags]).encode())
    for path in [*files, *sorted((USB / "include").glob("*.h"))]:
        digest.update(path.read_bytes())
    for path in sorted((USB / "src").glob("*.h")) + sorted((USB / "sim").glob("*.h")):
        digest.update(path.read_bytes())
    OUT.mkdir(parents=True, exist_ok=True)
    target = OUT / f"yusb-export-sim-{digest.hexdigest()[:12]}"
    if not target.exists():
        subprocess.run(
            [compiler, *flags, *includes, *map(str, files), "-o", str(target)],
            check=True,
            capture_output=True,
            text=True,
        )
    return target


class ExportServer:
    """The native export core over TCP with a simulated disk (a shared file)."""

    def __init__(self, *options: str, disk_mb: int = 8) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="yexp-"))
        self.disk = self.tmp / "disk.img"
        self.log = self.tmp / "export.log"
        command = [
            str(build_exporter()),
            "--disk",
            str(self.disk),
            "--disk-mb",
            str(disk_mb),
            *options,
        ]
        with self.log.open("w") as sink:
            self.process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=sink, text=True
            )
        assert self.process.stdout is not None
        self.port = int(self.process.stdout.readline())
        self.address = ("127.0.0.1", self.port)

    def read_disk(self, offset: int, size: int) -> bytes:
        with self.disk.open("rb") as f:
            f.seek(offset)
            return f.read(size)

    def stop(self) -> str:
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
            self.process.wait(10)
        text = self.log.read_text(errors="replace")
        shutil.rmtree(self.tmp, ignore_errors=True)
        assert "Sanitizer" not in text and "runtime error" not in text, text[-3000:]
        return text


@dataclass
class NativeRig:
    """host model ↔ fake UDC ↔ native worker ↔ TCP ↔ fake usbipd ↔ device."""

    device: DeviceModel
    usbipd: FakeUsbipd
    kernel: FakeRawGadgetKernel
    host: UsbHost
    binary: Path
    options: tuple[str, ...] = ()
    latency: float = 0.0
    jitter: float = 0.0
    tmp: Path = field(default_factory=lambda: Path(tempfile.mkdtemp(prefix="ynat-")))
    process: subprocess.Popen[str] | None = None
    rpc: RawGadgetRpcServer | None = None
    relay: TcpRelay | None = None
    address: tuple[str, int] | None = None
    generation: int = 0
    log: Path | None = None

    @property
    def control(self) -> ControlClient:
        return ControlClient(str(self.tmp / "control.sock"), timeout=10.0)

    def start(self) -> None:
        self.rpc = RawGadgetRpcServer(self.kernel, self.tmp / "rpc.sock")
        self.log = self.tmp / "worker.log"
        env = dict(os.environ, YOKE_GADGET_RPC=str(self.tmp / "rpc.sock"))
        env.setdefault("ASAN_OPTIONS", "detect_leaks=1:abort_on_error=0")
        env.setdefault("TSAN_OPTIONS", "halt_on_error=0:second_deadlock_stack=1")
        command = [
            str(self.binary),
            "--udc",
            self.kernel.name,
            "--gadget-driver",
            self.kernel.gadget_name,
            "--socket",
            str(self.tmp / "control.sock"),
            "--log-level",
            "DEBUG",
            *self.options,
        ]
        with self.log.open("w") as sink:
            self.process = subprocess.Popen(
                command, env=env, stdout=sink, stderr=subprocess.STDOUT, text=True
            )
        wait_for(lambda: (self.tmp / "control.sock").exists(), 10)

    def listen(self) -> tuple[str, int]:
        if self.address is None:
            self.address = self.usbipd.listen()
            if self.latency or self.jitter:
                self.relay = TcpRelay(
                    self.address, latency=self.latency, jitter=self.jitter
                )
        return self.relay.address if self.relay is not None else self.address

    def route(self, target: Target | None) -> WorkerStatus:
        current = self.control.status()
        self.generation = max(current.generation, self.generation) + 1
        return self.control.route(Route(self.generation, target))

    def connect(self) -> WorkerStatus:
        host, port = self.listen()
        return self.route(Target(host, port, BUSID, None))

    def status(self) -> WorkerStatus:
        return self.control.status()

    def counters(self) -> dict[str, int]:
        return self.status().counters

    def wait_state(self, *states: str, timeout: float = 10.0) -> WorkerStatus:
        status: list[WorkerStatus] = []

        def reached() -> bool:
            status[:] = [self.status()]
            return status[0].state in states

        wait_for(reached, timeout, lambda: f"worker status {status}")
        return status[0]

    def present(self) -> WorkerStatus:
        self.connect()
        return self.wait_state("presenting", "attached", "rejected")

    def stop(self) -> int | None:
        """SIGTERM; returns the exit code (None if it had to be killed)."""
        if self.process is None or self.process.poll() is not None:
            return None if self.process is None else self.process.returncode
        self.process.send_signal(signal.SIGTERM)
        try:
            return self.process.wait(15)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(5)
            return None

    def output(self) -> str:
        return self.log.read_text(errors="replace") if self.log else ""

    def close(self) -> None:
        code = self.stop()
        if self.rpc is not None:
            self.rpc.close()
        if self.relay is not None:
            self.relay.close()
        self.usbipd.stop()
        text = self.output()
        shutil.rmtree(self.tmp, ignore_errors=True)
        sanitizer = [
            line
            for line in text.splitlines()
            if "Sanitizer" in line or "runtime error" in line
        ]
        assert not sanitizer, "\n".join(text.splitlines()[-60:])
        assert code in (0, None), f"worker exit {code}\n{text[-4000:]}"


def wait_for(
    predicate: Callable[[], object],
    timeout: float = 5.0,
    describe: Callable[[], str] | None = None,
) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(
                "condition not reached" + (f": {describe()}" if describe else "")
            )
        time.sleep(0.005)


def make_native_rig(
    device: DeviceModel,
    *,
    options: tuple[str, ...] = (),
    host_speed: Speed = Speed.HIGH,
    udc_handles_standard: bool = True,
    udc_endpoints: list[UdcEndpoint] | None = None,
    unlink_mode: str = "cancel",
    clear_halt_cancels: bool = True,
    latency: float = 0.0,
    jitter: float = 0.0,
    service_delay: float = 0.0,
    gadget_name: str = "dwc3-gadget",
    present: bool = True,
    **_: Any,
) -> NativeRig:
    usbipd = FakeUsbipd(
        {BUSID: device}, unlink_mode=unlink_mode, service_delay=service_delay
    )
    kernel = FakeRawGadgetKernel(
        udc_handles_standard=udc_handles_standard,
        endpoints=udc_endpoints,
        clear_halt_cancels=clear_halt_cancels,
    )
    kernel.gadget_name = gadget_name
    rig = NativeRig(
        device=device,
        usbipd=usbipd,
        kernel=kernel,
        host=UsbHost(kernel, max_speed=host_speed),
        binary=build("sim"),
        options=options,
        latency=latency,
        jitter=jitter,
    )
    try:
        rig.start()
        if present:
            rig.present()
    except BaseException:
        with contextlib.suppress(BaseException):
            rig.close()
        raise
    return rig
