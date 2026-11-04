"""Opt-in real-kernel loopback test of the gadget worker (``make verify-usb-kernel``).

Boots the host's own kernel under QEMU/KVM (virtme-style): the host root is
shared read-only over virtio-9p, a tiny busybox initramfs mounts it, and
``guest.sh`` runs ``guest.py`` as root inside the VM. There, real kernel
drivers carry every byte:

    configfs mass-storage gadget on dummy_udc.1 -> dummy_hcd.1 host
      -> usbip-host stub (kernel) -> TCP 127.0.0.1:3240
      -> yoke.usb.worker (production code) -> /dev/raw-gadget on dummy_udc.0
      -> dummy_hcd.0 host -> usb-storage -> /dev/sdX (checksummed I/O)

Skips cleanly (exit 0, "SKIP: reason") without Linux/KVM/QEMU, a matching
kernel image and modules, or a static busybox. Exits 1 when the guest
reports a failure or does not finish. No root is needed on the host.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKTREE = HERE.parents[2]
MARKER = "YOKE-VM "
NINEP = ("netfs", "9pnet", "9pnet_virtio", "9p")
GUEST_MODULES = (
    "kernel/drivers/usb/gadget/udc/dummy_hcd",
    "kernel/drivers/usb/gadget/legacy/raw_gadget",
    "kernel/drivers/usb/gadget/libcomposite",
    "kernel/drivers/usb/gadget/function/usb_f_mass_storage",
    "kernel/drivers/usb/usbip/usbip-host",
    "kernel/drivers/usb/storage/usb-storage",
)
BUSYBOX = (Path("/usr/lib/initcpio/busybox"), Path("/usr/bin/busybox"))
TOOLS = (Path("/usr/bin/mount"), Path("/usr/bin/kmod"), Path("/usr/bin/switch_root"))


def preflight(kernel: Path, release: str) -> str | None:
    """Return a skip reason, or None when the VM test can run."""
    if sys.platform != "linux":
        return "not Linux"
    if not os.access("/dev/kvm", os.R_OK | os.W_OK):
        return "/dev/kvm is not accessible"
    if shutil.which("qemu-system-x86_64") is None:
        return "qemu-system-x86_64 not installed"
    if not kernel.is_file() or not os.access(kernel, os.R_OK):
        return f"kernel image {kernel} not readable"
    modules = Path("/lib/modules") / release
    if not (modules / "modules.dep").is_file():
        return f"no modules for {release}"
    dep = (modules / "modules.dep").read_text()
    for name in GUEST_MODULES + tuple(
        f"kernel/{p}" for p in ("fs/netfs/netfs", "net/9p/9pnet", "fs/9p/9p")
    ):
        if f"{name}.ko" not in dep:
            return f"kernel module {name} missing"
    if busybox() is None:
        return "no busybox (mkinitcpio-busybox)"
    for tool in TOOLS:
        if not os.access(tool, os.X_OK):
            return f"{tool} missing"
    if not (WORKTREE / ".scratch/venv/bin/python").exists():
        return "project venv missing (run make venv)"
    return None


def busybox() -> Path | None:
    for path in BUSYBOX:
        if path.is_file() and os.access(path, os.X_OK):
            return path
    return None


def shared_libraries(binary: Path) -> list[Path]:
    """The loader and libraries ``ldd`` reports (none for a static binary)."""
    result = subprocess.run(
        ["ldd", str(binary)], capture_output=True, text=True, check=False
    )
    libraries = []
    for line in result.stdout.splitlines():
        for token in line.split():
            if token.startswith("/") and Path(token).is_file():
                libraries.append(Path(token))
    return libraries


def module_path(modules: Path, name: str) -> Path:
    for line in (modules / "modules.dep").read_text().splitlines():
        path = line.split(":", 1)[0]
        if Path(path).name.split(".ko")[0] == name:
            return modules / path
    raise FileNotFoundError(name)


def decompress(path: Path) -> bytes:
    raw = path.read_bytes()
    if path.suffix == ".zst":
        try:
            from compression import zstd  # Python 3.14+

            return zstd.decompress(raw)
        except ImportError:
            result = subprocess.run(
                ["zstd", "-dc", str(path)], capture_output=True, check=True
            )
            return result.stdout
    if path.suffix == ".xz":
        import lzma

        return lzma.decompress(raw)
    if path.suffix == ".gz":
        import gzip

        return gzip.decompress(raw)
    return raw


def newc(entries: list[tuple[str, int, bytes]]) -> bytes:
    """A cpio "newc" archive; entries are (name, mode, data)."""
    out = bytearray()
    for ino, (name, mode, data) in enumerate([*entries, ("TRAILER!!!", 0, b"")], 1):
        encoded = name.encode() + b"\0"
        fields = [ino, mode, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(encoded), 0]
        out += b"070701" + b"".join(f"{f:08x}".encode() for f in fields) + encoded
        out += b"\0" * (-len(out) % 4) + data
        out += b"\0" * (-len(out) % 4)
    return bytes(out)


INIT = """#!/bin/busybox sh
B=/bin/busybox
$B mkdir -p /proc /sys /dev /mnt/root
M=/usr/bin/mount
$M -t proc proc /proc
$M -t sysfs sysfs /sys
$M -t devtmpfs devtmpfs /dev
for m in {modules}; do
  /usr/bin/insmod /lib/ninep/$m.ko ||
    echo "{marker}{{\\"step\\":\\"insmod $m\\",\\"ok\\":false}}"
done
$M -t 9p -o trans=virtio,version=9p2000.L,ro,msize=262144,cache=loose \\
  hostroot /mnt/root
$M --move /dev /mnt/root/dev
$B umount /proc /sys
exec /usr/bin/switch_root /mnt/root /bin/sh {guest}
"""


def build_initramfs(out: Path, release: str) -> Path:
    modules = Path("/lib/modules") / release
    box = busybox()
    assert box is not None
    script = INIT.format(
        modules=" ".join(NINEP), marker=MARKER, guest=HERE / "guest.sh"
    ).encode()
    entries: list[tuple[str, int, bytes]] = [
        ("bin", stat.S_IFDIR | 0o755, b""),
        ("dev", stat.S_IFDIR | 0o755, b""),
        ("proc", stat.S_IFDIR | 0o755, b""),
        ("sys", stat.S_IFDIR | 0o755, b""),
        ("lib", stat.S_IFDIR | 0o755, b""),
        ("lib/ninep", stat.S_IFDIR | 0o755, b""),
        ("mnt", stat.S_IFDIR | 0o755, b""),
        ("mnt/root", stat.S_IFDIR | 0o755, b""),
        ("bin/busybox", stat.S_IFREG | 0o755, box.read_bytes()),
        ("init", stat.S_IFREG | 0o755, script),
    ]
    made = {"bin", "dev", "proc", "sys", "lib", "lib/ninep", "mnt", "mnt/root"}
    files = {
        library for binary in (box, *TOOLS) for library in shared_libraries(binary)
    }
    for library in sorted(files | set(TOOLS)):
        parts = library.parts[1:]
        for depth in range(1, len(parts)):
            directory = "/".join(parts[:depth])
            if directory not in made:
                made.add(directory)
                entries.append((directory, stat.S_IFDIR | 0o755, b""))
        entries.append(
            ("/".join(parts), stat.S_IFREG | 0o755, library.resolve().read_bytes())
        )
    entries.append(("usr/bin/insmod", stat.S_IFLNK | 0o777, b"kmod"))
    for name in NINEP:
        entries.append(
            (
                f"lib/ninep/{name}.ko",
                stat.S_IFREG | 0o644,
                decompress(module_path(modules, name)),
            )
        )
    out.mkdir(parents=True, exist_ok=True)
    image = out / "initramfs.cpio"
    image.write_bytes(newc(entries))
    return image


def run_vm(
    kernel: Path, initramfs: Path, timeout: float, log: Path
) -> list[dict[str, object]]:
    command = [
        "qemu-system-x86_64",
        "-enable-kvm",
        "-cpu",
        "host",
        "-m",
        "1536",
        "-smp",
        "2",
        "-nographic",
        "-no-reboot",
        "-kernel",
        str(kernel),
        "-initrd",
        str(initramfs),
        "-append",
        f"console=ttyS0 panic=-1 loglevel=4 yoke.worktree={WORKTREE}",
        "-virtfs",
        "local,path=/,mount_tag=hostroot,security_model=none,readonly=on",
    ]
    started = time.monotonic()
    with log.open("w") as sink:
        try:
            subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=sink,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            timed_out = {"step": "vm", "ok": False, "error": "timeout"}
            sink.write(f"\n{MARKER}{json.dumps(timed_out)}\n")
    events = []
    for line in log.read_text(errors="replace").splitlines():
        if MARKER in line:
            try:
                events.append(json.loads(line.split(MARKER, 1)[1]))
            except ValueError:
                continue
    events.append(
        {
            "step": "vm-wall-time",
            "ok": True,
            "seconds": round(time.monotonic() - started, 1),
        }
    )
    return events


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--kernel", type=Path, default=Path("/boot/vmlinuz-linux"))
    parser.add_argument("--release", default=os.uname().release)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--out", type=Path, default=WORKTREE / ".scratch/kernel-vm")
    args = parser.parse_args(argv)
    reason = preflight(args.kernel, args.release)
    if reason is not None:
        print(f"SKIP: {reason}")
        return 0
    initramfs = build_initramfs(args.out, args.release)
    events = run_vm(args.kernel, initramfs, args.timeout, args.out / "console.log")
    for event in events:
        print(json.dumps(event, sort_keys=True))
    finished = any(e.get("step") == "done" for e in events)
    failed = [e for e in events if e.get("ok") is False]
    print(
        f"RESULT: {'PASS' if finished and not failed else 'FAIL'} "
        f"({len(failed)} failed steps; console log {args.out / 'console.log'})"
    )
    return 0 if finished and not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
