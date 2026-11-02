"""One real worker for synchronous device operations.

An asyncio lock cannot serialize cancelled ``to_thread`` calls: their
threads keep running after the lock is released. This executor preserves
submission order until the actual operations finish, including shutdown.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

_T = TypeVar("_T")


class SerialExecutor:
    """Submit work synchronously; cancellation only abandons its result.

    Calls must be made from one event-loop thread. ``close`` rejects new
    work immediately and drains previously submitted operations before
    releasing the worker. Device calls must impose their own I/O timeout.
    """

    def __init__(self, name: str = "yoke-driver") -> None:
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=name)
        self._closed = False
        self._drained: asyncio.Future[None] | None = None

    def submit(self, call: Callable[[], _T]) -> asyncio.Future[_T]:
        if self._closed:
            raise RuntimeError("executor is closed")
        future = asyncio.wrap_future(self._pool.submit(call))
        # A cancelled waiter can abandon an exception. Retrieve it even
        # when no caller remains, without changing the result for waiters.
        future.add_done_callback(self._observe)
        return future

    @staticmethod
    def _observe(future: asyncio.Future[_T]) -> None:
        if not future.cancelled():
            future.exception()

    async def call(self, call: Callable[[], _T]) -> _T:
        return await asyncio.shield(self.submit(call))

    async def close(self) -> None:
        if self._drained is None:
            self._closed = True
            self._drained = asyncio.wrap_future(self._pool.submit(lambda: None))
        await asyncio.shield(self._drained)
        # The barrier has completed: no device work remains to block here.
        self._pool.shutdown(wait=True)
