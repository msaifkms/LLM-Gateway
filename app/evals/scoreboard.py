"""Latest eval scores for /metrics.

Evals run in a separate process (the CLI) that Prometheus never scrapes, so the
gateway publishes the latest stored results instead. They are read from Postgres
at scrape time, at most once every TTL_S seconds; if the read fails the last
known values are kept, so a database blip does not blank the dashboard.
"""

import logging
import time
from collections.abc import Callable
from typing import Protocol

import asyncpg

from app.evals.store import LatestScore

logger = logging.getLogger(__name__)

TTL_S = 30.0
DB_ERRORS = (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError)


class ScoreSource(Protocol):
    async def latest_scores(self) -> list[LatestScore]: ...


class EvalScoreboard:
    def __init__(
        self,
        source: ScoreSource,
        *,
        ttl_s: float = TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._source = source
        self._ttl_s = ttl_s
        self._clock = clock
        self._scores: list[LatestScore] | None = None
        self._read_at = float("-inf")

    async def latest(self) -> list[LatestScore] | None:
        """The latest scores, or None if they have never been readable."""
        if self._clock() - self._read_at < self._ttl_s:
            return self._scores
        # Try again only after another TTL, failure or not, so a down database
        # costs one query per TTL instead of one per scrape.
        self._read_at = self._clock()
        try:
            self._scores = await self._source.latest_scores()
        except DB_ERRORS:
            logger.warning("Could not read eval scores; keeping the last values")
        return self._scores
