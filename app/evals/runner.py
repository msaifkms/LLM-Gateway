"""Run a dataset's cases through the gateway and score each answer."""

import asyncio
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any

from app.evals.client import GatewayClient, GatewayError
from app.evals.dataset import EvalCase
from app.evals.scorers import Judge, get_scorer


@dataclass(frozen=True)
class RunSettings:
    model: str
    concurrency: int = 4
    temperature: float = 0.0
    max_tokens: int | None = None
    #: Send Cache-Control: no-store, so every answer is fresh and latency is real.
    no_cache: bool = False
    judge_model: str | None = None
    #: Extra details kept with the run (gateway URL, filters); never the key.
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.concurrency < 1:
            raise ValueError("concurrency must be at least 1")

    def as_json(self) -> dict[str, Any]:
        settings = asdict(self)
        settings.update(settings.pop("extra"))
        return settings


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    scorer: str
    score: float
    passed: bool
    tags: list[str] = field(default_factory=list)
    latency_ms: float | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: Decimal = Decimal(0)
    judge_tokens: int = 0
    judge_cost_usd: Decimal = Decimal(0)
    cache_status: str | None = None
    provider: str | None = None
    response_model: str | None = None
    request_id: str | None = None
    output: str | None = None
    reason: str = ""
    #: Set when the gateway or the scorer failed; the case then counts as failed.
    error: str | None = None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def request_id_for(run_tag: str, case_id: str) -> str:
    """An X-Request-ID that finds this case in the gateway's logs (max 64 chars)."""
    return f"eval-{run_tag}-{case_id}"[:64]


async def evaluate_case(
    case: EvalCase,
    settings: RunSettings,
    client: GatewayClient,
    judge: Judge | None,
    run_tag: str,
) -> CaseResult:
    base: dict[str, Any] = {
        "case_id": case.id,
        "scorer": case.scorer,
        "tags": list(case.tags),
    }
    try:
        reply = await client.chat(
            settings.model,
            case.chat_messages(),
            temperature=settings.temperature,
            max_tokens=case.max_tokens or settings.max_tokens,
            no_store=settings.no_cache,
            request_id=request_id_for(run_tag, case.id),
        )
    except GatewayError as exc:
        if exc.fatal:
            raise
        return CaseResult(**base, score=0.0, passed=False, error=str(exc))

    answered: dict[str, Any] = {
        **base,
        "latency_ms": reply.latency_s * 1000,
        "prompt_tokens": reply.prompt_tokens,
        "completion_tokens": reply.completion_tokens,
        "cost_usd": reply.cost_usd,
        "cache_status": reply.cache_status,
        "provider": reply.provider,
        "response_model": reply.model,
        "request_id": reply.request_id,
        "output": reply.text,
    }
    try:
        scored = await get_scorer(case.scorer).score(case, reply.text, judge)
    except GatewayError as exc:
        if exc.fatal:
            raise
        return CaseResult(**answered, score=0.0, passed=False, error=f"judge: {exc}")
    except (ValueError, TypeError) as exc:  # includes JudgeError
        return CaseResult(**answered, score=0.0, passed=False, error=f"scorer: {exc}")

    verdict = scored.judge
    return CaseResult(
        **answered,
        score=scored.score,
        passed=scored.passed,
        reason=scored.reason,
        judge_tokens=(
            verdict.prompt_tokens + verdict.completion_tokens if verdict else 0
        ),
        judge_cost_usd=verdict.cost_usd if verdict else Decimal(0),
    )


def needs_judge(cases: list[EvalCase]) -> bool:
    return any(get_scorer(case.scorer).needs_judge for case in cases)


async def run_cases(
    cases: list[EvalCase],
    settings: RunSettings,
    client: GatewayClient,
    *,
    judge: Judge | None = None,
    run_tag: str = "run",
    on_result: Callable[[CaseResult], None] | None = None,
) -> list[CaseResult]:
    """Evaluate every case, at most ``settings.concurrency`` at a time.

    Results come back in dataset order. A fatal gateway error (a bad key)
    cancels the remaining cases and is raised.
    """
    if judge is None and needs_judge(cases):
        raise ValueError("this dataset has judge cases; set a judge model")

    limit = asyncio.Semaphore(settings.concurrency)

    async def one(case: EvalCase) -> CaseResult:
        async with limit:
            result = await evaluate_case(case, settings, client, judge, run_tag)
        if on_result is not None:
            on_result(result)
        return result

    try:
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(one(case)) for case in cases]
    except ExceptionGroup as group:
        # TaskGroup wraps the first failure; raise it as itself.
        raise group.exceptions[0] from None
    return [task.result() for task in tasks]
