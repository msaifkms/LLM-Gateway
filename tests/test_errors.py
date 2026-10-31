import pytest

from app.resilience.errors import (
    CircuitOpenError,
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


@pytest.mark.parametrize(
    ("cls", "retryable", "fallback", "status"),
    [
        (ProviderTimeoutError, True, True, 504),
        (ProviderConnectionError, True, True, 502),
        (ProviderRateLimitError, True, True, 503),
        (ProviderServerError, True, True, 502),
        (ProviderAuthError, False, True, 503),
        (ProviderConfigError, False, True, 503),
        (ProviderResponseError, False, True, 502),
        (ProviderBadRequestError, False, False, 400),
        (CircuitOpenError, False, True, 503),
    ],
)
def test_error_policy(
    cls: type[ProviderError], retryable: bool, fallback: bool, status: int
) -> None:
    error = cls("boom", provider="test")

    assert error.retryable is retryable
    assert error.fallback is fallback
    assert error.status_code == status


def test_custom_detail_overrides_default() -> None:
    error = ProviderBadRequestError("x", provider="t", detail="custom")

    assert error.detail == "custom"
    assert str(error) == "x"
