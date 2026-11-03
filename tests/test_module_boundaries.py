"""Exercise optional components without loading unrelated command shells."""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest


@pytest.mark.parametrize(
    ("blocked", "program"),
    [
        (
            ("yoke.cli",),
            """
            import argparse
            from yoke.telemetry.cli import configure_parsers, run_command

            parser = argparse.ArgumentParser()
            parser.set_defaults(
                group="239.121.111.107", port=9753,
                interface="0.0.0.0", psk_file=None,
            )
            configure_parsers(parser.add_subparsers(dest="command", required=True))
            args = parser.parse_args(["top", "--demo", "--once"])
            assert run_command(args) == 0
            """,
        ),
        (
            ("yoke.cli", "yoke.core", "yoke.drivers", "yoke.net", "yoke.snoop"),
            """
            from yoke.media.snoop import SnoopRecord, render_text

            record = SnoopRecord(
                verdict="ok", stream_id="synthetic", stage="wire", seq=1,
                summary="untrusted \\x1b[31mtext", detail={}, now=0.0,
            )
            rendered = render_text(record)
            assert "\\x1b" not in rendered
            assert r"\\x1b[31mtext" in rendered
            """,
        ),
    ],
    ids=("telemetry-command-without-main-cli", "media-observer-without-control"),
)
def test_optional_components_keep_their_import_boundaries(
    blocked: tuple[str, ...], program: str
) -> None:
    # A fresh interpreter prevents imports made by other tests from hiding a
    # dependency. Block transitive imports while exercising the public behavior.
    guard = f"""import importlib.abc
import sys

class BoundaryGuard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + ".")
               for name in {blocked!r}):
            raise AssertionError("unexpected component dependency: " + fullname)

sys.meta_path.insert(0, BoundaryGuard())
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", guard + textwrap.dedent(program)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
