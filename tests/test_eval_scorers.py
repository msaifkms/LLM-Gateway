import asyncio
from decimal import Decimal
from typing import Any, ClassVar

import pytest

from app.evals.dataset import EvalCase, parse_dataset
from app.evals.scorers import (
    SCORERS,
    JudgeVerdict,
    Score,
    Scorer,
    check_case,
    get_scorer,
    normalize,
    parse_json_answer,
    register,
)


def make(scorer: str, expected: Any = None, **fields: Any) -> EvalCase:
    raw: dict[str, Any] = {"id": "c", "prompt": "Q?", "scorer": scorer, **fields}
    if expected is not None:
        raw["expected"] = expected
    parsed = EvalCase.model_validate(raw)
    check_case(parsed)
    return parsed


def score(case: EvalCase, output: str, judge: Any = None) -> Score:
    return asyncio.run(get_scorer(case.scorer).score(case, output, judge))


# exact


def test_exact_ignores_surrounding_whitespace_only() -> None:
    case = make("exact", "Au")

    assert score(case, "  Au\n").passed
    assert not score(case, "au").passed
    assert not score(case, "Au.").passed
    assert score(case, "Ag").reason == "got 'Ag'"


def test_exact_accepts_any_of_several_answers_and_can_ignore_case() -> None:
    case = make("exact", ["yes", "y"], options={"case_sensitive": False})

    assert score(case, "YES").passed
    assert score(case, "Y").passed
    assert not score(case, "yep").passed


# normalized


def test_normalize_lowercases_and_drops_punctuation_and_extra_spaces() -> None:
    assert normalize("  Paris. ") == "paris"
    assert normalize("Hello,   World!") == "hello world"
    assert normalize("ＡＢＣ") == "abc"  # full-width letters fold too


def test_normalized_match() -> None:
    case = make("normalized", ["Paris", "Paris, France"])

    assert score(case, "paris!").passed
    assert score(case, "PARIS,  FRANCE.").passed
    assert not score(case, "The capital is Paris.").passed


def test_normalized_needs_something_left_after_normalizing() -> None:
    with pytest.raises(ValueError, match="empty after normalization"):
        make("normalized", "...")


# contains


def test_contains_all_scores_the_fraction_of_keywords_found() -> None:
    case = make("contains", ["adenine", "thymine", "cytosine", "guanine"])

    full = score(case, "Adenine, Thymine, Cytosine and Guanine.")
    partial = score(case, "adenine and thymine")

    assert full.passed and full.score == 1.0
    assert not partial.passed
    assert partial.score == 0.5
    assert partial.reason == "missing ['cytosine', 'guanine']"


def test_contains_any_passes_on_one_keyword() -> None:
    case = make("contains", ["ahoy", "matey"], options={"mode": "any"})

    assert score(case, "Ahoy there!").passed
    assert not score(case, "Hello.").passed


def test_contains_can_be_case_sensitive() -> None:
    case = make("contains", ["NASA"], options={"case_sensitive": True})

    assert score(case, "NASA launched").passed
    assert not score(case, "nasa launched").passed


def test_contains_rejects_an_unknown_mode() -> None:
    with pytest.raises(ValueError, match="'all' or 'any'"):
        make("contains", ["a"], options={"mode": "most"})


# regex


def test_regex_searches_anywhere_by_default() -> None:
    case = make("regex", r"300[,\s]?000")

    assert score(case, "About 300,000 km/s.").passed
    miss = score(case, " About 299 792 km/s.\n")
    assert not miss.passed
    assert miss.reason == "no match in 'About 299 792 km/s.'"


def test_regex_full_match_and_flags() -> None:
    case = make("regex", r"yes", options={"full_match": True, "ignore_case": True})

    assert score(case, " YES \n").passed  # the answer is stripped first
    assert not score(case, "yes, sure").passed


def test_regex_multiline_flag() -> None:
    case = make("regex", r"^- b$", options={"multiline": True})

    assert score(case, "- a\n- b\n- c").passed


def test_a_bad_regex_is_rejected_when_the_dataset_loads() -> None:
    with pytest.raises(ValueError, match="not a valid regex"):
        make("regex", "(unclosed")


def test_regex_takes_exactly_one_pattern() -> None:
    with pytest.raises(ValueError, match="one pattern"):
        make("regex", ["a", "b"])


# json


def test_parse_json_answer_unwraps_a_code_fence() -> None:
    assert parse_json_answer('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_answer("```\n[1, 2]\n```") == [1, 2]
    with pytest.raises(ValueError):
        parse_json_answer('```json\n{"a": 1}\n```', allow_fences=False)


def test_json_without_options_only_needs_valid_json() -> None:
    case = make("json")

    assert score(case, '{"anything": [1, 2]}').passed
    bad = score(case, "Sure! Here it is: {")
    assert not bad.passed and bad.score == 0.0
    assert bad.reason.startswith("not valid JSON")


def test_json_required_keys_and_values_score_partially() -> None:
    case = make(
        "json",
        options={"required_keys": ["item"], "values": {"quantity": 3, "price": 4.5}},
    )

    assert score(case, '{"item": "notebook", "quantity": 3, "price": 4.5}').passed
    half = score(case, '{"item": "notebook", "quantity": 2}')
    assert not half.passed
    assert half.score == pytest.approx(1 / 3)
    assert "'quantity' is 2, expected 3" in half.reason
    assert "'price' is None, expected 4.5" in half.reason


def test_json_with_keys_needs_an_object() -> None:
    case = make("json", options={"required_keys": ["a"]})

    result = score(case, "[1, 2]")

    assert not result.passed
    assert result.reason == "expected an object, got list"


def test_json_equals_compares_the_whole_value() -> None:
    case = make("json", options={"equals": [2, 3, 5, 7, 11]})

    assert score(case, "```json\n[2, 3, 5, 7, 11]\n```").passed
    assert not score(case, "[2, 3, 5, 7, 9]").passed


def test_json_options_are_validated() -> None:
    with pytest.raises(TypeError, match="'values' must be an object"):
        make("json", options={"values": [1]})
    with pytest.raises(ValueError, match="list of non-empty strings"):
        make("json", options={"required_keys": "a"})
    with pytest.raises(ValueError, match="'equals' on its own"):
        make("json", options={"equals": 1, "required_keys": ["a"]})


# judge


class FakeJudge:
    def __init__(self, rating: int, reason: str = "fine") -> None:
        self.rating = rating
        self.reason = reason
        self.calls: list[dict[str, Any]] = []

    async def grade(self, **kwargs: Any) -> JudgeVerdict:
        self.calls.append(kwargs)
        return JudgeVerdict(self.rating, self.reason, 100, 20, Decimal("0.0001"))


def test_judge_maps_the_rating_onto_zero_to_one_and_passes_at_four() -> None:
    case = make("judge", rubric="Mentions oxygen.", expected="Plants make O2.")

    good = score(case, "answer", FakeJudge(5))
    ok = score(case, "answer", FakeJudge(4))
    meh = score(case, "answer", FakeJudge(3, "vague"))
    bad = score(case, "answer", FakeJudge(1))

    assert (good.score, good.passed) == (1.0, True)
    assert (ok.score, ok.passed) == (0.75, True)
    assert (meh.score, meh.passed, meh.reason) == (0.5, False, "vague")
    assert (bad.score, bad.passed) == (0.0, False)
    assert good.judge is not None and good.judge.cost_usd == Decimal("0.0001")


def test_judge_receives_the_question_answer_rubric_and_reference() -> None:
    case = make("judge", rubric="R", expected="ref")
    judge = FakeJudge(5)

    score(case, "my answer", judge)

    assert judge.calls == [
        {"question": "Q?", "answer": "my answer", "rubric": "R", "reference": "ref"}
    ]


def test_judge_pass_score_is_configurable() -> None:
    case = make("judge", rubric="R", options={"pass_score": 3})

    assert score(case, "a", FakeJudge(3)).passed


def test_judge_cases_are_validated() -> None:
    with pytest.raises(ValueError, match="needs a 'rubric'"):
        make("judge")
    with pytest.raises(ValueError, match="between 1 and 5"):
        make("judge", rubric="R", options={"pass_score": 6})
    with pytest.raises(ValueError, match="must be a int"):
        make("judge", rubric="R", options={"pass_score": True})
    with pytest.raises(TypeError, match="one reference answer"):
        make("judge", ["a", "b"], rubric="R")


def test_judge_without_a_judge_is_an_error() -> None:
    with pytest.raises(ValueError, match="needs a judge model"):
        score(make("judge", rubric="R"), "answer", None)


# the registry


def test_scores_must_be_between_zero_and_one() -> None:
    with pytest.raises(ValueError):
        Score(1.5, True)


def test_the_built_in_scorers_are_registered() -> None:
    assert {"exact", "normalized", "contains", "regex", "json", "judge"} <= set(SCORERS)
    assert get_scorer("judge").needs_judge
    assert not get_scorer("exact").needs_judge


def test_an_unknown_scorer_names_the_known_ones() -> None:
    with pytest.raises(ValueError, match="known: .*exact"):
        get_scorer("fuzzy")


def test_a_new_scorer_plugs_in_with_register(monkeypatch: pytest.MonkeyPatch) -> None:
    class StartsWith(Scorer):
        name = "starts_with"
        options: ClassVar[frozenset[str]] = frozenset({"strip"})

        def check(self, case: EvalCase) -> None:
            if not isinstance(case.expected, str):
                raise TypeError("needs a prefix")

        async def score(self, case: EvalCase, output: str, judge: Any) -> Score:
            assert isinstance(case.expected, str)
            return Score.from_bool(output.startswith(case.expected))

    monkeypatch.setattr("app.evals.scorers.SCORERS", dict(SCORERS))
    register(StartsWith())

    cases = parse_dataset(
        '{"id": "x", "prompt": "Q", "scorer": "starts_with", "expected": "Sure"}',
        "plugin.jsonl",
    )
    assert score(cases[0], "Sure thing").passed
    with pytest.raises(ValueError, match="already registered"):
        register(StartsWith())
