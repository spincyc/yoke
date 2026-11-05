"""Interop over real sockets: native nodes with the Python agent and CLI.

Four endpoints share one multicast group on the loopback interface: a
Python producer agent and a Python consumer agent (`yoke agent`, sim
driver) and a native producer and a native consumer (tests/native_ywp/c/
host_node.c: the C core behind a small POSIX shell, ASan and UBSan). The
real `yoke pair`, `yoke unpair` and `yoke status` CLI pair across
implementations, C to C included, and a native consumer restored from its
durable state re-attaches. HOST ONLY: loopback multicast, simulated USB;
skipped where the sandbox forbids multicast.
"""

from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.native_ywp.conftest import C_DIR, build_exe
from yoke.net.mcast import open_multicast_socket

GROUP = "239.121.111.211"
PORT = 30000 + os.getpid() % 20000
IFACE = "127.0.0.1"
MEDIA = {"host": "127.0.0.1", "port": 3240, "busids": ["1-1"]}
YOKE = [sys.executable, "-c", "import sys; from yoke.cli import main; sys.exit(main())"]


class Endpoint:
    """A running endpoint process with its stdout lines in a queue."""

    def __init__(self, args: list[str], name: str) -> None:
        self.name = name
        self.process = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        self.lines: queue.Queue[str] = queue.Queue()
        self.last: dict[str, Any] = {}
        assert self.process.stdout is not None
        stream = self.process.stdout
        threading.Thread(
            target=lambda: [self.lines.put(x) for x in stream], daemon=True
        ).start()

    def wait_for(
        self, check: Callable[[dict[str, Any]], bool], timeout: float = 15.0
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while not check(self.last):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or self.process.poll() is not None:
                raise AssertionError(f"{self.name}: last status {self.last}")
            try:
                line = self.lines.get(timeout=remaining)
            except queue.Empty:
                continue
            if line.startswith('{"state"'):
                self.last = json.loads(line)
        return self.last

    def stop(self) -> str:
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
        try:
            _, err = self.process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            _, err = self.process.communicate()
        return err


@pytest.fixture
def lab(tmp_path: Path, native_dir: Path) -> Iterator[dict[str, Any]]:
    try:
        open_multicast_socket(GROUP, PORT, interface=IFACE).close()
    except OSError as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"multicast unavailable: {exc}")
    binary = build_exe(
        native_dir / "host_node", C_DIR / "host_node.c", flags=("-D_DEFAULT_SOURCE",)
    )
    psk = tmp_path / "psk"
    psk.write_text("5" * 64 + "\n")
    psk.chmod(0o600)
    endpoints: list[Endpoint] = []

    def python_agent(node_id: str, role: str) -> Endpoint:
        config = tmp_path / f"{node_id}.toml"
        media = (
            'media = { host = "127.0.0.1", port = 3240, busids = ["1-1"] }'
            if role == "producer"
            else ""
        )
        config.write_text(
            f'node_id = "{node_id}"\nrole = "{role}"\nkind = "usb"\n'
            f'psk_file = "{psk}"\nstate_file = "{tmp_path / node_id}.json"\n'
            f'group = "{GROUP}"\nport = {PORT}\ninterface = "{IFACE}"\n'
            f'[driver]\ntype = "sim"\n{media}\n'
        )
        endpoint = Endpoint(
            [*YOKE, "agent", "--config", str(config), "--commission"], node_id
        )
        endpoints.append(endpoint)
        return endpoint

    def native(node_id: str, role: str, *, commission: bool = True) -> Endpoint:
        args = [
            str(binary),
            "--id",
            node_id,
            "--role",
            role,
            "--kind",
            "usb",
            "--group",
            GROUP,
            "--port",
            str(PORT),
            "--interface",
            IFACE,
            "--psk-file",
            str(psk),
            "--state",
            str(tmp_path / f"{node_id}.ywp"),
        ]
        if commission:
            args.append("--commission")
        if role == "producer":
            args += ["--media", json.dumps(MEDIA)]
        endpoint = Endpoint(args, node_id)
        endpoints.append(endpoint)
        return endpoint

    def cli(*args: str) -> dict[str, Any]:
        result = subprocess.run(
            [
                *YOKE,
                "--group",
                GROUP,
                "--port",
                str(PORT),
                "--interface",
                IFACE,
                "--psk-file",
                str(psk),
                "--origin",
                "cli-test",
                *args,
                "--json",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        parsed: dict[str, Any] = json.loads(result.stdout)
        return parsed

    yield {"python_agent": python_agent, "native": native, "cli": cli}
    for endpoint in endpoints:
        err = endpoint.stop()
        assert "ERROR: AddressSanitizer" not in err and "runtime error" not in err, err


def _status(cli: Callable[..., dict[str, Any]], window: str = "3") -> dict[str, Any]:
    return cli("status", "--watch", window)


def test_native_and_python_endpoints_pair_over_multicast(lab: dict[str, Any]) -> None:
    prod_py = lab["python_agent"]("prod-py", "producer")
    con_py = lab["python_agent"]("con-py", "consumer")
    prod_c = lab["native"]("prod-c", "producer")
    con_c = lab["native"]("con-c", "consumer")
    prod_c.wait_for(lambda s: s.get("state") == "active")
    con_c.wait_for(lambda s: s.get("state") == "idle")
    time.sleep(2.5)  # one announce interval: everyone has heard everyone

    # Python CLI pairs a native consumer to a Python producer ...
    result = lab["cli"]("pair", "prod-py", "con-c")
    assert result["committed"] is True, result
    con_c.wait_for(
        lambda s: (
            s.get("state") == "active"
            and s["producer"] == "prod-py"
            and s["attached"] == "prod-py"
        )
    )
    # ... and a Python consumer to a native producer.
    lab["cli"]("pair", "prod-c", "con-py")
    # C to C, the MCU-only case, replacing the existing route.
    lab["cli"]("pair", "prod-c", "con-c")
    state = con_c.wait_for(
        lambda s: (
            s.get("state") == "active"
            and s["producer"] == "prod-c"
            and s["attached"] == "prod-c"
        )
    )
    version = state["version"]

    status = _status(lab["cli"])
    counts = status["counts"]
    assert status["authentication"] == "site-psk" and counts["ok"] > 0, counts
    assert not any(
        counts[k] for k in ("bad-sig", "malformed", "version", "oversize")
    ), counts
    nodes = {node["id"]: node for node in status["nodes"]}
    assert {"prod-py", "con-py", "prod-c", "con-c"} <= set(nodes), status
    assert nodes["prod-c"]["media"] == MEDIA and not nodes["prod-c"]["stale"]
    assert nodes["con-py"]["pairing"]["producer"] == "prod-c"
    assert nodes["con-py"]["state"] == "active"
    assert nodes["con-c"]["pairing"] == {"producer": "prod-c", "version": version}

    # A native consumer restarts from its durable state and re-attaches.
    con_c.stop()
    con_c = lab["native"]("con-c", "consumer", commission=False)
    con_c.wait_for(
        lambda s: (
            s.get("state") == "active"
            and s["producer"] == "prod-c"
            and s["version"] == version
        )
    )

    lab["cli"]("unpair", "con-c")
    con_c.wait_for(lambda s: s.get("state") == "idle" and s["producer"] is None)
    assert prod_py.process.poll() is None and con_py.process.poll() is None
