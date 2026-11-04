from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.auth.store import Principal
from app.usage.quota import exceeded, seconds_until_next_utc_day
from app.usage.store import DailyUsage


def principal(tokens: int | None = None, usd: str | None = None) -> Principal:
    return Principal(
        user_id=1,
        user_name="u",
        key_id=1,
        rate_limit_per_min=60,
        daily_token_limit=tokens,
        daily_usd_limit=Decimal(usd) if usd is not None else None,
        cache_enabled=True,
    )


def used(tokens: int = 0, usd: str = "0") -> DailyUsage:
    return DailyUsage(billed_tokens=tokens, cost_usd=Decimal(usd))


def test_no_limits_means_no_quota_however_much_is_used() -> None:
    assert exceeded(principal(), used(10**9, "1000000")) is None


def test_under_the_token_limit_is_fine() -> None:
    assert exceeded(principal(tokens=1_000), used(999)) is None


def test_reaching_the_token_limit_stops_requests() -> None:
    assert exceeded(principal(tokens=1_000), used(1_000)) == "tokens"
    assert exceeded(principal(tokens=1_000), used(5_000)) == "tokens"


def test_under_the_spending_limit_is_fine() -> None:
    assert exceeded(principal(usd="2.50"), used(usd="2.49999999")) is None


def test_reaching_the_spending_limit_stops_requests() -> None:
    assert exceeded(principal(usd="2.50"), used(usd="2.50")) == "usd"
    assert exceeded(principal(usd="2.50"), used(usd="3")) == "usd"


def test_the_token_limit_is_reported_first_when_both_are_reached() -> None:
    assert exceeded(principal(tokens=10, usd="1"), used(10, "1")) == "tokens"


def test_each_limit_is_independent() -> None:
    assert exceeded(principal(tokens=10, usd="1"), used(5, "1")) == "usd"
    assert exceeded(principal(tokens=10, usd="1"), used(10, "0.5")) == "tokens"
    assert exceeded(principal(tokens=10, usd="1"), used(5, "0.5")) is None


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 11, 3, 12, 0, 0, tzinfo=UTC), 12 * 3600),
        (datetime(2026, 11, 3, 23, 59, 59, tzinfo=UTC), 1),
        (datetime(2026, 11, 3, 0, 0, 0, tzinfo=UTC), 24 * 3600),  # just reset
        (datetime(2026, 12, 31, 23, 0, 0, tzinfo=UTC), 3600),  # across a year end
    ],
)
def test_seconds_until_the_daily_reset(now: datetime, expected: int) -> None:
    assert seconds_until_next_utc_day(now) == expected


def test_the_reset_is_midnight_utc_whatever_zone_the_clock_is_in() -> None:
    toronto = timezone(timedelta(hours=-5))
    now = datetime(2026, 11, 3, 19, 30, 0, tzinfo=toronto)  # 00:30 UTC on the 4th

    assert seconds_until_next_utc_day(now) == 23 * 3600 + 30 * 60
