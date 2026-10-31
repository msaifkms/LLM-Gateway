import asyncio
import random
import time
import uuid
from enum import StrEnum

from app.api.schemas import (
    ChatChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    TokenUsage,
)
from app.resilience.errors import (
    ProviderAuthError,
    ProviderBadRequestError,
    ProviderConnectionError,
    ProviderError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderServerError,
    ProviderTimeoutError,
)

PROVIDER_NAME = "mock"


class FailureMode(StrEnum):
    NONE = "none"
    SERVER_ERROR = "server_error"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    RATE_LIMIT = "rate_limit"
    AUTH = "auth"
    BAD_REQUEST = "bad_request"
    MALFORMED = "malformed"


_ERRORS: dict[FailureMode, type[ProviderError]] = {
    FailureMode.SERVER_ERROR: ProviderServerError,
    FailureMode.TIMEOUT: ProviderTimeoutError,
    FailureMode.CONNECTION: ProviderConnectionError,
    FailureMode.RATE_LIMIT: ProviderRateLimitError,
    FailureMode.AUTH: ProviderAuthError,
    FailureMode.BAD_REQUEST: ProviderBadRequestError,
    FailureMode.MALFORMED: ProviderResponseError,
}


class MockProvider:
    """Simulated LLM with configurable latency and failures.

    The attributes are deliberately mutable: a test can flip
    ``failure_mode`` mid-test to simulate a provider recovering.
    """

    def __init__(
        self,
        response_text: str = "Hello from the Mock LLM!",
        *,
        latency_s: float = 0.0,
        failure_mode: FailureMode = FailureMode.NONE,
        failure_rate: float = 1.0,
        retry_after_s: float = 1.0,
        seed: int | None = None,
    ) -> None:
        self.response_text = response_text
        self.latency_s = latency_s
        self.failure_mode = failure_mode
        self.failure_rate = failure_rate
        self.retry_after_s = retry_after_s
        self.calls = 0
        self._rng = random.Random(seed)

    def _build_error(self) -> ProviderError:
        error_cls = _ERRORS[self.failure_mode]
        message = f"mock simulated {self.failure_mode.value}"
        if self.failure_mode is FailureMode.RATE_LIMIT:
            return error_cls(
                message, provider=PROVIDER_NAME, retry_after=self.retry_after_s
            )
        return error_cls(message, provider=PROVIDER_NAME)

    async def complete(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        self.calls += 1

        if self.latency_s > 0:
            await asyncio.sleep(self.latency_s)

        if (
            self.failure_mode is not FailureMode.NONE
            and self._rng.random() < self.failure_rate
        ):
            raise self._build_error()

        return ChatCompletionResponse(
            id=f"chatcmpl-mock-{uuid.uuid4().hex}",
            created=int(time.time()),
            model=request.model,
            choices=[
                ChatChoice(
                    index=0,
                    message=ChatMessage(
                        role="assistant",
                        content=self.response_text,
                    ),
                    finish_reason="stop",
                )
            ],
            usage=TokenUsage(
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
            ),
        )
