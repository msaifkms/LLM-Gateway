"""Usage rows: one INSERT per answered request, and the per-user summary."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol

import asyncpg


@dataclass(frozen=True)
class UsageEvent:
    user_id: int
    api_key_id: int
    route: str
    provider: str
    model: str
    cache_status: str  # MISS, BYPASS, HIT or COALESCED
    prompt_tokens: int
    completion_tokens: int
    cost_usd: Decimal
    list_cost_usd: Decimal
    priced: bool


@dataclass(frozen=True)
class UsageTotals:
    requests: int
    cache_served: int  # HIT + COALESCED: answered without a provider call
    prompt_tokens: int
    completion_tokens: int
    cost_usd: Decimal
    saved_usd: Decimal


@dataclass(frozen=True)
class ModelUsage:
    model: str
    requests: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: Decimal


@dataclass(frozen=True)
class DayUsage:
    day: date  # UTC calendar day
    requests: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: Decimal


@dataclass(frozen=True)
class UsageSummary:
    totals: UsageTotals
    by_model: list[ModelUsage]
    by_day: list[DayUsage]


class UsageStore(Protocol):
    async def record(self, event: UsageEvent) -> None: ...

    async def summary(self, user_id: int, days: int) -> UsageSummary: ...


INSERT_SQL = """
INSERT INTO usage_events (
    user_id, api_key_id, route, provider, model, cache_status,
    prompt_tokens, completion_tokens, cost_usd, list_cost_usd, priced
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
"""

WINDOW = "user_id = $1 AND created_at >= now() - make_interval(days => $2)"

TOTALS_SQL = f"""
SELECT count(*)::int AS requests,
       (count(*) FILTER (WHERE cache_status IN ('HIT', 'COALESCED')))::int AS cache_served,
       COALESCE(sum(prompt_tokens), 0)::bigint AS prompt_tokens,
       COALESCE(sum(completion_tokens), 0)::bigint AS completion_tokens,
       COALESCE(sum(cost_usd), 0) AS cost_usd,
       COALESCE(sum(list_cost_usd - cost_usd), 0) AS saved_usd
FROM usage_events
WHERE {WINDOW}
"""

BY_MODEL_SQL = f"""
SELECT model,
       count(*)::int AS requests,
       sum(prompt_tokens)::bigint AS prompt_tokens,
       sum(completion_tokens)::bigint AS completion_tokens,
       sum(cost_usd) AS cost_usd
FROM usage_events
WHERE {WINDOW}
GROUP BY model
ORDER BY sum(cost_usd) DESC, model
"""

BY_DAY_SQL = f"""
SELECT (created_at AT TIME ZONE 'UTC')::date AS day,
       count(*)::int AS requests,
       sum(prompt_tokens)::bigint AS prompt_tokens,
       sum(completion_tokens)::bigint AS completion_tokens,
       sum(cost_usd) AS cost_usd
FROM usage_events
WHERE {WINDOW}
GROUP BY 1
ORDER BY 1
"""


class PostgresUsageStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def record(self, event: UsageEvent) -> None:
        await self._pool.execute(
            INSERT_SQL,
            event.user_id,
            event.api_key_id,
            event.route,
            event.provider,
            event.model,
            event.cache_status,
            event.prompt_tokens,
            event.completion_tokens,
            event.cost_usd,
            event.list_cost_usd,
            event.priced,
        )

    async def summary(self, user_id: int, days: int) -> UsageSummary:
        async with self._pool.acquire() as conn:
            totals = await conn.fetchrow(TOTALS_SQL, user_id, days)
            models = await conn.fetch(BY_MODEL_SQL, user_id, days)
            daily = await conn.fetch(BY_DAY_SQL, user_id, days)

        assert totals is not None  # an aggregate query always returns one row
        return UsageSummary(
            totals=UsageTotals(**dict(totals)),
            by_model=[ModelUsage(**dict(r)) for r in models],
            by_day=[DayUsage(**dict(r)) for r in daily],
        )
