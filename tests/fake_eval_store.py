"""An in-memory stand-in for PostgresEvalStore, for tests."""

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime

from app.evals.report import RunInfo
from app.evals.runner import CaseResult
from app.evals.store import RunListing


class FakeEvalStore:
    def __init__(self) -> None:
        self.runs: dict[int, RunInfo] = {}
        self.results: dict[int, list[CaseResult]] = {}

    async def create_run(self, info: RunInfo) -> RunInfo:
        run_id = len(self.runs) + 1
        stored = replace(
            info, id=run_id, status="running", started_at=datetime.now(UTC)
        )
        self.runs[run_id] = stored
        return stored

    async def finish_run(
        self, run_id: int, status: str, results: Sequence[CaseResult]
    ) -> RunInfo:
        run = self.runs[run_id]
        if run.status != "running":
            raise ValueError(f"run {run_id} does not exist or is already finished")
        self.results[run_id] = list(results)
        finished = replace(run, status=status, finished_at=datetime.now(UTC))
        self.runs[run_id] = finished
        return finished

    async def get_run(self, run_id: int) -> RunInfo | None:
        return self.runs.get(run_id)

    async def get_results(self, run_id: int) -> list[CaseResult]:
        return list(self.results.get(run_id, []))

    async def list_runs(
        self, *, dataset: str | None = None, limit: int = 20
    ) -> list[RunListing]:
        runs = [
            run
            for run in sorted(self.runs.values(), key=lambda r: -(r.id or 0))
            if dataset is None or run.dataset == dataset
        ]
        listings = []
        for run in runs[:limit]:
            results = self.results.get(run.id or 0, [])
            listings.append(
                RunListing(run, len(results), sum(r.passed for r in results))
            )
        return listings
