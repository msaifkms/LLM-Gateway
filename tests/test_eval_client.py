import asyncio
import json
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import httpx
import pytest

from app.api.schemas import ChatMessage
from app.evals.client import ChatReply, GatewayClient, GatewayError

KEY = "llmgw_test-key"
HI = [ChatMessage(role="user", content="Hi")]


def completion(
    text: str = "Hello", model: str = "gpt-4.1-mini", prompt: int = 1000, out: int = 500
) -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": out,
            "total_tokens": prompt + out,
        },
    }


class Recorder:
    """Answers each request with the next queued response, and keeps the requests."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def ok(body: dict[str, Any] | None = None, **headers: str) -> httpx.Response:
    return httpx.Response(200, json=body or completion(), headers=headers)


def call(
    handler: Callable[[httpx.Request], httpx.Response],
    sleeps: list[float] | None = None,
    **chat: Any,
) -> ChatReply:
    async def fake_sleep(seconds: float) -> None:
        if sleeps is not None:
            sleeps.append(seconds)

    async def scenario() -> ChatReply:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(base_url="http://gw", transport=transport) as http:
            client = GatewayClient(http, KEY, sleep=fake_sleep)
            return await client.chat("chat-default", HI, **chat)

    return asyncio.run(scenario())


def test_a_reply_carries_text_tokens_cost_and_gateway_headers() -> None:
    handler = Recorder(
        ok(**{"X-Cache": "MISS", "X-Provider": "openai-default", "X-Request-ID": "r1"})
    )

    reply = call(handler)

    assert reply.text == "Hello"
    assert reply.model == "gpt-4.1-mini"
    assert (reply.prompt_tokens, reply.completion_tokens) == (1000, 500)
    # Same price table as the gateway: 1000 * 0.40/M + 500 * 1.60/M.
    assert reply.cost_usd == Decimal("0.0012")
    assert (reply.provider, reply.cache_status, reply.request_id) == (
        "openai-default",
        "MISS",
        "r1",
    )
    assert reply.latency_s >= 0


@pytest.mark.parametrize("status", ["HIT", "COALESCED"])
def test_answers_from_the_cache_cost_nothing(status: str) -> None:
    reply = call(Recorder(ok(**{"X-Cache": status})))

    assert reply.cost_usd == 0
    assert reply.prompt_tokens == 1000  # tokens are still reported


def test_the_request_uses_the_public_api_with_the_key() -> None:
    handler = Recorder(ok())

    call(handler, temperature=0.0, max_tokens=50, request_id="eval-r1-c1")

    request = handler.requests[0]
    assert request.method == "POST"
    assert request.url.path == "/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert request.headers["x-request-id"] == "eval-r1-c1"
    assert "cache-control" not in request.headers
    assert json.loads(request.content) == {
        "model": "chat-default",
        "messages": [{"role": "user", "content": "Hi"}],
        "temperature": 0.0,
        "max_tokens": 50,
    }


def test_no_store_bypasses_the_cache() -> None:
    handler = Recorder(ok())

    call(handler, no_store=True)

    assert handler.requests[0].headers["cache-control"] == "no-store"


def test_a_rate_limit_is_waited_out_using_retry_after() -> None:
    limited = httpx.Response(
        429, json={"detail": "Rate limit"}, headers={"Retry-After": "2"}
    )
    handler = Recorder(limited, limited, ok())
    sleeps: list[float] = []

    reply = call(handler, sleeps)

    assert reply.text == "Hello"
    assert sleeps == [2.0, 2.0]
    assert len(handler.requests) == 3


def test_a_daily_quota_is_not_waited_for() -> None:
    quota = httpx.Response(
        429, json={"detail": "Daily quota reached"}, headers={"Retry-After": "40000"}
    )
    sleeps: list[float] = []

    with pytest.raises(GatewayError) as exc:
        call(Recorder(quota), sleeps)

    assert exc.value.status == 429
    assert exc.value.detail == "Daily quota reached"
    assert sleeps == []
    assert not exc.value.fatal


def test_rate_limit_retries_are_bounded() -> None:
    limited = httpx.Response(429, headers={"Retry-After": "1"})
    handler = Recorder(*[limited] * 6)

    with pytest.raises(GatewayError) as exc:
        call(handler)

    assert exc.value.status == 429
    assert len(handler.requests) == 6  # the first try and five retries


def test_a_bad_key_is_fatal() -> None:
    denied = httpx.Response(401, json={"detail": "Invalid or missing API key"})

    with pytest.raises(GatewayError) as exc:
        call(Recorder(denied))

    assert exc.value.fatal
    assert str(exc.value) == "HTTP 401: Invalid or missing API key"


@pytest.mark.parametrize(
    ("response", "detail"),
    [
        (httpx.Response(502, json={"detail": "Upstream error"}), "Upstream error"),
        (httpx.Response(500, text="oops"), "Internal Server Error"),
        (httpx.Response(200, text="not json"), "malformed response"),
        (httpx.Response(200, json={"choices": []}), "malformed response"),
    ],
)
def test_failures_become_gateway_errors(response: httpx.Response, detail: str) -> None:
    with pytest.raises(GatewayError) as exc:
        call(Recorder(response))

    assert exc.value.detail == detail
    assert not exc.value.fatal


def test_an_empty_choices_list_is_an_error() -> None:
    body = completion()
    body["choices"] = []

    with pytest.raises(GatewayError, match="no choices"):
        call(Recorder(ok(body)))


@pytest.mark.parametrize(
    ("error", "detail"),
    [
        (httpx.ConnectError("refused"), "ConnectError"),
        (httpx.ReadTimeout("slow"), "timed out"),
    ],
)
def test_network_failures_have_no_status(error: Exception, detail: str) -> None:
    with pytest.raises(GatewayError) as exc:
        call(Recorder(error))

    assert exc.value.status is None
    assert exc.value.detail == detail
    assert "refused" not in str(exc.value)  # no transport internals


def test_the_key_never_appears_in_errors() -> None:
    with pytest.raises(GatewayError) as exc:
        call(Recorder(httpx.Response(503, json={"detail": "Service unavailable"})))

    assert KEY not in str(exc.value)
