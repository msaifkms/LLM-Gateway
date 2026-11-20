"""Eval runs and per-case results in Postgres (tables from migration 004)."""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import asyncpg

from app.auth.store import PostgresKeyStore
from app.evals.report import RunInfo
from app.evals.runner import CaseResult


@dataclass(frozen=True)
class RunListing:
    info: RunInfo
    cases: int
    passed: int


@dataclass(frozen=True)
class LatestScore:
    """The most recent completed run of one dataset on one model, for /metrics."""

    dataset: str
    model: str
    pass_rate: float
    mean_score: float
    finished_at: datetime


class EvalStore(Protocol):
    async def create_run(self, info: RunInfo) -> RunInfo: ...

    async def finish_run(
        self, run_id: int, status: str, results: Sequence[CaseResult]
    ) -> RunInfo: ...

    async def get_run(self, run_id: int) -> RunInfo | None: ...

    async def get_results(self, run_id: int) -> list[CaseResult]: ...

    async def list_runs(
        self, *, dataset: str | None = None, limit: int = 20
    ) -> list[RunListing]: ...

    async def owner_of_key(self, key_hash: str) -> int | None: ...


class UserEvalStore(Protocol):
    """Read-only and scoped to one user: what GET /v1/evals/runs needs."""

    async def list_runs_for_user(
        self, user_id: int, *, limit: int = 20
    ) -> list[RunListing]: ...

    async def get_run_for_user(self, user_id: int, run_id: int) -> RunInfo | None: ...

    async def get_results(self, run_id: int) -> list[CaseResult]: ...


INSERT_RUN_SQL = """
INSERT INTO eval_runs (
    dataset, dataset_sha256, model, judge_model, batch_id, settings, user_id
)
VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)
RETURNING *
"""

INSERT_RESULT_SQL = """
INSERT INTO eval_results (
    run_id, case_id, scorer, tags, score, passed, latency_ms,
    prompt_tokens, completion_tokens, cost_usd, judge_tokens, judge_cost_usd,
    cache_status, provider, response_model, request_id, output, reason, error
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
          $13, $14, $15, $16, $17, $18, $19)
"""

FINISH_RUN_SQL = """
UPDATE eval_runs SET status = $2, finished_at = now()
WHERE id = $1 AND status = 'running'
RETURNING *
"""

LIST_RUNS_SQL = """
SELECT r.*,
       count(e.id)::int AS cases,
       (count(e.id) FILTER (WHERE e.passed))::int AS passed
FROM eval_runs AS r
LEFT JOIN eval_results AS e ON e.run_id = r.id
WHERE ($1::text IS NULL OR r.dataset = $1)
GROUP BY r.id
ORDER BY r.id DESC
LIMIT $2
"""

#: One user's runs only. NULL owners never match, so unowned runs stay hidden.
LIST_USER_RUNS_SQL = """
SELECT r.*,
       count(e.id)::int AS cases,
       (count(e.id) FILTER (WHERE e.passed))::int AS passed
FROM eval_runs AS r
LEFT JOIN eval_results AS e ON e.run_id = r.id
WHERE r.user_id = $1
GROUP BY r.id
ORDER BY r.id DESC
LIMIT $2
"""

#: The latest completed run per (dataset, model) in the last 30 days. The label
#: set on /metrics is bounded by the LIMIT whatever is in the table.
LATEST_SCORES_SQL = """
SELECT DISTINCT ON (r.dataset, r.model)
       r.dataset, r.model, r.finished_at,
       s.cases, s.passed, s.mean_score
FROM eval_runs AS r
CROSS JOIN LATERAL (
    SELECT count(*)::int AS cases,
           (count(*) FILTER (WHERE passed))::int AS passed,
           COALESCE(avg(score), 0)::float8 AS mean_score
    FROM eval_results WHERE run_id = r.id
) AS s
WHERE r.status = 'completed'
  AND r.finished_at >= now() - interval '30 days'
  AND s.cases > 0
ORDER BY r.dataset, r.model, r.finished_at DESC
LIMIT $1
"""

MAX_SCOREBOARD_SERIES = 100


def _info(row: Any) -> RunInfo:
    settings = row["settings"]
    return RunInfo(
        id=row["id"],
        dataset=row["dataset"],
        dataset_sha256=row["dataset_sha256"],
        model=row["model"],
        judge_model=row["judge_model"],
        batch_id=row["batch_id"],
        status=row["status"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        settings=json.loads(settings) if isinstance(settings, str) else settings,
        user_id=row["user_id"],
    )


def _result(row: Any) -> CaseResult:
    return CaseResult(
        case_id=row["case_id"],
        scorer=row["scorer"],
        tags=list(row["tags"]),
        score=row["score"],
        passed=row["passed"],
        latency_ms=row["latency_ms"],
        prompt_tokens=row["prompt_tokens"],
        completion_tokens=row["completion_tokens"],
        cost_usd=row["cost_usd"],
        judge_tokens=row["judge_tokens"],
        judge_cost_usd=row["judge_cost_usd"],
        cache_status=row["cache_status"],
        provider=row["provider"],
        response_model=row["response_model"],
        request_id=row["request_id"],
        output=row["output"],
        reason=row["reason"],
        error=row["error"],
    )


class PostgresEvalStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def create_run(self, info: RunInfo) -> RunInfo:
        row = await self._pool.fetchrow(
            INSERT_RUN_SQL,
            info.dataset,
            info.dataset_sha256,
            info.model,
            info.judge_model,
            info.batch_id,
            json.dumps(info.settings),
            info.user_id,
        )
        return _info(row)

    async def finish_run(
        self, run_id: int, status: str, results: Sequence[CaseResult]
    ) -> RunInfo:
        """Write every result and close the run, all or nothing."""
        async with self._pool.acquire() as conn, conn.transaction():
            # Close the run first: if it was already closed, raising here rolls
            # back before any result is written.
            row = await conn.fetchrow(FINISH_RUN_SQL, run_id, status)
            if row is None:
                raise ValueError(f"run {run_id} does not exist or is already finished")
            await conn.executemany(
                INSERT_RESULT_SQL,
                [
                    (
                        run_id,
                        r.case_id,
                        r.scorer,
                        r.tags,
                        r.score,
                        r.passed,
                        r.latency_ms,
                        r.prompt_tokens,
                        r.completion_tokens,
                        r.cost_usd,
                        r.judge_tokens,
                        r.judge_cost_usd,
                        r.cache_status,
                        r.provider,
                        r.response_model,
                        r.request_id,
                        r.output,
                        r.reason,
                        r.error,
                    )
                    for r in results
                ],
            )
        return _info(row)

    async def get_run(self, run_id: int) -> RunInfo | None:
        row = await self._pool.fetchrow("SELECT * FROM eval_runs WHERE id = $1", run_id)
        return _info(row) if row is not None else None

    async def get_results(self, run_id: int) -> list[CaseResult]:
        rows = await self._pool.fetch(
            "SELECT * FROM eval_results WHERE run_id = $1 ORDER BY id", run_id
        )
        return [_result(row) for row in rows]

    async def list_runs(
        self, *, dataset: str | None = None, limit: int = 20
    ) -> list[RunListing]:
        rows = await self._pool.fetch(LIST_RUNS_SQL, dataset, limit)
        return [RunListing(_info(r), r["cases"], r["passed"]) for r in rows]

    async def owner_of_key(self, key_hash: str) -> int | None:
        """The user behind an active key, with the same rules as authentication."""
        principal = await PostgresKeyStore(self._pool).lookup(key_hash)
        return principal.user_id if principal is not None else None

    async def list_runs_for_user(
        self, user_id: int, *, limit: int = 20
    ) -> list[RunListing]:
        rows = await self._pool.fetch(LIST_USER_RUNS_SQL, user_id, limit)
        return [RunListing(_info(r), r["cases"], r["passed"]) for r in rows]

    async def get_run_for_user(self, user_id: int, run_id: int) -> RunInfo | None:
        row = await self._pool.fetchrow(
            "SELECT * FROM eval_runs WHERE id = $1 AND user_id = $2", run_id, user_id
        )
        return _info(row) if row is not None else None

    async def latest_scores(self) -> list[LatestScore]:
        rows = await self._pool.fetch(LATEST_SCORES_SQL, MAX_SCOREBOARD_SERIES)
        return [
            LatestScore(
                dataset=r["dataset"],
                model=r["model"],
                pass_rate=r["passed"] / r["cases"],
                mean_score=r["mean_score"],
                finished_at=r["finished_at"],
            )
            for r in rows
        ]
