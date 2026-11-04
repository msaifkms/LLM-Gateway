"""Daily quotas: the pure decision, kept apart from the database and HTTP."""

from datetime import UTC, datetime, timedelta
from typing import Literal

from app.auth.store import Principal
from app.usage.store import DailyUsage

Exceeded = Literal["tokens", "usd"]


def exceeded(principal: Principal, used: DailyUsage) -> Exceeded | None:
    """Which daily quota the user has reached, if any. A NULL limit means no limit.

    "Reached" means used >= limit, so a limit of 1,000 tokens stops requests once
    1,000 have been spent.
    """
    if (
        principal.daily_token_limit is not None
        and used.billed_tokens >= principal.daily_token_limit
    ):
        return "tokens"
    if (
        principal.daily_usd_limit is not None
        and used.cost_usd >= principal.daily_usd_limit
    ):
        return "usd"
    return None


def seconds_until_next_utc_day(now: datetime) -> int:
    """How long until the quota resets at 00:00 UTC. Always at least 1."""
    now = now.astimezone(UTC)
    midnight = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return max(1, int((midnight - now).total_seconds()))
