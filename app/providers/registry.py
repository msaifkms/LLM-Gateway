"""Builds the provider instances the router can call."""

import httpx

from app.providers.anthropic import AnthropicProvider
from app.providers.base import LLMProvider
from app.providers.mock import MockProvider
from app.providers.openai import OpenAIProvider


def build_providers(client: httpx.AsyncClient) -> dict[str, LLMProvider]:
    """Create every provider, sharing one pooled HTTP client."""
    return {
        "mock-1": MockProvider(),
        "openai-default": OpenAIProvider(client=client),
        "anthropic-default": AnthropicProvider(client=client),
    }
