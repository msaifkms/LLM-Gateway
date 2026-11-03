import asyncio
import logging

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.core.config import Settings
from app.core.redis_client import check_redis


class FakeRedis:
    """Stands in for the Redis client: only ping() is needed here."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.pings = 0

    async def ping(self) -> bool:
        self.pings += 1
        if self.error is not None:
            raise self.error
        return True


def test_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("REDIS_TIMEOUT_S", raising=False)

    settings = Settings.from_env()

    assert settings.redis_url == "redis://localhost:6379/0"
    assert settings.redis_timeout_s == 0.5


def test_settings_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/1")
    monkeypatch.setenv("REDIS_TIMEOUT_S", "2")

    settings = Settings.from_env()

    assert settings.redis_url == "redis://redis:6379/1"
    assert settings.redis_timeout_s == 2.0


@pytest.mark.parametrize("value", ["", "   "])
def test_empty_redis_url_disables_redis(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("REDIS_URL", value)

    assert Settings.from_env().redis_url is None


def test_invalid_timeout_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_TIMEOUT_S", "fast")

    with pytest.raises(ValueError):
        Settings.from_env()


def test_check_redis_reports_healthy() -> None:
    fake = FakeRedis()

    assert asyncio.run(check_redis(fake)) is True  # type: ignore[arg-type]
    assert fake.pings == 1


@pytest.mark.parametrize(
    "error", [RedisConnectionError("refused"), OSError("network down")]
)
def test_check_redis_returns_false_and_logs_instead_of_raising(
    error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        result = asyncio.run(check_redis(FakeRedis(error)))  # type: ignore[arg-type]

    assert result is False
    assert "unreachable" in caplog.text


def test_cache_ttl_default_and_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CACHE_TTL_SECONDS", raising=False)
    assert Settings.from_env().cache_ttl_s == 3600

    monkeypatch.setenv("CACHE_TTL_SECONDS", "120")
    assert Settings.from_env().cache_ttl_s == 120


@pytest.mark.parametrize("value", ["0", "-5", "soon"])
def test_invalid_cache_ttl_fails_fast(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("CACHE_TTL_SECONDS", value)

    with pytest.raises(ValueError):
        Settings.from_env()
