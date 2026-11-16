import asyncio
from decimal import Decimal
from typing import Any

import pytest

from app.api.schemas import ChatMessage
from app.evals.client import ChatReply, GatewayError
from app.evals.dataset import EvalCase
from app.evals.runner import (
    CaseResult,
    RunSettings,
    needs_judge,
    request_id_for,
    run_cases,
)
from app.evals.scorers import JudgeVerdict


def make(case_id: str, expected: str = "Hello", **fields: Any) -> EvalCase:
    raw = {"id": case_id, "prompt": f"say {expected}", "scorer": "exact", **fields}
    if "rubric" in fields:
        raw["scorer"] = "judge"
    else:
        raw["expected"] = expected
    return EvalCase.model_validate(raw)


class FakeClient:
    """Stands in for GatewayClient: answers from a script and tracks concurrency."""

    def __init__(
        self,
        answers: dict[str, str | GatewayError] | None = None,
        delay: float = 0.0,
    ) -> None:
        self.answers = answers or {}
        self.delay = delay
        self.in_flight = 0
        self.max_in_flight = 0
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self, model: str, messages: list[ChatMessage], **kwargs: Any
    ) -> ChatReply:
        self.calls.append({"model": model, "messages": messages, **kwargs})
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.in_flight -= 1
        prompt = messages[-1].content
        answer = self.answers.get(prompt, prompt.removeprefix("say "))
        if isinstance(answer, GatewayError):
            raise answer
        return ChatReply(
            text=answer,
            model="gpt-4.1-mini",
            provider="openai-default",
            cache_status="MISS",
            prompt_tokens=10,
            completion_tokens=5,
            cost_usd=Decimal("0.00001"),
            latency_s=0.25,
            request_id=kwargs.get("request_id"),
        )


class FakeJudge:
    async def grade(self, **kwargs: Any) -> JudgeVerdict:
        return JudgeVerdict(5, "great", 100, 10, Decimal("0.00005"))


def run(
    cases: list[EvalCase], client: FakeClient, judge: Any = None, **settings: Any
) -> list[CaseResult]:
    return asyncio.run(
        run_cases(
            cases,
            RunSettings(model="chat-default", **settings),
            client,  # type: ignore[arg-type]
            judge=judge,
            run_tag="r7",
        )
    )


def test_each_case_is_answered_scored_and_returned_in_dataset_order() -> None:
    client = FakeClient({"say B": "wrong"}, delay=0.001)
    cases = [make("a", "A"), make("b", "B"), make("c", "C")]

    results = run(cases, client)

    assert [r.case_id for r in results] == ["a", "b", "c"]
    assert [r.passed for r in results] == [True, False, True]
    first = results[0]
    assert first.latency_ms == 250
    assert (first.prompt_tokens, first.completion_tokens) == (10, 5)
    assert first.cost_usd == Decimal("0.00001")
    assert first.output == "A"
    assert first.provider == "openai-default"
    assert first.response_model == "gpt-4.1-mini"
    assert first.request_id == "eval-r7-a"
    assert results[1].reason == "got 'wrong'"


def test_settings_reach_the_gateway() -> None:
    client = FakeClient()
    cases = [make("a"), make("b", max_tokens=9)]

    run(cases, client, temperature=0.5, max_tokens=100, no_cache=True)

    assert [c["max_tokens"] for c in client.calls] == [100, 9]  # a case overrides
    assert all(c["temperature"] == 0.5 for c in client.calls)
    assert all(c["no_store"] is True for c in client.calls)
    assert all(c["model"] == "chat-default" for c in client.calls)


def test_concurrency_is_limited() -> None:
    client = FakeClient(delay=0.01)
    cases = [make(f"c{i}") for i in range(12)]

    run(cases, client, concurrency=3)

    assert client.max_in_flight == 3


def test_a_gateway_error_fails_that_case_only() -> None:
    client = FakeClient({"say B": GatewayError(502, "Upstream error")})

    results = run([make("a", "A"), make("b", "B")], client)

    assert results[0].passed
    failed = results[1]
    assert not failed.passed
    assert failed.score == 0.0
    assert failed.error == "HTTP 502: Upstream error"
    assert failed.latency_ms is None
    assert failed.output is None


def test_a_bad_key_stops_the_run() -> None:
    client = FakeClient({"say B": GatewayError(401, "Invalid or missing API key")})

    with pytest.raises(GatewayError) as exc:
        run([make("a", "A"), make("b", "B"), make("c", "C")], client)

    assert exc.value.fatal


def test_a_judge_case_records_the_judges_tokens_and_cost() -> None:
    case = make("j", rubric="Says hello.")

    [result] = run([case], FakeClient(), judge=FakeJudge())

    assert result.passed
    assert result.score == 1.0
    assert result.reason == "great"
    assert result.judge_tokens == 110
    assert result.judge_cost_usd == Decimal("0.00005")


def test_an_unreadable_judge_verdict_is_a_scoring_error() -> None:
    class Garbled:
        async def grade(self, **kwargs: Any) -> JudgeVerdict:
            from app.evals.judge import JudgeError

            raise JudgeError("judge reply had no JSON object")

    [result] = run([make("j", rubric="R")], FakeClient(), judge=Garbled())

    assert not result.passed
    assert result.error == "scorer: judge reply had no JSON object"
    assert result.output is not None  # the answer is kept for reading


def test_a_judge_gateway_failure_is_recorded() -> None:
    class Down:
        async def grade(self, **kwargs: Any) -> JudgeVerdict:
            raise GatewayError(503, "Service unavailable")

    [result] = run([make("j", rubric="R")], FakeClient(), judge=Down())

    assert result.error == "judge: HTTP 503: Service unavailable"


def test_judge_cases_need_a_judge() -> None:
    cases = [make("a"), make("j", rubric="R")]

    assert needs_judge(cases)
    assert not needs_judge(cases[:1])
    with pytest.raises(ValueError, match="judge"):
        run(cases, FakeClient())


def test_results_are_reported_as_they_finish() -> None:
    seen: list[str] = []

    asyncio.run(
        run_cases(
            [make("a"), make("b")],
            RunSettings(model="m"),
            FakeClient(),  # type: ignore[arg-type]
            on_result=lambda r: seen.append(r.case_id),
        )
    )

    assert sorted(seen) == ["a", "b"]


def test_request_ids_fit_the_gateways_limit() -> None:
    assert request_id_for("r1", "c") == "eval-r1-c"
    assert len(request_id_for("r1", "x" * 100)) == 64


def test_settings_are_validated_and_serialized_without_secrets() -> None:
    with pytest.raises(ValueError):
        RunSettings(model="m", concurrency=0)

    settings = RunSettings(model="m", no_cache=True, extra={"gateway_url": "http://gw"})

    assert settings.as_json() == {
        "model": "m",
        "concurrency": 4,
        "temperature": 0.0,
        "max_tokens": None,
        "no_cache": True,
        "judge_model": None,
        "gateway_url": "http://gw",
    }
