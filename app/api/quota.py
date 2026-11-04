"""Quota enforcement for the chat endpoint."""

from datetime import UTC, datetime

import asyncpg
from fastapi import HTTPException

from app.auth.store import Principal
from app.usage.quota import exceeded, seconds_until_next_utc_day
from app.usage.store import UsageStore

MESSAGES = {
    "tokens": "Daily token quota exceeded",
    "usd": "Daily spending quota exceeded",
}


async def enforce_quota(principal: Principal, store: UsageStore | None) -> None:
    """Refuse the request if the user has used up a daily quota.

    Called only when a provider is about to be paid for. Cache hits are free, so
    they are never blocked. Users with no limits cost nothing here (no query).
    Fails closed: if a limit exists but usage cannot be read, the request is refused.
    """
    if principal.daily_token_limit is None and principal.daily_usd_limit is None:
        return
    if store is None:
        raise HTTPException(status_code=503, detail="Quota check unavailable")

    try:
        used = await store.usage_today(principal.user_id)
    except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError):
        raise HTTPException(status_code=503, detail="Quota check unavailable") from None

    reached = exceeded(principal, used)
    if reached is not None:
        retry_after = seconds_until_next_utc_day(datetime.now(UTC))
        raise HTTPException(
            status_code=429,
            detail=MESSAGES[reached],
            headers={"Retry-After": str(retry_after)},
        )
