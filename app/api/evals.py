"""Read-only eval results for the calling user (the web UI's Evals page).

    GET /v1/evals/runs?limit=20   the caller's recent runs
    GET /v1/evals/runs/{id}       one of the caller's runs, with every case

A run belongs to the owner of the API key the eval CLI ran with. Other users'
runs, and runs with no owner, are reported as not found, so a caller cannot
tell whether a run id exists.
"""

from datetime import datetime
from decimal import Decimal
from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel

from app.api.auth import authenticate
from app.api.usage import Money
from app.auth.store import Principal
from app.evals.report import RunInfo, summarize
from app.evals.runner import CaseResult
from app.evals.store import UserEvalStore

router = APIRouter(prefix="/v1/evals")

DB_ERRORS = (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError)


def get_eval_store(request: Request) -> UserEvalStore | None:
    store: UserEvalStore | None = getattr(request.app.state, "eval_store", None)
    return store


def unavailable() -> HTTPException:
    return HTTPException(status_code=503, detail="Eval results are unavailable")


class RunOut(BaseModel):
    id: int
    dataset: str
    model: str
    judge_model: str | None
    status: str
    started_at: datetime | None
    finished_at: datetime | None
    cases: int
    passed: int
    #: None for a run with no results (still running, or failed at once).
    pass_rate: float | None


class RunList(BaseModel):
    runs: list[RunOut]


class SummaryOut(BaseModel):
    errors: int
    mean_score: float
    p50_ms: float | None
    p95_ms: float | None
    prompt_tokens: int
    completion_tokens: int
    judge_tokens: int
    cost_usd: Money
    judge_cost_usd: Money


class ResultOut(BaseModel):
    case_id: str
    scorer: str
    tags: list[str]
    passed: bool
    score: float
    latency_ms: float | None
    prompt_tokens: int
    completion_tokens: int
    cost_usd: Money
    cache_status: str | None
    provider: str | None
    output: str | None
    reason: str
    error: str | None


class RunDetail(BaseModel):
    run: RunOut
    summary: SummaryOut
    results: list[ResultOut]


def run_out(info: RunInfo, cases: int, passed: int) -> RunOut:
    return RunOut(
        id=info.id or 0,
        dataset=info.dataset,
        model=info.model,
        judge_model=info.judge_model,
        status=info.status,
        started_at=info.started_at,
        finished_at=info.finished_at,
        cases=cases,
        passed=passed,
        pass_rate=passed / cases if cases else None,
    )


def result_out(r: CaseResult) -> ResultOut:
    return ResultOut(
        case_id=r.case_id,
        scorer=r.scorer,
        tags=list(r.tags),
        passed=r.passed,
        score=r.score,
        latency_ms=r.latency_ms,
        prompt_tokens=r.prompt_tokens,
        completion_tokens=r.completion_tokens,
        cost_usd=Decimal(r.cost_usd),
        cache_status=r.cache_status,
        provider=r.provider,
        output=r.output,
        reason=r.reason,
        error=r.error,
    )


@router.get("/runs", response_model=RunList)
async def list_runs(
    principal: Annotated[Principal, Depends(authenticate)],
    store: Annotated[UserEvalStore | None, Depends(get_eval_store)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> RunList:
    if store is None:
        raise unavailable()
    try:
        listings = await store.list_runs_for_user(principal.user_id, limit=limit)
    except DB_ERRORS:
        raise unavailable() from None
    return RunList(runs=[run_out(x.info, x.cases, x.passed) for x in listings])


@router.get("/runs/{run_id}", response_model=RunDetail)
async def get_run(
    principal: Annotated[Principal, Depends(authenticate)],
    store: Annotated[UserEvalStore | None, Depends(get_eval_store)],
    run_id: Annotated[int, Path(ge=1)],
) -> RunDetail:
    if store is None:
        raise unavailable()
    try:
        info = await store.get_run_for_user(principal.user_id, run_id)
        if info is None:
            raise HTTPException(status_code=404, detail="Eval run not found")
        results = await store.get_results(run_id)
    except DB_ERRORS:
        raise unavailable() from None

    summary = summarize(results)
    return RunDetail(
        run=run_out(info, summary.cases, summary.passed),
        summary=SummaryOut(
            errors=summary.errors,
            mean_score=summary.mean_score,
            p50_ms=summary.p50_ms,
            p95_ms=summary.p95_ms,
            prompt_tokens=summary.prompt_tokens,
            completion_tokens=summary.completion_tokens,
            judge_tokens=summary.judge_tokens,
            cost_usd=summary.cost_usd,
            judge_cost_usd=summary.judge_cost_usd,
        ),
        results=[result_out(r) for r in results],
    )
