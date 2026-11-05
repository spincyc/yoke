"""Build and load the real YX encoder (native/yusb/src/yx.c) through ctypes.

The shared library is compiled on first use into .scratch/cache/usbtrace,
named by a hash of its sources, headers and flags:

    cc -std=c11 -O2 -shared -fPIC -Wl,-z,defs -I native/yusb/include \
       -I native/third_party/lz4 native/yusb/src/yx.c \
       native/third_party/lz4/lz4.c tools/usbtrace/yxshim.c -o libyxshim-<hash>.so

If yx.c needs other portable-core files, the build retries with every
native/yusb/src/*.c. Stdlib only, Python >= 3.9.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import shutil
import subprocess
import time
from pathlib import Path
from types import TracebackType

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
NATIVE = ROOT / "native"
CACHE = ROOT / ".scratch" / "cache" / "usbtrace"
FLAGS = ("-std=c11", "-O2", "-shared", "-fPIC", "-Wl,-z,defs")
FILL, LZ4, CACHE_LEVER, READAHEAD = 0x01, 0x02, 0x04, 0x08  # yusb_yx.h
KINDS = ("raw", "fill", "lz4", "ref")


class EncoderUnavailableError(RuntimeError):
    """The native sources or a compiler are missing, or the build failed."""


def compiler() -> str:
    for name in (os.environ.get("CC", ""), "cc", "gcc", "clang"):
        if name and shutil.which(name):
            return name
    raise EncoderUnavailableError("no C compiler (set CC)")


def _inputs(native: Path) -> tuple[list[Path], list[Path], list[Path]]:
    """(minimal sources, whole-core sources, headers); raises when absent."""
    yx = native / "yusb" / "src" / "yx.c"
    lz4 = native / "third_party" / "lz4" / "lz4.c"
    missing = [str(p) for p in (yx, lz4) if not p.exists()]
    if missing:
        raise EncoderUnavailableError("missing native sources: " + ", ".join(missing))
    shim = HERE / "yxshim.c"
    core = sorted((native / "yusb" / "src").glob("*.c"))
    headers = sorted((native / "yusb" / "include").glob("*.h"))
    headers += sorted((native / "yusb" / "src").glob("*.h"))
    headers += sorted((native / "third_party" / "lz4").glob("*.h"))
    return [yx, lz4, shim], [*core, lz4, shim], headers


def build(native: Path = NATIVE, cache: Path = CACHE) -> Path:
    """Compile (once per content hash) and return the shared library path."""
    minimal, whole, headers = _inputs(native)
    cc = compiler()
    include = [
        "-I",
        str(native / "yusb" / "include"),
        "-I",
        str(native / "third_party" / "lz4"),
    ]
    digest = hashlib.sha256(" ".join((cc, *FLAGS)).encode())
    for path in sorted(set(whole)) + headers:
        digest.update(path.name.encode() + b"\0" + path.read_bytes())
    target = cache / f"libyxshim-{digest.hexdigest()[:12]}.so"
    if target.exists():
        return target
    cache.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(f".{os.getpid()}.tmp")
    errors = []
    for sources in (minimal, whole):
        command = [
            cc,
            *FLAGS,
            *include,
            *(str(s) for s in sources),
            "-o",
            str(temporary),
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode == 0:
            os.replace(temporary, target)
            for stale in cache.glob("libyxshim-*.so"):  # older sources; safe if mapped
                if stale != target:
                    stale.unlink(missing_ok=True)
            return target
        errors.append(result.stderr.strip())
    raise EncoderUnavailableError(
        "building the YX shim failed:\n" + "\n---\n".join(errors)
    )


class Library:
    """The loaded shim with its ctypes signatures."""

    def __init__(self, path: Path) -> None:
        self.path = path
        lib = ctypes.CDLL(str(path))
        size, void_p, err_p = (
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
        )
        lib.shim_enc_new.restype = void_p
        lib.shim_enc_new.argtypes = [ctypes.c_uint, ctypes.c_uint, err_p]
        lib.shim_enc_free.argtypes = [void_p]
        lib.shim_bound.restype = size
        lib.shim_bound.argtypes = [size]
        lib.shim_encode.restype = size
        lib.shim_encode.argtypes = [void_p, ctypes.c_char_p, size, void_p]
        lib.shim_enc_stats.argtypes = [void_p, ctypes.POINTER(ctypes.c_uint64)]
        lib.shim_dec_new.restype = void_p
        lib.shim_dec_new.argtypes = [ctypes.c_uint, ctypes.c_uint, err_p]
        lib.shim_dec_free.argtypes = [void_p]
        lib.shim_decode.restype = ctypes.c_int
        lib.shim_decode.argtypes = [
            void_p,
            ctypes.c_char_p,
            size,
            void_p,
            size,
            ctypes.POINTER(size),
        ]
        self.lib = lib

    def bound(self, length: int) -> int:
        return int(self.lib.shim_bound(length))


def load(native: Path = NATIVE, cache: Path = CACHE) -> Library:
    path = build(native, cache)
    try:
        return Library(path)
    except OSError as exc:
        raise EncoderUnavailableError(f"loading {path} failed: {exc}") from exc


class _Handle:
    _free_name = ""

    def __init__(self, library: Library, handle: int) -> None:
        self.library = library
        self.handle: int | None = handle
        self._out = ctypes.create_string_buffer(0)

    def _buffer(self, size: int) -> ctypes.Array[ctypes.c_char]:
        if len(self._out) < size:
            self._out = ctypes.create_string_buffer(size)
        return self._out

    def close(self) -> None:
        if self.handle is not None:
            getattr(self.library.lib, self._free_name)(self.handle)
            self.handle = None

    def __enter__(self) -> _Handle:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def _new(library: Library, name: str, levers: int, cache_blocks: int) -> int:
    err = ctypes.c_int(0)
    handle = getattr(library.lib, name)(levers, cache_blocks, ctypes.byref(err))
    if not handle:
        raise ValueError(
            f"{name}(levers={levers:#x}, cache={cache_blocks}): error {err.value}"
        )
    return int(handle)


class Encoder(_Handle):
    """One sender direction; ``elapsed_ns`` sums the time spent in C."""

    _free_name = "shim_enc_free"

    def __init__(self, library: Library, levers: int, cache_blocks: int = 0) -> None:
        super().__init__(library, _new(library, "shim_enc_new", levers, cache_blocks))
        self.elapsed_ns = 0

    def encode(self, data: bytes) -> bytes:
        out = self._buffer(self.library.bound(len(data)))
        start = time.perf_counter_ns()
        size = self.library.lib.shim_encode(self.handle, data, len(data), out)
        self.elapsed_ns += time.perf_counter_ns() - start
        return ctypes.string_at(out, size)

    def stats(self) -> dict[str, int]:
        values = (ctypes.c_uint64 * 8)()
        self.library.lib.shim_enc_stats(self.handle, values)
        result = {f"blocks_{kind}": int(values[i]) for i, kind in enumerate(KINDS)}
        names = ("raw_bytes", "wire_bytes", "lz4_tries", "lz4_bypass")
        result.update({name: int(values[4 + i]) for i, name in enumerate(names)})
        return result


class Decoder(_Handle):
    _free_name = "shim_dec_free"

    def __init__(self, library: Library, levers: int, cache_blocks: int = 0) -> None:
        super().__init__(library, _new(library, "shim_dec_new", levers, cache_blocks))

    def decode(self, wire: bytes, length: int) -> bytes:
        out = self._buffer(length)
        consumed = ctypes.c_size_t(0)
        rc = self.library.lib.shim_decode(
            self.handle, wire, len(wire), out, length, ctypes.byref(consumed)
        )
        if rc != 0 or consumed.value != len(wire):
            raise ValueError(
                f"YX decode failed: rc={rc}, consumed {consumed.value}/{len(wire)}"
            )
        return ctypes.string_at(out, length)
