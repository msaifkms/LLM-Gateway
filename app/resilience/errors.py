"""Provider error taxonomy.

Every failure from a provider adapter is translated into one of these classes.
Later layers (retry, circuit breaker, router) decide what to do by reading the
class attributes, never by inspecting httpx exceptions.
"""


class ProviderError(Exception):
    """Base class for every error raised by a provider adapter."""

    #: Retry the same provider (transient failure)?
    retryable: bool = False
    #: After retries are exhausted, try the next provider in the route?
    fallback: bool = True
    #: HTTP status the gateway returns to its own client.
    status_code: int = 502
    #: Safe, generic text for clients. The exception message is for logs.
    default_detail: str = "Upstream provider error"

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        retry_after: float | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.retry_after = retry_after
        self.detail = detail or self.default_detail


class ProviderTimeoutError(ProviderError):
    retryable = True
    status_code = 504
    default_detail = "Upstream provider timed out"


class ProviderConnectionError(ProviderError):
    retryable = True
    status_code = 502
    default_detail = "Upstream provider connection failed"


class ProviderRateLimitError(ProviderError):
    retryable = True
    status_code = 503
    default_detail = "Upstream provider rate limit exceeded"


class ProviderServerError(ProviderError):
    retryable = True
    status_code = 502
    default_detail = "Upstream provider error"


class ProviderAuthError(ProviderError):
    status_code = 503
    default_detail = "Upstream provider unavailable"


class ProviderConfigError(ProviderError):
    status_code = 503
    default_detail = "Provider configuration unavailable"


class ProviderResponseError(ProviderError):
    status_code = 502
    default_detail = "Upstream provider returned an invalid response"


class ProviderBadRequestError(ProviderError):
    fallback = False
    status_code = 400
    default_detail = "Upstream provider rejected the request"


class CircuitOpenError(ProviderError):
    """Raised by the circuit breaker itself, without calling the provider.

    Not retryable (retrying an open circuit is pointless) but the router
    should fall back to the next provider.
    """

    status_code = 503
    default_detail = "Upstream provider temporarily unavailable"
