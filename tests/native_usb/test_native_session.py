"""Deterministic C unit tests of native/yusb/linux/session.c (session_test.c).

Exercises F5 (s->in_send teardown guard, start_gadget stopping race) and F11
(monotonic join_until with SIGUSR1 cancellation) against a scripted loopback
exporter and fake ysys() Raw Gadget backend.
"""

from __future__ import annotations

import subprocess

from conftest import HERE, USB, build, library_sources


def test_session_teardown_and_races() -> None:
    sources = [
        HERE / "session_test.c",
        *library_sources(),
        USB / "linux" / "rawgadget.c",
        USB / "linux" / "session.c",
    ]
    binary = build(
        "session-test",
        sources,
        std="gnu11",
        sanitize=False,
        extra=("-Wl,--wrap=sendmsg",),
    )
    result = subprocess.run(
        [str(binary)], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "all session tests passed" in result.stdout
