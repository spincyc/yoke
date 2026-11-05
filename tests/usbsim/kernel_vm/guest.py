"""Real-kernel loopback scenario, run as root inside the test VM (launch.py).

Every step prints one ``YOKE-VM {json}`` line with ``ok`` true/false. The
agent-path steps run twice: with the Python worker (``yoke.usb.worker``)
and with the native worker (``yoke-gadget-worker``, steps ``native-*``),
each followed by a performance step. Then the native export core serves a
simulated disk that the guest kernel imports through ``vhci-hcd``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import mmap
import os
import random
import signal
import socket
import statistics
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

WORKTREE = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
PYTHON = str(WORKTREE / ".scratch/venv/bin/python")
HERE = WORKTREE / "tests/usbsim/kernel_vm"
GADGET = Path("/sys/kernel/config/usb_gadget/src")
DEVICES = Path("/sys/bus/usb/devices")
UDC = Path("/sys/class/udc")
DISK = Path("/tmp/disk.img")
DISK_SIZE = 32 * 1024 * 1024
VID, PID = "1209", "5eed"
MIB = 1024 * 1024
NATIVE = WORKTREE / ".scratch/native/yoke-gadget-worker"
EXPORTER = WORKTREE / ".scratch/native/yusb-export-sim"
VHCI = Path("/sys/devices/platform/vhci_hcd.0")


def emit(step: str, ok: bool, **data: object) -> None:
    print("YOKE-VM " + json.dumps({"step": step, "ok": ok, **data}), flush=True)


def run(*command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=False)


def wait(predicate: Callable[[], object], timeout: float) -> object:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    return None


def write(path: Path, value: str) -> None:
    path.write_text(value)


def devices_with_ids(vid: str = VID, pid: str = PID) -> list[str]:
    found = []
    for device in DEVICES.iterdir():
        try:
            if (device / "idVendor").read_text().strip() == vid and (
                device / "idProduct"
            ).read_text().strip() == pid:
                found.append(device.name)
        except OSError:
            continue
    return sorted(found)


def block_device(busid: str) -> str | None:
    for path in DEVICES.glob(f"{busid}:1.0/host*/target*/*:*/block/*"):
        return f"/dev/{path.name}"
    return None


def udc_state(name: str) -> str:
    try:
        return (UDC / name / "state").read_text().strip()
    except OSError:
        return "missing"


def drop_caches() -> None:
    os.sync()
    write(Path("/proc/sys/vm/drop_caches"), "3")


def read_block(device: str, offset: int, size: int) -> bytes:
    fd = os.open(device, os.O_RDONLY)
    try:
        os.lseek(fd, offset, os.SEEK_SET)
        data = b""
        while len(data) < size:
            chunk = os.read(fd, size - len(data))
            if not chunk:
                break
            data += chunk
        return data
    finally:
        os.close(fd)


def write_block(device: str, offset: int, data: bytes) -> None:
    fd = os.open(device, os.O_WRONLY)
    try:
        os.lseek(fd, offset, os.SEEK_SET)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
        os.fsync(fd)
    finally:
        os.close(fd)


def make_source_gadget() -> None:
    rng = random.Random(1209)
    DISK.write_bytes(rng.randbytes(DISK_SIZE))
    GADGET.mkdir()
    write(GADGET / "idVendor", f"0x{VID}")
    write(GADGET / "idProduct", f"0x{PID}")
    write(GADGET / "bcdUSB", "0x0200")
    strings = GADGET / "strings/0x409"
    strings.mkdir(parents=True)
    write(strings / "manufacturer", "Yoke VM test")
    write(strings / "product", "Source disk")
    write(strings / "serialnumber", "YOKEVM0001")
    function = GADGET / "functions/mass_storage.usb0"
    function.mkdir(parents=True)
    write(function / "lun.0/file", str(DISK))
    config = GADGET / "configs/c.1"
    config.mkdir(parents=True)
    write(config / "MaxPower", "100")
    (config / "mass_storage.usb0").symlink_to(function)
    write(GADGET / "UDC", "dummy_udc.1")


SOCKET = "/run/yoke-gadget.sock"


def start_worker(
    busid: str,
    *,
    fcntl_control: bool = False,
    agent: bool = False,
    native: bool = False,
) -> subprocess.Popen[str]:
    args = ["-m", "yoke.usb.worker", "--udc", "dummy_udc.0"]
    if agent:
        args += ["--socket", SOCKET]
    else:
        args += ["--target", f"127.0.0.1:3240/{busid}", "--source", "127.0.0.1"]
    if fcntl_control:
        args = [str(HERE / "fcntl_worker.py"), *args[2:]]
    command = [str(NATIVE), *args[2:]] if native else [PYTHON, *args]
    name = "native" if native else "fcntl" if fcntl_control else ""
    log = open(f"/tmp/worker{'-' + name if name else ''}.log", "a")  # noqa: SIM115
    return subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, text=True)


def start_usbipd(busid: str) -> subprocess.Popen[str]:
    log = open("/tmp/usbipd.log", "a")  # noqa: SIM115
    process = subprocess.Popen(
        [PYTHON, str(HERE / "usbipd_lite.py"), busid],
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
    )
    wait(lambda: "serving" in Path("/tmp/usbipd.log").read_text(), 10)
    return process


def proxied(source: str) -> Callable[[], str | None]:
    def find() -> str | None:
        others = [b for b in devices_with_ids() if b != source]
        return others[0] if others else None

    return find


def stop_worker(worker: subprocess.Popen[str], step: str) -> bool:
    begun = time.monotonic()
    worker.send_signal(signal.SIGTERM)
    try:
        code = worker.wait(30)
    except subprocess.TimeoutExpired:
        worker.kill()
        code = None
    seconds = round(time.monotonic() - begun, 2)
    ok = code == 0
    emit(step, ok, exit_code=code, seconds=seconds)
    return ok


def verify_io(source: str, step: str) -> str | None:
    busid = wait(proxied(source), 30)
    if not busid:
        emit(step, False, error="proxied device never enumerated")
        return None
    device = wait(lambda: block_device(str(busid)), 30)
    configured = wait(lambda: udc_state("dummy_udc.0") == "configured", 10)
    if not device:
        emit(step, False, error="no block device", udc=udc_state("dummy_udc.0"))
        return None
    device = str(device)
    drop_caches()
    expected = DISK.read_bytes()[: 8 * MIB]
    begun = time.monotonic()
    data = read_block(device, 0, 8 * MIB)
    read_s = time.monotonic() - begun
    payload = random.Random(7).randbytes(8 * MIB)
    begun = time.monotonic()
    write_block(device, 16 * MIB, payload)
    write_s = time.monotonic() - begun
    landed = DISK.read_bytes()[16 * MIB : 24 * MIB]
    drop_caches()
    readback = read_block(device, 16 * MIB, 8 * MIB)
    ok = data == expected and landed == payload and readback == payload
    emit(
        step,
        ok,
        busid=str(busid),
        device=device,
        speed=(DEVICES / str(busid) / "speed").read_text().strip(),
        udc_configured=bool(configured),
        read_sha256_match=data == expected,
        write_landed_in_source_file=landed == payload,
        readback_match=readback == payload,
        read_mb_s=round(8 / read_s, 2),
        write_mb_s=round(8 / write_s, 2),
        sha256=hashlib.sha256(payload).hexdigest()[:16],
    )
    return str(busid) if ok else None


def timed_read(device: str) -> float:
    drop_caches()
    begun = time.monotonic()
    read_block(device, 0, 8 * MIB)
    return round(8 / (time.monotonic() - begun), 2)


def cpu_seconds(pid: int) -> float:
    """CPU time of every live thread of ``pid`` (schedstat, nanoseconds):
    the worker's own user and system time, including its ioctls and copies."""
    total = 0
    for task in Path(f"/proc/{pid}/task").iterdir():
        with contextlib.suppress(OSError):
            total += int((task / "schedstat").read_text().split()[0])
    return total / 1e9


def direct_buffer(size: int) -> mmap.mmap:
    return mmap.mmap(-1, size)  # page aligned, as O_DIRECT needs


def open_when_ready(device: str, flags: int, timeout: float = 5.0) -> int:
    """A disk listed in sysfs can still refuse opens (ENXIO) while sd adds it."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            return os.open(device, flags)
        except OSError as exc:
            if exc.errno not in (6, 2) or time.monotonic() > deadline:  # ENXIO, ENOENT
                raise
            time.sleep(0.05)


def perf(device: str, step: str, pid: int | None) -> None:
    """Sequential O_DIRECT throughput, the worker's CPU per MiB, 4 KiB latency."""
    size, chunk = 32 * MIB, MIB
    buf = direct_buffer(chunk)
    fd = open_when_ready(device, os.O_RDWR | os.O_DIRECT)
    try:
        result: dict[str, object] = {}
        for label, op in (("read", os.preadv), ("write", os.pwritev)):
            cpu = cpu_seconds(pid) if pid else 0.0
            begun = time.monotonic()
            for offset in range(0, size, chunk):
                op(fd, [buf], offset)
            os.fsync(fd)
            seconds = time.monotonic() - begun
            result[f"{label}_mb_s"] = round(size / MIB / seconds, 1)
            if pid:
                used = cpu_seconds(pid) - cpu
                result[f"{label}_cpu_ms_per_mib"] = round(1000 * used / (size / MIB), 3)
        small = direct_buffer(4096)
        rng = random.Random(4)
        samples = []
        for _ in range(300):
            offset = rng.randrange(size // 4096) * 4096
            begun = time.perf_counter()
            os.preadv(fd, [small], offset)
            samples.append((time.perf_counter() - begun) * 1000)
        samples.sort()
        result["read_4k_median_ms"] = round(statistics.median(samples), 3)
        result["read_4k_p99_ms"] = round(samples[int(len(samples) * 0.99)], 3)
    finally:
        os.close(fd)
    emit(step, True, **result)


def export_available(source: str) -> bool:
    return (DEVICES / source / "usbip_status").read_text().strip() == "1"


def native_sweep(source: str) -> None:
    """Per-URB cost and pipelining: both workers at several BOT URB sizes."""
    for kind, max_transfer in (
        ("native", 65536),
        ("native", 16384),
        ("native", 512),
        ("python", 512),
    ):
        # The previous presentation must be gone, or its stale /dev/sdX is found.
        wait(lambda: proxied(source)() is None, 15)
        wait(lambda: export_available(source), 15)
        options = [
            "--udc",
            "dummy_udc.0",
            "--target",
            f"127.0.0.1:3240/{source}",
            "--source",
            "127.0.0.1",
            "--log-level",
            "WARNING",
        ]
        if kind == "native":
            command = [str(NATIVE), *options, "--max-transfer", str(max_transfer)]
        else:
            command = [PYTHON, str(HERE / "policy_worker.py"), *options]
        worker = subprocess.Popen(
            command,
            env=dict(os.environ, YOKE_MAX_TRANSFER=str(max_transfer)),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        step = f"{kind}-perf-max-transfer-{max_transfer}"
        device = wait(lambda: block_device(str(proxied(source)() or "none")), 30)
        wait(lambda: udc_state("dummy_udc.0") == "configured", 10)
        if device:
            perf(str(device), step, worker.pid)
        else:
            emit(step, False, error="no device")
        worker.send_signal(signal.SIGTERM)
        worker.wait(15)


def agent_steps(
    kind: str, source: str, usbipd: subprocess.Popen[str]
) -> subprocess.Popen[str]:
    """Route, present, I/O, detach, remote loss, usbipd restart, SIGTERM."""
    from yoke.drivers.gadget import UsbGadgetDriver
    from yoke.usb.control import ControlClient

    native = kind == "native"
    name = (lambda step: f"native-{step}") if native else (lambda step: step)
    status_file = DEVICES / source / "usbip_status"
    media = {"host": "127.0.0.1", "port": 3240, "busids": [source]}
    with contextlib.suppress(FileNotFoundError):
        Path(SOCKET).unlink()
    worker = start_worker(source, agent=True, native=native)
    client = ControlClient(SOCKET, timeout=10)
    wait(lambda: Path(SOCKET).exists(), 20)
    driver = UsbGadgetDriver(udc="dummy_udc.0", socket_path=SOCKET)
    # 1. Agent path: route -> present -> enumerate -> checksummed I/O.
    driver.attach("prod-vm", media)
    proxied_busid = verify_io(source, name("io"))
    poll = driver.poll()
    status = client.status()
    emit(
        name("driver-active"),
        poll.state.value == "active",
        detail=poll.detail,
        worker_state=status.state,
        link_speed=status.link_speed,
        counters={k: v for k, v in status.counters.items() if v},
    )
    device = block_device(proxied_busid) if proxied_busid else None
    if device:
        perf(device, name("perf"), worker.pid)
    # 2. Detach through the driver: confirmed teardown (interrupts blocked
    #    EVENT_FETCH/EP_READ) and the export is released (FIN reaches stub).
    begun = time.monotonic()
    driver.detach()
    seconds = round(time.monotonic() - begun, 3)
    gone = wait(lambda: proxied(source)() is None, 15)
    released = wait(lambda: status_file.read_text().strip() == "1", 15)
    emit(
        name("detach"),
        bool(gone and released) and driver.poll().state.value == "idle",
        seconds=seconds,
        device_gone=bool(gone),
        export_available=bool(released),
    )
    # 3. Remote loss and a usbipd restart while presenting.
    driver.attach("prod-vm", media)
    verify_io(source, name("io-again"))
    usbipd.send_signal(signal.SIGTERM)
    usbipd.wait(10)
    begun = time.monotonic()
    write(DEVICES / source / "usbip_sockfd", "-1")  # the stub drops the session
    lost = wait(lambda: proxied(source)() is None, 20)
    emit(
        name("remote-loss-presented"),
        bool(lost),
        seconds=round(time.monotonic() - begun, 2),
    )
    wait(lambda: status_file.read_text().strip() == "1", 20)
    usbipd = start_usbipd(source)
    restarted = time.monotonic()
    back = wait(lambda: block_device(str(proxied(source)() or "none")), 30)
    emit(
        name("reconnect-after-usbipd-restart"),
        bool(back),
        seconds=round(time.monotonic() - restarted, 2),
        reconnects=client.status().reconnects,
    )
    verify_io(source, name("io-after-reconnect"))
    driver.detach()
    stop_worker(worker, name("worker-sigterm"))
    wait(lambda: status_file.read_text().strip() == "1", 15)
    return usbipd


def vhci_import(port_number: int, busid: str) -> tuple[int, socket.socket]:
    """``usbip attach`` by hand: OP_REQ_IMPORT, then hand the socket to vhci."""
    from yoke.usb import usbip

    sock = socket.create_connection(("127.0.0.1", port_number), timeout=5)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.sendall(usbip.encode_req_import(busid))
    reply = b""
    while len(reply) < 8 + usbip.DEVICE_INFO_SIZE:
        chunk = sock.recv(8 + usbip.DEVICE_INFO_SIZE - len(reply))
        if not chunk:
            raise ConnectionError("exporter closed the import")
        reply += chunk
    usbip.parse_op_header(reply[:8], usbip.OP_REP_IMPORT)
    info = usbip.DeviceInfo.unpack(reply[8:])
    port = None
    for line in (VHCI / "status").read_text().splitlines()[1:]:
        fields = line.split()
        if fields[0] == "hs" and fields[2] == "004":  # VDEV_ST_NULL
            port = int(fields[1])
            break
    if port is None:
        raise RuntimeError("no free high-speed vhci port")
    write(VHCI / "attach", f"{port} {sock.fileno()} {info.devid} {info.speed}")
    return port, sock


def export_steps() -> None:
    """The native export core under the guest kernel's vhci-hcd (ADR 0027)."""
    result = run("modprobe", "vhci-hcd")
    if result.returncode:
        emit("export-vhci", False, error=result.stderr.strip())
        return
    disk = Path("/tmp/export.img")
    disk.write_bytes(random.Random(5).randbytes(DISK_SIZE))
    log = open("/tmp/exporter.log", "w")  # noqa: SIM115
    exporter = subprocess.Popen(
        [str(EXPORTER), "--disk", str(disk), "--port", "3241"],
        stdout=subprocess.PIPE,
        stderr=log,
        text=True,
    )
    assert exporter.stdout is not None
    port_number = int(exporter.stdout.readline())
    port, sock = vhci_import(port_number, "1-1")
    sock.close()  # vhci holds its own reference, as usbip attach does
    busid = wait(lambda: devices_with_ids(VID, "5eef")[:1], 20)
    device = wait(lambda: block_device(str(busid[0])) if busid else None, 30)  # type: ignore[index]
    if not busid or not device:
        emit("export-vhci-io", False, error="vhci device or block device missing")
        exporter.kill()
        return
    device = str(device)
    drop_caches()
    expected = disk.read_bytes()[: 8 * MIB]
    data = read_block(device, 0, 8 * MIB)
    payload = random.Random(8).randbytes(8 * MIB)
    write_block(device, 16 * MIB, payload)
    landed = disk.read_bytes()[16 * MIB : 24 * MIB]
    drop_caches()
    readback = read_block(device, 16 * MIB, 8 * MIB)
    speed = (DEVICES / str(busid[0]) / "speed").read_text().strip()  # type: ignore[index]
    emit(
        "export-vhci-io",
        data == expected and landed == payload and readback == payload,
        device=device,
        speed=speed,
        read_matches_server_disk=data == expected,
        write_landed_in_server_disk=landed == payload,
        readback_match=readback == payload,
    )
    perf(device, "export-vhci-perf", exporter.pid)
    write(VHCI / "detach", str(port))
    gone = wait(lambda: not devices_with_ids(VID, "5eef"), 15)
    exporter.send_signal(signal.SIGTERM)
    code = exporter.wait(10)
    text = Path("/tmp/exporter.log").read_text()
    emit(
        "export-vhci-detach",
        bool(gone) and code == 0 and "Sanitizer" not in text,
        device_gone=bool(gone),
        exit_code=code,
        log_tail=text.splitlines()[-4:],
    )


def main() -> int:
    emit("boot", True, kernel=os.uname().release)
    for module in (
        "dummy_hcd num=2",
        "raw_gadget",
        "libcomposite",
        "usb_f_mass_storage",
        "usbip-host",
        "usb-storage",
    ):
        result = run("modprobe", *module.split())
        if result.returncode:
            emit("modprobe", False, module=module, error=result.stderr.strip())
            return 1
    udcs = sorted(p.name for p in UDC.iterdir())
    emit(
        "udcs",
        "dummy_udc.0" in udcs and "dummy_udc.1" in udcs,
        udcs=udcs,
        gadget_driver=(UDC / "dummy_udc.0/device/driver").resolve().name,
    )
    make_source_gadget()
    source = wait(lambda: devices_with_ids()[:1], 20)
    if not source:
        emit("source", False, error="source gadget did not enumerate")
        return 1
    busid = str(source[0])  # type: ignore[index]
    direct = wait(lambda: block_device(busid), 20)
    baseline = timed_read(str(direct)) if direct else None
    emit("source", True, busid=busid, direct_read_mb_s=baseline)
    if direct:
        perf(str(direct), "source-perf", None)
    usbipd = start_usbipd(busid)
    status_file = DEVICES / busid / "usbip_status"
    emit("export", status_file.read_text().strip() == "1")

    for kind in ("python", "native"):
        try:
            usbipd = agent_steps(kind, busid, usbipd)
        except Exception as exc:  # report with the worker's own account, go on
            log = Path(f"/tmp/worker{'-native' if kind == 'native' else ''}.log")
            emit(
                f"{kind}-scenario",
                False,
                error=repr(exc),
                worker_log=log.read_text().splitlines()[-20:] if log.exists() else [],
            )
            run("pkill", "-KILL", "-f", "yoke.usb.worker|yoke-gadget-worker")
            wait(lambda: proxied(busid)() is None, 15)
    native_sweep(busid)

    # Negative control: the Python worker with fcntl.ioctl cannot stop once
    # its threads are blocked inside Raw Gadget ioctls.
    control = start_worker(busid, fcntl_control=True)
    ready = wait(lambda: block_device(str(proxied(busid)() or "none")), 30)
    time.sleep(1.0)  # let enumeration traffic settle: threads now block
    begun = time.monotonic()
    control.send_signal(signal.SIGTERM)
    try:
        code = control.wait(30)
    except subprocess.TimeoutExpired:
        control.kill()
        code = None
    emit(
        "control-fcntl-cannot-cancel",
        bool(ready) and code != 0,
        presented=bool(ready),
        exit_code=code,
        seconds=round(time.monotonic() - begun, 2),
        note="expected: teardown unconfirmed with fcntl.ioctl (PEP 475 retry)",
    )
    usbipd.send_signal(signal.SIGTERM)

    export_steps()

    dmesg = run("dmesg").stdout.splitlines()
    warnings = [
        line for line in dmesg if "WARNING" in line or "BUG" in line or "BOGUS" in line
    ]
    emit(
        "dmesg",
        not warnings,
        warnings=warnings[:10],
        usbip=[line for line in dmesg if "usbip" in line or "vhci" in line][-8:],
    )
    for name in ("worker", "worker-native", "worker-fcntl", "usbipd", "exporter"):
        path = Path(f"/tmp/{name}.log")
        if path.exists():
            emit(f"log-{name}", True, tail=path.read_text().splitlines()[-8:])
    emit("done", True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # report, never hang PID 1
        emit("guest", False, error=repr(exc))
        raise SystemExit(1) from exc
