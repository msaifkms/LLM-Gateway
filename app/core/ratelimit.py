"""Per-user rate limiting: a token bucket in Redis, updated atomically.

Each user has a bucket that holds up to `limit` tokens and refills at
limit/60 tokens per second. A request spends one token; with none left it is
refused and told how long to wait.

The check-and-spend runs as one Lua script inside Redis. Redis runs a script
start to finish with nothing else interleaved, so a hundred simultaneous
requests cannot all read "1 token left" and all be let through.
"""

import logging
from dataclasses import dataclass

from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

KEY_PREFIX = "llmgw:rl:v1:"

BUCKET_SCRIPT = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2]) -- tokens added per second

-- One clock for every gateway instance: Redis's own.
local time = redis.call('TIME')
local now = tonumber(time[1]) + tonumber(time[2]) / 1000000

local state = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(state[1])
local last = tonumber(state[2])
if tokens == nil or last == nil then
  tokens = capacity
  last = now
end

local elapsed = math.max(0, now - last)
tokens = math.min(capacity, tokens + elapsed * rate)

local allowed = 0
local retry_after = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
else
  retry_after = (1 - tokens) / rate
end

redis.call('HSET', key, 'tokens', tostring(tokens), 'ts', tostring(now))
-- Once the bucket would be full again it is indistinguishable from a new one.
redis.call('PEXPIRE', key, math.ceil(capacity / rate * 1000) + 1000)

return {allowed, math.floor(tokens), tostring(retry_after)}
"""


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    #: How long until a token is available. 0 when the request was allowed.
    retry_after_s: float


class RateLimiterUnavailable(Exception):
    """Redis could not be reached and the limiter is configured to fail closed."""


class RateLimiter:
    def __init__(self, client: Redis, *, fail_open: bool = True) -> None:
        self._script = client.register_script(BUCKET_SCRIPT)
        self._fail_open = fail_open
        self._unavailable = False  # so an outage is logged once, not per request

    async def check(self, user_id: int, limit_per_min: int) -> RateLimitResult | None:
        """Spend one token for this user.

        Returns None when Redis is down and the limiter fails open (the request
        is then allowed, unlimited). Raises RateLimiterUnavailable when it fails
        closed.
        """
        try:
            reply = await self._script(
                keys=[f"{KEY_PREFIX}{user_id}"],
                args=[limit_per_min, limit_per_min / 60],
            )
        except (RedisError, OSError, TimeoutError):
            self._on_failure()  # raises if configured to fail closed
            return None

        if self._unavailable:
            logger.info("Redis rate limiter recovered")
            self._unavailable = False

        allowed, remaining, retry_after = reply
        return RateLimitResult(
            allowed=bool(int(allowed)),
            limit=limit_per_min,
            remaining=int(remaining),
            retry_after_s=float(retry_after),
        )

    def _on_failure(self) -> None:
        if not self._unavailable:
            self._unavailable = True
            policy = "allowing requests" if self._fail_open else "refusing requests"
            logger.warning("Redis rate limiter unavailable; %s", policy)
        if not self._fail_open:
            raise RateLimiterUnavailable from None
