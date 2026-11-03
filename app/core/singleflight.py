"""Single-flight: concurrent calls with the same key share one execution."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any


class SingleFlight:
    """Collapses identical in-flight work into one call (cache stampede guard).

    The first caller for a key is the *leader*; callers that arrive while it is
    still running are *followers* and wait for the leader's result (or error).
    The work runs in its own task, so a client that disconnects mid-request
    cannot cancel it for everyone else.

    In-process only: with several gateway instances, each still makes one call.
    """

    def __init__(self) -> None:
        self._inflight: dict[str, asyncio.Future[Any]] = {}

    @property
    def in_flight(self) -> int:
        return len(self._inflight)

    async def run[T](
        self, key: str, func: Callable[[], Awaitable[T]]
    ) -> tuple[T, bool]:
        """Return (result, is_leader). Errors are raised to every waiter."""
        task = self._inflight.get(key)
        is_leader = task is None

        if task is None:
            task = asyncio.ensure_future(func())
            self._inflight[key] = task
            task.add_done_callback(lambda done: self._finish(key, done))

        # shield: cancelling one waiting request must not cancel the shared work.
        return await asyncio.shield(task), is_leader

    def _finish(self, key: str, task: asyncio.Future[Any]) -> None:
        if self._inflight.get(key) is task:
            del self._inflight[key]
        if not task.cancelled():
            task.exception()  # mark retrieved; each waiter re-raises it itself
