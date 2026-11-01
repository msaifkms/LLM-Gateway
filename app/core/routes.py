"""Route table: model alias -> ordered provider targets.

Hardcoded for now; the Route dataclass is what a YAML loader will build later.
"""

from dataclasses import dataclass
from enum import StrEnum


class CacheMode(StrEnum):
    #: Cache temperature-0 requests, but only if every target honors temperature.
    AUTO = "auto"
    #: Cache temperature-0 requests even if a target ignores temperature.
    #: An explicit opt-in: the route author accepts that one sampled answer is reused.
    TEMPERATURE_ZERO = "temperature_zero"
    #: Never cache this route.
    NEVER = "never"


@dataclass(frozen=True)
class Route:
    targets: tuple[str, ...]  # provider names, in priority order
    timeout_s: float = 20.0  # per attempt
    max_retries: int = 2  # retries per target
    deadline_s: float = 45.0  # whole request, across all targets
    cache: CacheMode = CacheMode.AUTO

    def __post_init__(self) -> None:
        if not self.targets:
            raise ValueError("a route needs at least one target")
        if self.timeout_s <= 0 or self.deadline_s <= 0:
            raise ValueError("timeout_s and deadline_s must be > 0")
        if self.max_retries < 0:
            raise ValueError("max_retries must be >= 0")


DEFAULT_ROUTES: dict[str, Route] = {
    "chat-default": Route(targets=("anthropic-default", "openai-default")),
    "chat-test": Route(targets=("mock-1",)),
    "mock-1": Route(targets=("mock-1",)),
    "openai-default": Route(targets=("openai-default",)),
    "anthropic-default": Route(targets=("anthropic-default",)),
}
