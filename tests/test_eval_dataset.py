import json
from pathlib import Path

import pytest

from app.evals.dataset import (
    BUILTIN_DIR,
    DatasetError,
    EvalCase,
    builtin_datasets,
    load_dataset,
    parse_dataset,
    resolve_path,
)
from app.evals.scorers import get_scorer


def jsonl(*cases: object) -> str:
    return "\n".join(json.dumps(case) for case in cases) + "\n"


def case(**fields: object) -> dict[str, object]:
    return {"id": "c1", "prompt": "Q?", "expected": "A", "scorer": "exact", **fields}


def problems(text: str) -> list[str]:
    with pytest.raises(DatasetError) as exc:
        parse_dataset(text, "test.jsonl")
    return exc.value.problems


def test_a_valid_file_parses_in_order_and_skips_blank_lines() -> None:
    text = jsonl(case(id="a"), case(id="b")) + "\n\n" + jsonl(case(id="c"))

    cases = parse_dataset(text, "test.jsonl")

    assert [c.id for c in cases] == ["a", "b", "c"]


def test_a_prompt_becomes_a_user_message_after_the_system_message() -> None:
    parsed = EvalCase.model_validate(case(system="Be terse."))

    messages = parsed.chat_messages()

    assert [(m.role, m.content) for m in messages] == [
        ("system", "Be terse."),
        ("user", "Q?"),
    ]
    assert parsed.question() == "Q?"


def test_messages_are_sent_as_given() -> None:
    raw = case(
        prompt=None,
        messages=[
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello"},
            {"role": "user", "content": "Again"},
        ],
    )
    del raw["prompt"]

    parsed = EvalCase.model_validate(raw)

    assert [m.content for m in parsed.chat_messages()] == ["Hi", "Hello", "Again"]
    assert parsed.question() == "Again"


def test_every_problem_is_reported_with_its_line_number_at_once() -> None:
    text = "\n".join(
        [
            json.dumps(case(id="ok")),
            "{not json",
            json.dumps([1, 2]),
            json.dumps(case(id="bad-scorer", scorer="nope")),
            json.dumps(case(id="ok")),
        ]
    )

    found = problems(text)

    assert len(found) == 4
    assert found[0].startswith("line 2: not valid JSON")
    assert found[1] == "line 3: expected a JSON object"
    assert found[2].startswith("line 4 (id 'bad-scorer'): unknown scorer 'nope'")
    assert found[3] == "line 5 (id 'ok'): duplicate id, first used on line 1"


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"prompt": None}, "exactly one of 'prompt' or 'messages'"),
        (
            {"messages": [{"role": "user", "content": "x"}]},
            "exactly one of 'prompt' or 'messages'",
        ),
        ({"id": "has space"}, "id:"),
        ({"surprise": 1}, "surprise: Extra inputs are not permitted"),
        ({"max_tokens": 0}, "max_tokens:"),
        ({"tags": "geo"}, "tags:"),
        ({"expected": None}, "needs a non-empty 'expected'"),
        ({"options": {"case_sensitiv": False}}, "unknown option(s) ['case_sensitiv']"),
        ({"options": {"case_sensitive": "no"}}, "'case_sensitive' must be a bool"),
    ],
)
def test_invalid_cases_are_rejected_with_a_clear_reason(
    fields: dict[str, object], message: str
) -> None:
    raw = {k: v for k, v in {**case(), **fields}.items() if v is not None}

    found = problems(jsonl(raw))

    assert len(found) == 1
    assert message in found[0], found[0]


def test_a_system_message_cannot_be_combined_with_messages() -> None:
    raw = case(system="x", messages=[{"role": "user", "content": "x"}])
    del raw["prompt"]

    assert "'system' goes with 'prompt'" in problems(jsonl(raw))[0]


def test_the_last_message_must_be_from_the_user() -> None:
    raw = case(messages=[{"role": "assistant", "content": "x"}])
    del raw["prompt"]

    assert "last message must be from the user" in problems(jsonl(raw))[0]


def test_an_empty_dataset_is_an_error() -> None:
    assert problems("\n\n") == ["the dataset has no cases"]


def test_load_reads_a_file_and_hashes_it(tmp_path: Path) -> None:
    path = tmp_path / "mine.jsonl"
    path.write_text(jsonl(case()))

    dataset = load_dataset(str(path))

    assert dataset.name == "mine"
    assert len(dataset.cases) == 1
    assert len(dataset.sha256) == 64
    path.write_text(jsonl(case(expected="B")))
    assert load_dataset(str(path)).sha256 != dataset.sha256


def test_a_missing_file_lists_the_built_in_datasets(tmp_path: Path) -> None:
    with pytest.raises(DatasetError) as exc:
        load_dataset(str(tmp_path / "missing.jsonl"))

    assert "factual_qa" in exc.value.problems[0]


def test_a_file_that_is_not_utf8_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "latin1.jsonl"
    path.write_bytes(b"\xff\xfe\x00")

    with pytest.raises(DatasetError, match="not UTF-8"):
        load_dataset(str(path))


def test_names_resolve_to_built_in_datasets() -> None:
    assert resolve_path("factual_qa") == BUILTIN_DIR / "factual_qa.jsonl"
    assert resolve_path("some/file.jsonl") == Path("some/file.jsonl")


def test_select_filters_by_tag_then_limits() -> None:
    dataset = load_dataset("factual_qa")

    science = dataset.select(["science"])
    assert science.cases and all("science" in c.tags for c in science.cases)
    assert len(dataset.select(["science", "history"]).cases) > len(science.cases)
    assert len(dataset.select(limit=2).cases) == 2
    assert dataset.select(["no-such-tag"]).cases == []
    assert len(dataset.cases) > 2  # select returned copies


def test_every_built_in_dataset_is_valid() -> None:
    names = builtin_datasets()
    assert {"factual_qa", "instruction_following", "json_output", "mock_smoke"} <= set(
        names
    )
    for name in names:
        assert load_dataset(name).cases


def test_the_starter_datasets_are_small_and_cover_every_scorer() -> None:
    starters = [
        load_dataset(n) for n in ("factual_qa", "instruction_following", "json_output")
    ]
    cases = [c for d in starters for c in d.cases]

    assert 20 <= len(cases) <= 30  # cheap to run against real providers
    assert {c.scorer for c in cases} == {
        "exact",
        "normalized",
        "contains",
        "regex",
        "json",
        "judge",
    }


def test_the_mock_smoke_dataset_needs_no_judge() -> None:
    # CI runs it against the Mock provider, which cannot act as a judge.
    for parsed in load_dataset("mock_smoke").cases:
        assert not get_scorer(parsed.scorer).needs_judge
