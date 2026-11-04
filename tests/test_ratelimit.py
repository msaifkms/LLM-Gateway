import asyncio
import logging
from typing import Any

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.core.config import Settings
from app.core.ratelimit import (
    BUCKET_SCRIPT,
    KEY_PREFIX,
    RateLimiter,
    RateLimiterUnavailable,
    RateLimitResult,
)

# --- the limiter's Python side (the Lua itself is tested against real Redis) ---


class FakeScript:
    """Stands in for a registered Redis script."""

    def __init__(self, replies: list[Any] | None = None) -> None:
        self.replies = list(replies or [])
        self.calls: list[tuple[list[str], list[float]]] = []

    async def __call__(self, keys: list[str], args: list[float]) -> Any:
        self.calls.append((keys, args))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class FakeClient:
    def __init__(self, script: FakeScript) -> None:
        self.script = script
        self.registered: str | None = None

    def register_script(self, lua: str) -> FakeScript:
        self.registered = lua
        return self.script


def make_limiter(
    *replies: Any, fail_open: bool = True
) -> tuple[RateLimiter, FakeScript]:
    script = FakeScript(list(replies))
    return RateLimiter(FakeClient(script), fail_open=fail_open), script  # type: ignore[arg-type]


def check(limiter: RateLimiter, user_id: int = 1, limit: int = 60):  # type: ignore[no-untyped-def]
    return asyncio.run(limiter.check(user_id, limit))


def test_an_allowed_reply_becomes_a_result() -> None:
    limiter, _ = make_limiter([1, 59, b"0"])

    assert check(limiter) == RateLimitResult(
        allowed=True, limit=60, remaining=59, retry_after_s=0.0
    )


def test_a_denied_reply_carries_how_long_to_wait() -> None:
    limiter, _ = make_limiter([0, 0, b"11.97"])

    result = check(limiter)

    assert result is not None
    assert result.allowed is False
    assert result.remaining == 0
    assert result.retry_after_s == pytest.approx(11.97)


def test_the_script_gets_the_users_key_capacity_and_refill_rate() -> None:
    limiter, script = make_limiter([1, 119, b"0"])

    check(limiter, user_id=7, limit=120)

    assert script.calls == [([f"{KEY_PREFIX}7"], [120, 2.0])]  # 120/min = 2 per second


def test_each_user_has_their_own_bucket() -> None:
    limiter, script = make_limiter([1, 9, b"0"], [1, 9, b"0"])

    check(limiter, user_id=1)
    check(limiter, user_id=2)

    assert script.calls[0][0] != script.calls[1][0]


def test_the_script_does_its_own_timekeeping_and_cleanup() -> None:
    assert "redis.call('TIME')" in BUCKET_SCRIPT  # one clock for every instance
    assert "PEXPIRE" in BUCKET_SCRIPT  # idle buckets do not pile up in Redis


# --- when Redis is down ----------------------------------------------------


def test_failing_open_lets_the_request_through(
    caplog: pytest.LogCaptureFixture,
) -> None:
    limiter, _ = make_limiter(RedisConnectionError("down"), fail_open=True)

    with caplog.at_level(logging.WARNING):
        assert check(limiter) is None  # None = "no verdict, allow it"

    assert "allowing requests" in caplog.text


def test_failing_closed_refuses_the_request() -> None:
    limiter, _ = make_limiter(RedisConnectionError("down"), fail_open=False)

    with pytest.raises(RateLimiterUnavailable):
        check(limiter)


@pytest.mark.parametrize("error", [OSError("reset"), TimeoutError()])
def test_any_connection_level_failure_counts_as_down(error: Exception) -> None:
    limiter, _ = make_limiter(error, fail_open=True)

    assert check(limiter) is None


def test_an_outage_is_logged_once_not_per_request(
    caplog: pytest.LogCaptureFixture,
) -> None:
    down = RedisConnectionError("down")
    limiter, _ = make_limiter(down, down, down)

    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            check(limiter)

    assert caplog.text.count("rate limiter unavailable") == 1


def test_recovery_is_logged_and_a_new_outage_warns_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    limiter, _ = make_limiter(
        RedisConnectionError("down"),
        [1, 9, b"0"],  # Redis came back
        RedisConnectionError("down again"),
    )

    with caplog.at_level(logging.INFO):
        for _ in range(3):
            check(limiter)

    assert caplog.text.count("rate limiter unavailable") == 2
    assert "recovered" in caplog.text


# --- settings --------------------------------------------------------------


def test_failing_open_is_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RATE_LIMIT_FAIL_OPEN", raising=False)

    assert Settings.from_env().rate_limit_fail_open is True


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("true", True),
        ("1", True),
        ("YES", True),
        ("false", False),
        ("0", False),
        ("No", False),
    ],
)
def test_fail_open_can_be_set_either_way(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: bool
) -> None:
    monkeypatch.setenv("RATE_LIMIT_FAIL_OPEN", value)

    assert Settings.from_env().rate_limit_fail_open is expected


def test_a_garbled_fail_open_setting_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RATE_LIMIT_FAIL_OPEN", "maybe")

    with pytest.raises(ValueError, match="RATE_LIMIT_FAIL_OPEN"):
        Settings.from_env()
