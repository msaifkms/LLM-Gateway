import asyncio
import json
from decimal import Decimal

import httpx
import pytest

from app.evals.client import GatewayClient
from app.evals.judge import (
    JUDGE_SYSTEM,
    GatewayJudge,
    JudgeError,
    build_judge_prompt,
    parse_verdict,
)
from app.evals.scorers import JudgeVerdict


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"score": 5, "reason": "Correct."}', (5, "Correct.")),
        ('{"score": 4.0, "reason": " ok "}', (4, "ok")),
        ('Here you go:\n```json\n{"score": 2, "reason": "Wrong"}\n```', (2, "Wrong")),
        ('{"score": 3}', (3, "")),
        ('{"reason": 1, "score": 1}', (1, "")),
        ('{bad} then {"score": 5, "reason": "x"}', (5, "x")),
    ],
)
def test_verdicts_are_parsed_from_the_reply(
    text: str, expected: tuple[int, str]
) -> None:
    assert parse_verdict(text) == expected


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("Hello from the Mock LLM!", "no JSON object"),
        ('{"reason": "no score"}', "no numeric 'score'"),
        ('{"score": "5"}', "no numeric 'score'"),
        ('{"score": true}', "no numeric 'score'"),
        ('{"score": 4.5}', "not an integer from 1 to 5"),
        ('{"score": 0}', "not an integer from 1 to 5"),
        ('{"score": 10}', "not an integer from 1 to 5"),
    ],
)
def test_unusable_verdicts_are_errors_not_zeros(text: str, message: str) -> None:
    with pytest.raises(JudgeError, match=message):
        parse_verdict(text)


def test_the_prompt_includes_the_reference_only_when_given() -> None:
    with_ref = build_judge_prompt("Q", "A", "R", "REF")
    without = build_judge_prompt("Q", "A", "R", None)

    assert "REFERENCE:\nREF" in with_ref
    assert "REFERENCE" not in without
    # The answer comes last, so text inside it cannot pose as the rubric.
    assert with_ref.endswith("ANSWER:\nA")


def test_the_judge_calls_the_gateway_deterministically() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"X-Cache": "MISS"},
            json={
                "id": "x",
                "created": 1,
                "model": "gpt-4.1-mini",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": '{"score": 4, "reason": "Minor gap."}',
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 200,
                    "completion_tokens": 20,
                    "total_tokens": 220,
                },
            },
        )

    async def scenario() -> JudgeVerdict:
        async with httpx.AsyncClient(
            base_url="http://gw", transport=httpx.MockTransport(handler)
        ) as http:
            judge = GatewayJudge(
                GatewayClient(http, "llmgw_k"), "chat-judge", no_store=True
            )
            return await judge.grade(
                question="Q?", answer="A.", rubric="Be right.", reference=None
            )

    verdict = asyncio.run(scenario())

    assert (verdict.rating, verdict.reason) == (4, "Minor gap.")
    assert (verdict.prompt_tokens, verdict.completion_tokens) == (200, 20)
    assert verdict.cost_usd == Decimal("0.000112")  # 200*0.40/M + 20*1.60/M
    body = json.loads(requests[0].content)
    assert body["model"] == "chat-judge"
    assert body["temperature"] == 0.0
    assert body["messages"][0] == {"role": "system", "content": JUDGE_SYSTEM}
    assert "RUBRIC:\nBe right." in body["messages"][1]["content"]
    assert requests[0].headers["cache-control"] == "no-store"
