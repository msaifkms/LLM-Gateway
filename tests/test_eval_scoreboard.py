import asyncio
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient
from helpers import FakeClock

from app.api.metrics import router as metrics_router
from app.core import metrics
from app.evals.scoreboard import EvalScoreboard
from app.evals.store import LatestScore

FINISHED = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


def score(dataset: str = "factual_qa", model: str = "chat-default") -> LatestScore:
    return LatestScore(
        dataset, model, pass_rate=0.8, mean_score=0.85, finished_at=FINISHED
    )


class FakeSource:
    def __init__(self, scores: list[LatestScore]) -> None:
        self.scores = scores
        self.reads = 0
        self.fail = False

    async def latest_scores(self) -> list[LatestScore]:
        self.reads += 1
        if self.fail:
            raise OSError("database down")
        return self.scores


def sample(name: str, labels: dict[str, str]) -> float | None:
    return metrics.REGISTRY.get_sample_value(name, labels)


def test_scores_are_read_at_most_once_per_ttl() -> None:
    source, clock = FakeSource([score()]), FakeClock()
    board = EvalScoreboard(source, ttl_s=30, clock=clock)

    async def scenario() -> None:
        assert await board.latest() == [score()]
        clock.now = 29
        await board.latest()
        assert source.reads == 1
        clock.now = 31
        await board.latest()
        assert source.reads == 2

    asyncio.run(scenario())


def test_a_failed_read_keeps_the_last_scores_and_waits_a_ttl_to_retry() -> None:
    source, clock = FakeSource([score()]), FakeClock()
    board = EvalScoreboard(source, ttl_s=30, clock=clock)

    async def scenario() -> None:
        await board.latest()
        source.fail = True
        clock.now = 31
        assert await board.latest() == [score()]
        clock.now = 40
        await board.latest()
        assert source.reads == 2  # no query per scrape while the database is down

    asyncio.run(scenario())


def test_before_any_successful_read_there_is_nothing() -> None:
    source = FakeSource([])
    source.fail = True

    assert asyncio.run(EvalScoreboard(source).latest()) is None


def test_update_replaces_every_series() -> None:
    labels = {"dataset": "old_set", "model": "m"}
    metrics.update_eval_scores([score("old_set", "m")])
    assert sample("gateway_eval_pass_rate", labels) == 0.8

    metrics.update_eval_scores([score("factual_qa", "chat-default")])

    assert sample("gateway_eval_pass_rate", labels) is None  # gone, not stale
    new = {"dataset": "factual_qa", "model": "chat-default"}
    assert sample("gateway_eval_pass_rate", new) == 0.8
    assert sample("gateway_eval_mean_score", new) == 0.85
    assert (
        sample("gateway_eval_last_run_timestamp_seconds", new) == FINISHED.timestamp()
    )


def test_the_metrics_endpoint_publishes_eval_scores() -> None:
    app = FastAPI()
    app.include_router(metrics_router)
    app.state.eval_scoreboard = EvalScoreboard(FakeSource([score("json_output", "x")]))

    body = TestClient(app).get("/metrics").text

    assert 'gateway_eval_pass_rate{dataset="json_output",model="x"} 0.8' in body
    assert 'gateway_eval_mean_score{dataset="json_output",model="x"} 0.85' in body


def test_an_unreadable_scoreboard_leaves_the_gauges_alone() -> None:
    metrics.update_eval_scores([score("kept", "m")])
    source = FakeSource([])
    source.fail = True
    app = FastAPI()
    app.include_router(metrics_router)
    app.state.eval_scoreboard = EvalScoreboard(source)

    assert TestClient(app).get("/metrics").status_code == 200
    assert sample("gateway_eval_pass_rate", {"dataset": "kept", "model": "m"}) == 0.8
