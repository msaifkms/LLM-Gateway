"""Builds the provider instances the router can call."""

import httpx

from app.providers.anthropic import AnthropicProvider
from app.providers.base import LLMProvider
from app.providers.mock import FailureMode, MockProvider
from app.providers.openai import OpenAIProvider

#: Benchmark-only Mock targets (GATEWAY_BENCH_MODE=true). Each one isolates
#: one thing to measure; see bench/README.md.
BENCH_MOCK_DELAY_S = 0.050
BENCH_PRICED_DELAY_S = 0.200
BENCH_PRICED_MODEL = "gpt-4.1-mini"
BENCH_PRICED_TOKENS = (500, 200)


def bench_providers() -> dict[str, LLMProvider]:
    prompt_tokens, completion_tokens = BENCH_PRICED_TOKENS
    return {
        # A fixed delay, so the gateway's own time is (measured - 50 ms).
        # Reports "mock-1" (priced at $0), like the plain Mock, so no request
        # pays for an "unpriced model" warning that real traffic never sees.
        "mock-delay-50": MockProvider(
            latency_s=BENCH_MOCK_DELAY_S, model_name="mock-1"
        ),
        # A provider that is always down, for fallback and the circuit breaker.
        "mock-down": MockProvider(
            failure_mode=FailureMode.SERVER_ERROR, model_name="mock-1"
        ),
        # Slow enough for identical requests to overlap, and priced like a real
        # model (simulated tokens), so cache savings have a dollar value.
        "mock-priced": MockProvider(
            latency_s=BENCH_PRICED_DELAY_S,
            model_name=BENCH_PRICED_MODEL,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        ),
    }


def build_providers(
    client: httpx.AsyncClient, *, bench: bool = False
) -> dict[str, LLMProvider]:
    """Create every provider, sharing one pooled HTTP client."""
    providers: dict[str, LLMProvider] = {
        "mock-1": MockProvider(),
        "openai-default": OpenAIProvider(client=client),
        "anthropic-default": AnthropicProvider(client=client),
    }
    if bench:
        providers |= bench_providers()
    return providers
