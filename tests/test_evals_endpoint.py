"""GET /v1/evals/runs and /v1/evals/runs/{id}: the caller's own eval runs only."""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import asyncpg
import pytest
from conftest import TEST_PRINCIPAL
from fake_eval_store import FakeEvalStore
from fastapi.testclient import TestClient

from app.api.auth import authenticate
from app.api.evals import get_eval_store
from app.evals.report import RunInfo
from app.evals.runner import CaseResult
from app.main import app

ME = TEST_PRINCIPAL.user_id
SOMEONE_ELSE = ME + 1


def case(case_id: str, passed: bool, **kw: object) -> CaseResult:
    fields: dict[str, object] = {
        "score": 1.0 if passed else 0.0,
        "latency_ms": 40.0,
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "cost_usd": Decimal("0.00001000"),
        "cache_status": "BYPASS",
        "provider": "mock-1",
        "output": "Hello from the Mock LLM!",
        "reason": "" if passed else "expected 'x'",
        **kw,
    }
    return CaseResult(case_id=case_id, scorer="exact", passed=passed, **fields)  # type: ignore[arg-type]


async def add_run(
    store: FakeEvalStore,
    user_id: int | None,
    results: list[CaseResult],
    dataset: str = "mock_smoke",
) -> int:
    run = await store.create_run(
        RunInfo(dataset=dataset, model="mock-1", user_id=user_id)
    )
    assert run.id is not None
    await store.finish_run(run.id, "completed", results)
    return run.id


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeEvalStore:
    import asyncio

    fake = FakeEvalStore()

    async def seed() -> None:
        await add_run(fake, ME, [case("a", True), case("b", False)])  # run 1
        await add_run(fake, SOMEONE_ELSE, [case("c", True)], "factual_qa")  # 2
        await add_run(fake, None, [case("d", True)], "json_output")  # 3, unowned
        await add_run(fake, ME, [case("e", True)], "json_output")  # 4

    asyncio.run(seed())
    monkeypatch.setitem(app.dependency_overrides, get_eval_store, lambda: fake)
    return fake


def test_the_list_holds_only_the_callers_runs_newest_first(
    client: TestClient, store: FakeEvalStore
) -> None:
    response = client.get("/v1/evals/runs")

    assert response.status_code == 200
    runs = response.json()["runs"]
    assert [r["id"] for r in runs] == [4, 1]
    first = runs[1]
    assert first["dataset"] == "mock_smoke"
    assert first["model"] == "mock-1"
    assert (first["cases"], first["passed"], first["pass_rate"]) == (2, 1, 0.5)
    assert first["status"] == "completed"
    assert first["finished_at"] is not None


def test_the_list_respects_the_limit(client: TestClient, store: FakeEvalStore) -> None:
    assert [r["id"] for r in client.get("/v1/evals/runs?limit=1").json()["runs"]] == [4]


@pytest.mark.parametrize("limit", ["0", "101", "x"])
def test_bad_limits_are_rejected(
    client: TestClient, store: FakeEvalStore, limit: str
) -> None:
    assert client.get(f"/v1/evals/runs?limit={limit}").status_code == 422


def test_a_run_with_no_results_has_no_pass_rate(
    client: TestClient, store: FakeEvalStore
) -> None:
    import asyncio

    asyncio.run(store.create_run(RunInfo(dataset="d", model="m", user_id=ME)))

    newest = client.get("/v1/evals/runs").json()["runs"][0]
    assert (newest["status"], newest["cases"], newest["pass_rate"]) == (
        "running",
        0,
        None,
    )


def test_one_run_has_a_summary_and_every_case(
    client: TestClient, store: FakeEvalStore
) -> None:
    response = client.get("/v1/evals/runs/1")

    assert response.status_code == 200
    body = response.json()
    assert body["run"]["id"] == 1
    assert body["run"]["pass_rate"] == 0.5
    assert body["summary"]["mean_score"] == 0.5
    assert body["summary"]["cost_usd"] == "0.00002000"
    assert body["summary"]["p50_ms"] == 40.0
    assert [r["case_id"] for r in body["results"]] == ["a", "b"]
    failed = body["results"][1]
    assert failed["passed"] is False
    assert failed["reason"] == "expected 'x'"
    assert failed["output"] == "Hello from the Mock LLM!"
    assert failed["cost_usd"] == "0.00001000"


@pytest.mark.parametrize("run_id", [2, 3, 999])
def test_other_users_and_unowned_runs_look_like_missing_runs(
    client: TestClient, store: FakeEvalStore, run_id: int
) -> None:
    response = client.get(f"/v1/evals/runs/{run_id}")

    assert response.status_code == 404
    assert response.json() == {"detail": "Eval run not found"}


def test_the_run_id_must_be_positive(client: TestClient, store: FakeEvalStore) -> None:
    assert client.get("/v1/evals/runs/0").status_code == 422


@pytest.mark.parametrize("path", ["/v1/evals/runs", "/v1/evals/runs/1"])
def test_eval_results_need_an_api_key(
    client: TestClient,
    store: FakeEvalStore,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    monkeypatch.delitem(app.dependency_overrides, authenticate)

    response = client.get(path)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("path", ["/v1/evals/runs", "/v1/evals/runs/1"])
def test_eval_results_are_503_without_a_database(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    monkeypatch.setitem(app.dependency_overrides, get_eval_store, lambda: None)

    response = client.get(path)

    assert response.status_code == 503
    assert response.json() == {"detail": "Eval results are unavailable"}


class BrokenStore(FakeEvalStore):
    async def list_runs_for_user(self, user_id: int, *, limit: int = 20):  # type: ignore[no-untyped-def]
        raise asyncpg.PostgresConnectionError("down")

    async def get_run_for_user(self, user_id: int, run_id: int):  # type: ignore[no-untyped-def]
        raise OSError("down")


@pytest.mark.parametrize("path", ["/v1/evals/runs", "/v1/evals/runs/1"])
def test_eval_results_are_503_when_the_database_errors(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    monkeypatch.setitem(app.dependency_overrides, get_eval_store, BrokenStore)

    assert client.get(path).status_code == 503


def test_dates_are_iso_8601(client: TestClient, store: FakeEvalStore) -> None:
    started = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    store.runs[1] = replace(store.runs[1], started_at=started)

    body = client.get("/v1/evals/runs/1").json()
    assert body["run"]["started_at"] == "2026-11-20T12:00:00Z"
