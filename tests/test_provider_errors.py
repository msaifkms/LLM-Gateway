import asyncio
from collections.abc import Callable

import httpx
import pytest

from app.api.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
)
from app.providers.anthropic import AnthropicProvider
from app.providers.http import parse_retry_after
from app.providers.openai import OpenAIProvider
from app.resilience.errors import (
    ProviderAuthError,
    ProviderBadRequestError,
    ProviderConfigError,
    ProviderConnectionError,
    ProviderError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderServerError,
    ProviderTimeoutError,
)

Handler = Callable[[httpx.Request], httpx.Response]

USER_REQUEST = ChatCompletionRequest(
    model="test",
    messages=[ChatMessage(role="user", content="Hi")],
)


def call_openai(handler: Handler) -> ChatCompletionResponse:
    async def run() -> ChatCompletionResponse:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            provider = OpenAIProvider(
                api_key="test-key", model="gpt-4.1-mini", client=client
            )
            return await provider.complete(USER_REQUEST)

    return asyncio.run(run())


def call_anthropic(handler: Handler) -> ChatCompletionResponse:
    async def run() -> ChatCompletionResponse:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            provider = AnthropicProvider(
                api_key="test-key", model="claude-haiku-5-5", client=client
            )
            return await provider.complete(USER_REQUEST)

    return asyncio.run(run())


ADAPTERS = pytest.mark.parametrize("call", [call_openai, call_anthropic])


def raise_timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("simulated timeout", request=request)


def raise_connect_error(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("simulated connect error", request=request)


@ADAPTERS
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (400, ProviderBadRequestError),
        (401, ProviderAuthError),
        (403, ProviderAuthError),
        (404, ProviderBadRequestError),
        (429, ProviderRateLimitError),
        (500, ProviderServerError),
        (503, ProviderServerError),
        (529, ProviderServerError),
    ],
)
def test_http_status_translation(
    call: Callable[[Handler], ChatCompletionResponse],
    status: int,
    expected: type[ProviderError],
) -> None:
    with pytest.raises(expected):
        call(lambda request: httpx.Response(status))


@ADAPTERS
def test_retry_after_header_is_parsed(
    call: Callable[[Handler], ChatCompletionResponse],
) -> None:
    with pytest.raises(ProviderRateLimitError) as info:
        call(lambda request: httpx.Response(429, headers={"Retry-After": "7"}))

    assert info.value.retry_after == 7.0
    assert info.value.retryable is True


def test_parse_retry_after_edge_cases() -> None:
    assert parse_retry_after(None) is None
    assert parse_retry_after("2.5") == 2.5
    assert parse_retry_after("-1") is None
    assert parse_retry_after("nan") is None
    assert parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT") is None


@ADAPTERS
def test_timeout_is_translated(
    call: Callable[[Handler], ChatCompletionResponse],
) -> None:
    with pytest.raises(ProviderTimeoutError):
        call(raise_timeout)


@ADAPTERS
def test_connection_error_is_translated(
    call: Callable[[Handler], ChatCompletionResponse],
) -> None:
    with pytest.raises(ProviderConnectionError):
        call(raise_connect_error)


@ADAPTERS
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={}),
    ],
    ids=["non-json", "json-array", "empty-object"],
)
def test_malformed_success_body_is_a_response_error(
    call: Callable[[Handler], ChatCompletionResponse],
    response: httpx.Response,
) -> None:
    with pytest.raises(ProviderResponseError):
        call(lambda request: response)


@pytest.mark.parametrize(
    ("provider_cls", "env_var"),
    [
        (OpenAIProvider, "OPENAI_API_KEY"),
        (AnthropicProvider, "ANTHROPIC_API_KEY"),
    ],
)
def test_missing_api_key(
    monkeypatch: pytest.MonkeyPatch,
    provider_cls: type[OpenAIProvider] | type[AnthropicProvider],
    env_var: str,
) -> None:
    monkeypatch.delenv(env_var, raising=False)
    provider = provider_cls(api_key=None)

    with pytest.raises(ProviderConfigError):
        asyncio.run(provider.complete(USER_REQUEST))


def test_anthropic_rejects_system_only_conversation() -> None:
    provider = AnthropicProvider(api_key="test-key")
    request = ChatCompletionRequest(
        model="test",
        messages=[ChatMessage(role="system", content="Be brief.")],
    )

    with pytest.raises(ProviderBadRequestError) as info:
        asyncio.run(provider.complete(request))

    assert "non-system" in info.value.detail
