"""Command execution seam for drivers that shell out to device tools.

Drivers never call :mod:`subprocess` directly: they are handed a
:class:`CommandRunner`, which in production is :class:`SubprocessRunner`
and in tests is :class:`FakeRunner`. This keeps every driver code path —
including timeouts and missing binaries — testable offline, and makes
``shell=True`` structurally impossible: argv is always a vector, never a
string handed to a shell.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from yoke.drivers.base import DriverError


@dataclass(frozen=True)
class CommandResult:
    """Outcome of one completed (non-timed-out) command."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@runtime_checkable
class CommandRunner(Protocol):
    def run(self, argv: Sequence[str], *, timeout: float) -> CommandResult:
        """Run ``argv`` and return its result.

        Raises :class:`DriverError` when the command cannot be run at
        all (missing binary) or does not finish within ``timeout``
        seconds. A nonzero exit status is *not* an error at this layer;
        callers decide what a failing command means.
        """
        ...


class SubprocessRunner:
    """Real runner: ``subprocess.run`` in text mode, output captured.

    Never uses ``shell=True``. Environment and working directory are
    inherited from the agent process.
    """

    def run(self, argv: Sequence[str], *, timeout: float) -> CommandResult:
        try:
            proc = subprocess.run(
                list(argv),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise DriverError(
                f"command timed out after {timeout:g}s: {' '.join(argv)}"
            ) from exc
        except FileNotFoundError as exc:
            raise DriverError(f"command not found: {argv[0]}") from exc
        return CommandResult(
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )


# A scripted response: a ready-made result, an exception instance to
# raise (e.g. DriverError, matching what SubprocessRunner raises on
# timeout or a missing binary), or a callable computing either from the
# full argv.
FakeResponse = CommandResult | Exception | Callable[[Sequence[str]], CommandResult]


@dataclass
class FakeRunner:
    """Test double for :class:`CommandRunner`.

    Constructed with ``(argv_prefix, response)`` pairs. Each ``run``
    call is matched against the prefixes in order; the first pair whose
    prefix equals the leading elements of ``argv`` supplies the
    response. Responses are not consumed, so repeated polls can reuse
    one script entry; use a callable response for call-varying output.

    Every invocation is recorded in :attr:`calls` (argv tuples, in
    order) and :attr:`timeouts`. An invocation matching no scripted
    prefix raises :class:`AssertionError` — an unexpected command in a
    test is a test bug, not driver behavior.
    """

    scripts: list[tuple[tuple[str, ...], FakeResponse]] = field(default_factory=list)
    calls: list[tuple[str, ...]] = field(default_factory=list)
    timeouts: list[float] = field(default_factory=list)

    def script(self, prefix: Iterable[str], response: FakeResponse) -> None:
        """Append one scripted response for commands starting with ``prefix``."""
        self.scripts.append((tuple(prefix), response))

    def run(self, argv: Sequence[str], *, timeout: float) -> CommandResult:
        call = tuple(argv)
        self.calls.append(call)
        self.timeouts.append(timeout)
        for prefix, response in self.scripts:
            if call[: len(prefix)] == prefix:
                if isinstance(response, Exception):
                    raise response
                if isinstance(response, CommandResult):
                    return response
                return response(argv)
        raise AssertionError(f"unexpected command: {call!r}")
