"""Global kill switch: one flag in Postgres that makes the gateway refuse chat requests.

It lives in the database, not an environment variable, so flipping it takes effect
without a restart or a deploy. Each gateway process re-reads it at most every
CACHE_TTL_S seconds, so it takes effect everywhere within about two seconds.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import asyncpg

logger = logging.getLogger(__name__)

FLAG_NAME = "kill_switch"
CACHE_TTL_S = 2.0
DB_ERRORS = (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError)


class KillSwitchUnavailable(Exception):
    """The flag could not be read and no earlier value is known."""


@dataclass(frozen=True)
class KillSwitchState:
    enabled: bool
    reason: str = ""


class FlagStore(Protocol):
    async def get(self) -> KillSwitchState: ...


class PostgresKillSwitchStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def get(self) -> KillSwitchState:
        row = await self._pool.fetchrow(
            "SELECT enabled, reason FROM gateway_flags WHERE name = $1", FLAG_NAME
        )
        if row is None:
            return KillSwitchState(enabled=False)  # no row means off
        return KillSwitchState(enabled=row["enabled"], reason=row["reason"])

    async def set(self, enabled: bool, reason: str = "") -> None:
        await self._pool.execute(
            "INSERT INTO gateway_flags (name, enabled, reason) VALUES ($1, $2, $3) "
            "ON CONFLICT (name) DO UPDATE SET enabled = EXCLUDED.enabled, "
            "reason = EXCLUDED.reason, updated_at = now()",
            FLAG_NAME,
            enabled,
            reason,
        )


class KillSwitch:
    """Reads the flag through a short cache, so a request does not cost a query."""

    def __init__(
        self,
        store: FlagStore,
        *,
        ttl_s: float = CACHE_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._ttl_s = ttl_s
        self._clock = clock
        self._enabled: bool | None = None  # None until the first successful read
        self._checked_at = float("-inf")
        self._lock = asyncio.Lock()

    def _fresh(self) -> bool:
        return self._clock() - self._checked_at < self._ttl_s

    def _answer(self) -> bool:
        if self._enabled is None:
            raise KillSwitchUnavailable
        return self._enabled

    async def is_on(self) -> bool:
        if self._fresh():
            return self._answer()
        async with self._lock:  # one reader refreshes; the rest wait and reuse it
            if not self._fresh():
                await self._refresh()
        return self._answer()

    async def _refresh(self) -> None:
        try:
            state = await self._store.get()
        except DB_ERRORS:
            self._checked_at = self._clock()  # retry in ttl_s, not on every request
            if self._enabled is not None:
                logger.warning("Kill switch lookup failed; using the last known value")
            return

        if state.enabled != self._enabled:
            logger.warning(
                "Kill switch is now %s%s",
                "ON" if state.enabled else "OFF",
                f" ({state.reason})" if state.reason else "",
            )
        self._enabled = state.enabled
        self._checked_at = self._clock()
