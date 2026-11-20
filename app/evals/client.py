"""Calls the gateway's own HTTP API, exactly like any other client would.

Evals go through authentication, rate limits, quotas, the cache and cost tracking,
so they measure what users get. The API key travels only in the Authorization
header and is never printed, logged or stored.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal

import httpx
from pydantic import ValidationError

from app.api.schemas import ChatCompletionResponse, ChatMessage
from app.usage.pricing import compute_cost
from app.usage.recorder import BILLABLE_STATUSES

CHAT_PATH = "/v1/chat/completions"
#: Wait at most this long for one 429. A rate limit clears within a minute;
#: a daily quota says "come back tomorrow", which is an error, not a wait.
DEFAULT_MAX_WAIT_S = 30.0
DEFAULT_RATE_LIMIT_RETRIES = 5


class GatewayError(Exception):
    """The gateway refused or failed a request. ``status`` is None for no reply."""

    def __init__(self, status: int | None, detail: str) -> None:
        self.status = status
        self.detail = detail
        prefix = f"HTTP {status}" if status is not None else "no response"
        super().__init__(f"{prefix}: {detail}")

    @property
    def fatal(self) -> bool:
        """A bad key fails every case the same way, so the run should stop."""
        return self.status == 401


@dataclass(frozen=True)
class ChatReply:
    text: str
    #: The provider's model name, e.g. "gpt-4.1-mini-2025-04-14".
    model: str
    provider: str | None
    cache_status: str | None
    prompt_tokens: int
    completion_tokens: int
    #: What the gateway billed for this call: 0 for a cache HIT or COALESCED.
    cost_usd: Decimal
    #: Time for the request that answered, not counting rate-limit waits.
    latency_s: float
    request_id: str | None


def _detail(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        detail = None
    return detail if isinstance(detail, str) else response.reason_phrase or "error"


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return max(0.0, float(response.headers["retry-after"]))
    except (KeyError, ValueError):
        return None


class GatewayClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        api_key: str,
        *,
        max_wait_s: float = DEFAULT_MAX_WAIT_S,
        rate_limit_retries: int = DEFAULT_RATE_LIMIT_RETRIES,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._http = http
        self._auth = {"Authorization": f"Bearer {api_key}"}
        self._max_wait_s = max_wait_s
        self._retries = rate_limit_retries
        self._sleep = sleep
        self._clock = clock

    async def chat(
        self,
        model: str,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        no_store: bool = False,
        request_id: str | None = None,
    ) -> ChatReply:
        body: dict[str, object] = {
            "model": model,
            "messages": [m.model_dump() for m in messages],
            "temperature": temperature,
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        headers = dict(self._auth)
        if no_store:
            headers["Cache-Control"] = "no-store"
        if request_id is not None:
            headers["X-Request-ID"] = request_id

        for attempt in range(self._retries + 1):
            start = self._clock()
            try:
                response = await self._http.post(CHAT_PATH, json=body, headers=headers)
            except httpx.TimeoutException:
                raise GatewayError(None, "timed out") from None
            except httpx.HTTPError as exc:
                raise GatewayError(None, f"{type(exc).__name__}") from None
            latency_s = self._clock() - start

            if response.status_code == 429:
                wait = _retry_after(response)
                if (
                    wait is not None
                    and wait <= self._max_wait_s
                    and attempt < self._retries
                ):
                    await self._sleep(wait)
                    continue
            if response.status_code != 200:
                raise GatewayError(response.status_code, _detail(response))
            return self._reply(response, latency_s)

        raise AssertionError("unreachable: the last attempt returns or raises")

    def _reply(self, response: httpx.Response, latency_s: float) -> ChatReply:
        try:
            parsed = ChatCompletionResponse.model_validate(response.json())
        except (ValueError, ValidationError):
            raise GatewayError(response.status_code, "malformed response") from None
        if not parsed.choices:
            raise GatewayError(response.status_code, "response has no choices")

        cache_status = response.headers.get("x-cache")
        usage = parsed.usage
        billed = cache_status is None or cache_status in BILLABLE_STATUSES
        cost = (
            compute_cost(parsed.model, usage.prompt_tokens, usage.completion_tokens)
            if billed
            else None
        )
        return ChatReply(
            text=parsed.choices[0].message.content,
            model=parsed.model,
            provider=response.headers.get("x-provider"),
            cache_status=cache_status,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cost_usd=cost.amount if cost is not None else Decimal(0),
            latency_s=latency_s,
            request_id=response.headers.get("x-request-id"),
        )
