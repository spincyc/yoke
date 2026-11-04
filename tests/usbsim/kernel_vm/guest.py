"""Real-kernel loopback scenario, run as root inside the test VM (launch.py).

Every step prints one ``YOKE-VM {json}`` line with ``ok`` true/false.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import signal
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


def devices_with_ids() -> list[str]:
    found = []
    for device in DEVICES.iterdir():
        try:
            if (device / "idVendor").read_text().strip() == VID and (
                device / "idProduct"
            ).read_text().strip() == PID:
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
    busid: str, *, fcntl_control: bool = False, agent: bool = False
) -> subprocess.Popen[str]:
    args = ["-m", "yoke.usb.worker", "--udc", "dummy_udc.0"]
    if agent:
        args += ["--socket", SOCKET]
    else:
        args += ["--target", f"127.0.0.1:3240/{busid}", "--source", "127.0.0.1"]
    if fcntl_control:
        args = [str(HERE / "fcntl_worker.py"), *args[2:]]
    log = open(f"/tmp/worker{'-fcntl' if fcntl_control else ''}.log", "a")  # noqa: SIM115
    return subprocess.Popen(
        [PYTHON, *args], stdout=log, stderr=subprocess.STDOUT, text=True
    )


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


def main() -> int:
    from yoke.drivers.gadget import UsbGadgetDriver
    from yoke.usb.control import ControlClient

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
    usbipd = start_usbipd(busid)
    status_file = DEVICES / busid / "usbip_status"
    emit("export", status_file.read_text().strip() == "1")

    # 1. Agent path: route -> present -> enumerate -> checksummed I/O.
    worker = start_worker(busid, agent=True)
    client = ControlClient(SOCKET, timeout=10)
    wait(lambda: Path(SOCKET).exists(), 20)
    driver = UsbGadgetDriver(udc="dummy_udc.0", socket_path=SOCKET)
    driver.attach("prod-vm", {"host": "127.0.0.1", "port": 3240, "busids": [busid]})
    verify_io(busid, "io")
    poll = driver.poll()
    status = client.status()
    emit(
        "driver-active",
        poll.state.value == "active",
        detail=poll.detail,
        worker_state=status.state,
        link_speed=status.link_speed,
        counters={k: v for k, v in status.counters.items() if v},
    )
    # 2. Detach through the driver: confirmed teardown (interrupts blocked
    #    EVENT_FETCH/EP_READ) and the export is released (FIN reaches stub).
    begun = time.monotonic()
    driver.detach()
    seconds = round(time.monotonic() - begun, 3)
    gone = wait(lambda: proxied(busid)() is None, 15)
    released = wait(lambda: status_file.read_text().strip() == "1", 15)
    emit(
        "detach",
        bool(gone and released) and driver.poll().state.value == "idle",
        seconds=seconds,
        device_gone=bool(gone),
        export_available=bool(released),
    )
    # 3. Remote loss and a usbipd restart while presenting.
    driver.attach("prod-vm", {"host": "127.0.0.1", "port": 3240, "busids": [busid]})
    verify_io(busid, "io-again")
    usbipd.send_signal(signal.SIGTERM)
    usbipd.wait(10)
    begun = time.monotonic()
    write(DEVICES / busid / "usbip_sockfd", "-1")  # the stub drops the session
    lost = wait(lambda: proxied(busid)() is None, 20)
    emit(
        "remote-loss-presented", bool(lost), seconds=round(time.monotonic() - begun, 2)
    )
    wait(lambda: status_file.read_text().strip() == "1", 20)
    usbipd = start_usbipd(busid)
    restarted = time.monotonic()
    back = wait(lambda: block_device(str(proxied(busid)() or "none")), 30)
    emit(
        "reconnect-after-usbipd-restart",
        bool(back),
        seconds=round(time.monotonic() - restarted, 2),
        reconnects=client.status().reconnects,
    )
    verify_io(busid, "io-after-reconnect")
    driver.detach()
    stop_worker(worker, "worker-sigterm")
    wait(lambda: status_file.read_text().strip() == "1", 15)

    # 4. Negative control: the same worker with fcntl.ioctl cannot stop once
    #    its threads are blocked inside Raw Gadget ioctls.
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

    dmesg = run("dmesg").stdout.splitlines()
    warnings = [
        line for line in dmesg if "WARNING" in line or "BUG" in line or "BOGUS" in line
    ]
    emit(
        "dmesg",
        not warnings,
        warnings=warnings[:10],
        usbip=[line for line in dmesg if "usbip" in line][-6:],
    )
    for name in ("worker", "worker-fcntl", "usbipd"):
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
