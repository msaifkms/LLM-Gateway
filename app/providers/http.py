"""HTTP plumbing shared by provider adapters."""

import math
from typing import Any

import httpx

from app.resilience.errors import (
    ProviderAuthError,
    ProviderBadRequestError,
    ProviderConnectionError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderServerError,
    ProviderTimeoutError,
)


def parse_retry_after(value: str | None) -> float | None:
    """Parse a Retry-After header given in seconds. HTTP-dates are ignored."""
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def raise_for_status(provider: str, response: httpx.Response) -> None:
    """Translate a non-2xx response into the matching ProviderError."""
    if response.is_success:
        return

    status = response.status_code
    message = f"{provider} returned HTTP {status}"  # never include the body

    if status == 429:
        retry_after = parse_retry_after(response.headers.get("retry-after"))
        raise ProviderRateLimitError(
            message, provider=provider, retry_after=retry_after
        )
    if status >= 500:
        raise ProviderServerError(message, provider=provider)
    if status in (401, 403):
        raise ProviderAuthError(message, provider=provider)
    if status >= 400:
        raise ProviderBadRequestError(message, provider=provider)
    raise ProviderResponseError(message, provider=provider)  # unexpected 1xx/3xx


async def post_json(
    client: httpx.AsyncClient,
    *,
    provider: str,
    url: str,
    headers: dict[str, str],
    payload: dict[str, object],
) -> dict[str, Any]:
    """POST JSON and return the decoded body, raising only ProviderError."""
    try:
        response = await client.post(url, headers=headers, json=payload)
    except httpx.TimeoutException as exc:  # must come before RequestError
        raise ProviderTimeoutError(
            f"{provider} request timed out", provider=provider
        ) from exc
    except httpx.RequestError as exc:
        raise ProviderConnectionError(
            f"{provider} connection failed", provider=provider
        ) from exc

    raise_for_status(provider, response)

    try:
        data = response.json()
    except ValueError as exc:  # json.JSONDecodeError is a ValueError
        raise ProviderResponseError(
            f"{provider} returned a non-JSON body", provider=provider
        ) from exc

    if not isinstance(data, dict):
        raise ProviderResponseError(
            f"{provider} returned an unexpected JSON type", provider=provider
        )
    return data
