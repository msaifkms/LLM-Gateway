from datetime import date
from decimal import Decimal

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.api.auth import authenticate
from app.api.usage import get_usage_store
from app.main import app
from app.usage.store import (
    DayUsage,
    ModelUsage,
    UsageEvent,
    UsageSummary,
    UsageTotals,
)

SUMMARY = UsageSummary(
    totals=UsageTotals(
        requests=3,
        cache_served=1,
        prompt_tokens=3_000,
        completion_tokens=1_500,
        cost_usd=Decimal("0.00240000"),
        saved_usd=Decimal("0.00120000"),
    ),
    by_model=[
        ModelUsage("gpt-4.1-mini", 3, 3_000, 1_500, Decimal("0.00240000")),
    ],
    by_day=[
        DayUsage(date(2026, 11, 2), 1, 1_000, 500, Decimal("0.00120000")),
        DayUsage(date(2026, 11, 3), 2, 2_000, 1_000, Decimal("0.00120000")),
    ],
)


class FakeSummaryStore:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.asked: list[tuple[int, int]] = []

    async def record(self, event: UsageEvent) -> None:
        raise NotImplementedError

    async def summary(self, user_id: int, days: int) -> UsageSummary:
        self.asked.append((user_id, days))
        if self.error is not None:
            raise self.error
        return SUMMARY


def use(monkeypatch: pytest.MonkeyPatch, store: FakeSummaryStore | None) -> None:
    monkeypatch.setitem(app.dependency_overrides, get_usage_store, lambda: store)


def test_usage_reports_totals_models_and_days(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    use(monkeypatch, FakeSummaryStore())

    response = client.get("/v1/usage")

    assert response.status_code == 200
    assert response.json() == {
        "days": 7,
        "totals": {
            "requests": 3,
            "cache_served": 1,
            "prompt_tokens": 3000,
            "completion_tokens": 1500,
            "cost_usd": "0.00240000",  # money travels as a string, never a float
            "saved_usd": "0.00120000",
        },
        "by_model": [
            {
                "model": "gpt-4.1-mini",
                "requests": 3,
                "prompt_tokens": 3000,
                "completion_tokens": 1500,
                "cost_usd": "0.00240000",
            }
        ],
        "by_day": [
            {
                "day": "2026-11-02",
                "requests": 1,
                "prompt_tokens": 1000,
                "completion_tokens": 500,
                "cost_usd": "0.00120000",
            },
            {
                "day": "2026-11-03",
                "requests": 2,
                "prompt_tokens": 2000,
                "completion_tokens": 1000,
                "cost_usd": "0.00120000",
            },
        ],
    }


def test_usage_is_only_ever_the_callers_own(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FakeSummaryStore()
    use(monkeypatch, store)

    client.get("/v1/usage?days=30")

    assert store.asked == [(1, 30)]  # TEST_PRINCIPAL is user 1; no user id is accepted


@pytest.mark.parametrize("days", ["0", "91", "-1", "abc"])
def test_the_window_must_be_between_1_and_90_days(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, days: str
) -> None:
    store = FakeSummaryStore()
    use(monkeypatch, store)

    assert client.get(f"/v1/usage?days={days}").status_code == 422
    assert store.asked == []


def test_usage_needs_an_api_key(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    use(monkeypatch, FakeSummaryStore())
    monkeypatch.delitem(app.dependency_overrides, authenticate)

    assert client.get("/v1/usage").status_code == 401


def test_usage_is_503_without_a_database(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    use(monkeypatch, None)

    response = client.get("/v1/usage")

    assert response.status_code == 503
    assert response.json() == {"detail": "Usage reporting is unavailable"}


def test_usage_is_503_when_the_database_errors(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    use(monkeypatch, FakeSummaryStore(error=asyncpg.PostgresConnectionError("down")))

    assert client.get("/v1/usage").status_code == 503


def test_zero_money_is_fixed_point_not_scientific(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ZeroStore(FakeSummaryStore):
        async def summary(self, user_id: int, days: int) -> UsageSummary:
            zero = Decimal("0E-8")  # what Postgres returns for a sum of zeros
            return UsageSummary(
                totals=UsageTotals(1, 0, 0, 0, zero, zero),
                by_model=[ModelUsage("mock-1", 1, 0, 0, zero)],
                by_day=[DayUsage(date(2026, 11, 3), 1, 0, 0, zero)],
            )

    use(monkeypatch, ZeroStore())

    body = client.get("/v1/usage").json()

    assert body["totals"]["cost_usd"] == "0.00000000"
    assert body["totals"]["saved_usd"] == "0.00000000"
    assert body["by_model"][0]["cost_usd"] == "0.00000000"
    assert body["by_day"][0]["cost_usd"] == "0.00000000"
