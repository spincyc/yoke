"""Build and drive the host IR endpoint simulation over real localhost TCP.

The harness compiles firmware/esp32/components/yoke_ir unchanged. Its clock
is virtual and advanced only by tests, so timing assertions are exact and
deterministic. HOST SIMULATION ONLY: no RMT, GPIO, LED or Ethernet hardware.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMPONENT = ROOT / "firmware/esp32/components/yoke_ir"
HERE = Path(__file__).resolve().parent
SOURCES = ("ir_core.c", "ir_program.c", "ir_encode.c")
WARNINGS = ["-Wall", "-Wextra", "-Werror", "-Wshadow", "-Wno-sign-conversion"]


def build(target: Path, main: str, *extra: str) -> Path:
    compiler = shutil.which("cc")
    if compiler is None:
        pytest.skip("host C compiler unavailable; target build is a separate check")
    subprocess.run(
        [
            compiler,
            "-std=gnu17",
            "-O2",
            *WARNINGS,
            *extra,
            "-I",
            str(COMPONENT / "include"),
            str(HERE / main),
            *(str(COMPONENT / source) for source in SOURCES),
            "-o",
            str(target),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return target


@pytest.fixture(scope="session")
def ir_server_binary(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("firmware-ir")
    return build(out / "host-ir-server", "host_ir_server.c")


@pytest.fixture(scope="session")
def ir_bench_binary(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("firmware-ir-bench")
    return build(out / "host-ir-bench", "host_ir_bench.c")


class Client:
    """A raw iTach-style TCP client: CR-terminated lines both ways."""

    def __init__(self, port: int, rcvbuf: int | None = None) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if rcvbuf is not None:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, rcvbuf)
        self.sock.settimeout(3)
        self.sock.connect(("127.0.0.1", port))
        self.buffer = b""

    def send(self, line: str, end: bytes = b"\r") -> None:
        self.sock.sendall(line.encode("latin-1") + end)

    def send_raw(self, data: bytes) -> None:
        self.sock.sendall(data)

    def lines(self, count: int, timeout: float = 3.0) -> list[str]:
        self.sock.settimeout(timeout)
        while self.buffer.count(b"\r") < count:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("endpoint closed the connection")
            self.buffer += chunk
        parts = self.buffer.split(b"\r")
        taken, self.buffer = parts[:count], b"\r".join(parts[count:])
        return [part.decode("latin-1") for part in taken]

    def line(self) -> str:
        return self.lines(1)[0]

    def pending(self, wait: float = 0.05) -> bytes:
        """Bytes already sent by the endpoint, without blocking for more."""
        self.sock.settimeout(wait)
        try:
            while True:
                chunk = self.sock.recv(65536)
                if not chunk:
                    break
                self.buffer += chunk
        except (TimeoutError, BlockingIOError):
            pass
        data, self.buffer = self.buffer, b""
        return data

    def closed_by_peer(self, wait: float = 2.0) -> bool:
        self.sock.settimeout(wait)
        try:
            while True:
                chunk = self.sock.recv(65536)
                if not chunk:
                    return True
        except ConnectionResetError:
            return True
        except TimeoutError:
            return False

    def close(self) -> None:
        self.sock.close()


class Endpoint:
    def __init__(self, binary: Path, *options: str) -> None:
        self.process = subprocess.Popen(
            [str(binary), *options],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        assert self.process.stdout is not None
        self.port = int(self.process.stdout.readline())
        self.clients: list[Client] = []

    def control(self, command: str) -> str:
        assert self.process.stdin is not None and self.process.stdout is not None
        self.process.stdin.write(command + "\n")
        self.process.stdin.flush()
        return self.process.stdout.readline().strip()

    def advance(self, microseconds: int) -> int:
        reply = self.control(f"advance {microseconds}")
        assert reply.startswith("ok "), reply
        return int(reply.split()[1])

    def now(self) -> int:
        return int(self.control("now"))

    def sync(self) -> None:
        """Every byte written by any client so far has been processed."""
        self.now()

    def waveform(self, output: int = 0) -> dict[str, Any]:
        return json.loads(self.control(f"waveform {output}"))  # type: ignore[no-any-return]

    def counters(self) -> dict[str, Any]:
        return json.loads(self.control("counters"))  # type: ignore[no-any-return]

    def connect(self, rcvbuf: int | None = None) -> Client:
        client = Client(self.port, rcvbuf)
        self.clients.append(client)
        self.sync()
        return client

    def stop(self) -> None:
        for client in self.clients:
            client.close()
        if self.process.poll() is None:
            self.control("quit")
        self.process.wait(timeout=5)


@pytest.fixture
def endpoint_factory(ir_server_binary: Path) -> Iterator[Any]:
    started: list[Endpoint] = []

    def start(*options: str) -> Endpoint:
        endpoint = Endpoint(ir_server_binary, *options)
        started.append(endpoint)
        return endpoint

    yield start
    for endpoint in started:
        endpoint.stop()


@pytest.fixture
def endpoint(endpoint_factory: Any) -> Endpoint:
    return endpoint_factory("--outputs", "3")  # type: ignore[no-any-return]
