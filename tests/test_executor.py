"""Actual worker cancellation and ordering, without device/network I/O."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable

import pytest

from yoke.net.executor import SerialExecutor


async def eventually(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.001)


async def test_cancelled_call_cannot_overlap_or_reorder_next_operation() -> None:
    worker = SerialExecutor()
    entered = threading.Event()
    release = threading.Event()
    calls: list[tuple[str, int]] = []

    def first() -> None:
        calls.append(("first-start", threading.get_ident()))
        entered.set()
        assert release.wait(2)
        calls.append(("first-end", threading.get_ident()))

    def second() -> None:
        calls.append(("second", threading.get_ident()))

    try:
        task = asyncio.create_task(worker.call(first))
        await eventually(entered.is_set)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        following = worker.submit(second)
        assert [name for name, _ in calls] == ["first-start"]
        release.set()
        await following
        assert [name for name, _ in calls] == ["first-start", "first-end", "second"]
        assert len({thread_id for _, thread_id in calls}) == 1
    finally:
        release.set()
        await worker.close()


async def test_close_drains_work_after_waiter_and_close_waiter_cancelled() -> None:
    worker = SerialExecutor()
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocked() -> None:
        entered.set()
        assert release.wait(2)
        finished.set()

    call = asyncio.create_task(worker.call(blocked))
    try:
        await eventually(entered.is_set)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        close = asyncio.create_task(worker.close())
        await asyncio.sleep(0)
        assert not close.done()
        with pytest.raises(RuntimeError, match="closed"):
            worker.submit(lambda: None)
        close.cancel()
        with pytest.raises(asyncio.CancelledError):
            await close
        release.set()
        await worker.close()
        assert finished.is_set()
        await worker.close()  # idempotent, without submitting to a dead pool
    finally:
        release.set()
        await worker.close()


async def test_operation_failure_does_not_poison_next_operation() -> None:
    worker = SerialExecutor()

    def fail() -> None:
        raise OSError("scripted device failure")

    try:
        with pytest.raises(OSError, match="scripted"):
            await worker.call(fail)
        assert await worker.call(lambda: 42) == 42
    finally:
        await worker.close()
