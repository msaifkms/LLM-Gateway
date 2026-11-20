"""Scorers: turn a model's answer into a score between 0 and 1 and a pass/fail.

Every scorer has a name (the "scorer" field in a dataset), a list of options it
accepts, a ``check`` that validates a case when the dataset is loaded, and an
async ``score``. To add one, subclass Scorer and call ``register``:

    class StartsWith(Scorer):
        name = "starts_with"

        def check(self, case):
            expected_strings(case)

        async def score(self, case, output, judge):
            ok = output.strip().startswith(expected_strings(case)[0])
            return Score.from_bool(ok, "" if ok else "wrong start")

    register(StartsWith())
"""

import json
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, ClassVar, Protocol

from app.evals.dataset import EvalCase


@dataclass(frozen=True)
class JudgeVerdict:
    """What the judge model said, and what asking it cost."""

    rating: int  # 1 (worst) to 5 (best)
    reason: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: Decimal = Decimal(0)


class Judge(Protocol):
    async def grade(
        self, *, question: str, answer: str, rubric: str, reference: str | None
    ) -> JudgeVerdict: ...


@dataclass(frozen=True)
class Score:
    score: float  # 0.0 to 1.0
    passed: bool
    reason: str = ""
    #: Set when a judge model was asked, so its tokens and cost can be counted.
    judge: JudgeVerdict | None = None

    def __post_init__(self) -> None:
        if not 0.0 <= self.score <= 1.0:
            raise ValueError(f"score must be between 0 and 1, got {self.score}")

    @classmethod
    def from_bool(cls, passed: bool, reason: str = "") -> "Score":
        return cls(1.0 if passed else 0.0, passed, reason)


class Scorer:
    name: ClassVar[str]
    #: Option names this scorer accepts; anything else in "options" is a typo.
    options: ClassVar[frozenset[str]] = frozenset()
    #: True if scoring calls a judge model (the run then needs --judge-model).
    needs_judge: ClassVar[bool] = False

    def check(self, case: EvalCase) -> None:
        """Raise ValueError (or TypeError) if the case is unusable with this scorer."""

    async def score(self, case: EvalCase, output: str, judge: Judge | None) -> Score:
        raise NotImplementedError


SCORERS: dict[str, Scorer] = {}


def register(scorer: Scorer) -> Scorer:
    if scorer.name in SCORERS:
        raise ValueError(f"a scorer named {scorer.name!r} is already registered")
    SCORERS[scorer.name] = scorer
    return scorer


def get_scorer(name: str) -> Scorer:
    try:
        return SCORERS[name]
    except KeyError:
        known = ", ".join(sorted(SCORERS))
        raise ValueError(f"unknown scorer {name!r} (known: {known})") from None


def check_case(case: EvalCase) -> None:
    """Validate a case against its scorer. Called by the dataset loader."""
    scorer = get_scorer(case.scorer)
    unknown = set(case.options) - scorer.options
    if unknown:
        allowed = ", ".join(sorted(scorer.options)) or "none"
        raise ValueError(
            f"unknown option(s) {sorted(unknown)} for scorer {scorer.name!r} "
            f"(allowed: {allowed})"
        )
    scorer.check(case)


# Helpers shared by the scorers.


def expected_strings(case: EvalCase) -> list[str]:
    """``expected`` as a non-empty list of non-empty strings."""
    expected = case.expected
    values = [expected] if isinstance(expected, str) else expected
    if not values or any(not value for value in values):
        raise ValueError(f"scorer {case.scorer!r} needs a non-empty 'expected'")
    return values


def option[T](case: EvalCase, name: str, kind: type[T], default: T) -> T:
    value = case.options.get(name, default)
    # bool is a subclass of int, so be strict about it either way.
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise ValueError(f"option {name!r} must be a {kind.__name__}")
    return value


def string_list_option(case: EvalCase, name: str) -> list[str]:
    value = case.options.get(name, [])
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise ValueError(f"option {name!r} must be a list of non-empty strings")
    return value


def normalize(text: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace: "  Paris. " -> "paris"."""
    text = unicodedata.normalize("NFKC", text).casefold()
    text = "".join(
        " " if unicodedata.category(ch).startswith("P") else ch for ch in text
    )
    return " ".join(text.split())


def _quote(text: str, limit: int = 60) -> str:
    return repr(text if len(text) <= limit else text[: limit - 3] + "...")


# The built-in scorers.


class ExactMatch(Scorer):
    """The answer equals one of the expected strings (surrounding spaces ignored)."""

    name = "exact"
    options = frozenset({"case_sensitive"})

    def check(self, case: EvalCase) -> None:
        expected_strings(case)
        option(case, "case_sensitive", bool, True)

    async def score(self, case: EvalCase, output: str, judge: Judge | None) -> Score:
        sensitive = option(case, "case_sensitive", bool, True)
        answer = output.strip()
        expected = [value.strip() for value in expected_strings(case)]
        if not sensitive:
            answer, expected = answer.casefold(), [v.casefold() for v in expected]
        if answer in expected:
            return Score.from_bool(True)
        return Score.from_bool(False, f"got {_quote(output.strip())}")


class NormalizedMatch(Scorer):
    """Like exact, after lowercasing and removing punctuation and extra spaces."""

    name = "normalized"

    def check(self, case: EvalCase) -> None:
        if not any(normalize(value) for value in expected_strings(case)):
            raise ValueError("'expected' is empty after normalization")

    async def score(self, case: EvalCase, output: str, judge: Judge | None) -> Score:
        answer = normalize(output)
        if answer in {normalize(value) for value in expected_strings(case)}:
            return Score.from_bool(True)
        return Score.from_bool(False, f"got {_quote(answer)}")


class Contains(Scorer):
    """The answer contains the expected keywords: all of them, or any one.

    The score is the fraction of keywords found, so a partial answer scores
    between 0 and 1 even though it fails.
    """

    name = "contains"
    options = frozenset({"mode", "case_sensitive"})

    def _mode(self, case: EvalCase) -> str:
        mode = option(case, "mode", str, "all")
        if mode not in {"all", "any"}:
            raise ValueError("option 'mode' must be 'all' or 'any'")
        return mode

    def check(self, case: EvalCase) -> None:
        expected_strings(case)
        self._mode(case)
        option(case, "case_sensitive", bool, False)

    async def score(self, case: EvalCase, output: str, judge: Judge | None) -> Score:
        keywords = expected_strings(case)
        mode = self._mode(case)
        sensitive = option(case, "case_sensitive", bool, False)
        haystack = output if sensitive else output.casefold()
        found = [k for k in keywords if (k if sensitive else k.casefold()) in haystack]
        missing = [k for k in keywords if k not in found]
        if mode == "any":
            passed = bool(found)
            return Score.from_bool(passed, "" if passed else "none of the keywords")
        fraction = len(found) / len(keywords)
        reason = f"missing {missing}" if missing else ""
        return Score(fraction, not missing, reason)


class RegexMatch(Scorer):
    """The answer matches a regular expression (anywhere, or in full)."""

    name = "regex"
    options = frozenset({"ignore_case", "multiline", "dotall", "full_match"})

    def _compile(self, case: EvalCase) -> re.Pattern[str]:
        patterns = expected_strings(case)
        if len(patterns) != 1:
            raise ValueError("scorer 'regex' needs one pattern in 'expected'")
        flags = 0
        for name, flag in [
            ("ignore_case", re.IGNORECASE),
            ("multiline", re.MULTILINE),
            ("dotall", re.DOTALL),
        ]:
            if option(case, name, bool, False):
                flags |= flag
        try:
            return re.compile(patterns[0], flags)
        except re.error as exc:
            raise ValueError(f"'expected' is not a valid regex ({exc})") from None

    def check(self, case: EvalCase) -> None:
        self._compile(case)
        option(case, "full_match", bool, False)

    async def score(self, case: EvalCase, output: str, judge: Judge | None) -> Score:
        pattern = self._compile(case)
        text = output.strip()
        if option(case, "full_match", bool, False):
            matched = pattern.fullmatch(text) is not None
        else:
            matched = pattern.search(text) is not None
        return Score.from_bool(
            matched, "" if matched else f"no match in {_quote(text)}"
        )


_FENCE = re.compile(r"^```[A-Za-z]*\s*\n(.*?)\n?```$", re.DOTALL)


def parse_json_answer(output: str, *, allow_fences: bool = True) -> Any:
    """Decode the answer as JSON, unwrapping a ```json fence if allowed."""
    text = output.strip()
    if allow_fences:
        fenced = _FENCE.match(text)
        if fenced:
            text = fenced.group(1).strip()
    return json.loads(text)


class ValidJson(Scorer):
    """The answer is valid JSON, optionally with required keys and values.

    Options: required_keys (list of keys), values (an object of key: expected
    value, compared exactly), equals (the whole answer must equal this JSON
    value), allow_fences (accept a ```json fence, default true). The score is
    the fraction of checks that passed.
    """

    name = "json"
    options = frozenset({"required_keys", "values", "equals", "allow_fences"})

    def _values(self, case: EvalCase) -> dict[str, Any]:
        values = case.options.get("values", {})
        if not isinstance(values, dict):
            raise TypeError("option 'values' must be an object")
        return values

    def check(self, case: EvalCase) -> None:
        string_list_option(case, "required_keys")
        self._values(case)
        option(case, "allow_fences", bool, True)
        if "equals" in case.options and (
            "required_keys" in case.options or "values" in case.options
        ):
            raise ValueError("use 'equals' on its own, or 'required_keys'/'values'")

    async def score(self, case: EvalCase, output: str, judge: Judge | None) -> Score:
        try:
            data = parse_json_answer(
                output, allow_fences=option(case, "allow_fences", bool, True)
            )
        except json.JSONDecodeError as exc:
            return Score(0.0, False, f"not valid JSON ({exc.msg})")

        if "equals" in case.options:
            same = data == case.options["equals"]
            return Score.from_bool(same, "" if same else f"got {_quote(output)}")
        required = string_list_option(case, "required_keys")
        values = self._values(case)
        if not required and not values:
            return Score.from_bool(True)
        if not isinstance(data, dict):
            return Score(0.0, False, f"expected an object, got {type(data).__name__}")

        problems: list[str] = []
        checks = 0
        for key in required:
            checks += 1
            if key not in data:
                problems.append(f"missing key {key!r}")
        for key, want in values.items():
            checks += 1
            if data.get(key) != want:
                problems.append(f"{key!r} is {data.get(key)!r}, expected {want!r}")
        score = (checks - len(problems)) / checks
        return Score(score, not problems, "; ".join(problems))


class LLMJudge(Scorer):
    """A judge model rates the answer from 1 to 5 against a rubric.

    "rubric" says what a good answer looks like; "expected" (optional) is a
    reference answer shown to the judge. Option pass_score (1-5, default 4) is the
    lowest rating that passes. The score is the rating mapped onto 0-1.
    """

    name = "judge"
    options = frozenset({"pass_score"})
    needs_judge = True

    def _pass_score(self, case: EvalCase) -> int:
        pass_score = option(case, "pass_score", int, 4)
        if not 1 <= pass_score <= 5:
            raise ValueError("option 'pass_score' must be between 1 and 5")
        return pass_score

    def check(self, case: EvalCase) -> None:
        if case.rubric is None:
            raise ValueError("scorer 'judge' needs a 'rubric'")
        if isinstance(case.expected, list):
            raise TypeError("scorer 'judge' takes one reference answer in 'expected'")
        self._pass_score(case)

    async def score(self, case: EvalCase, output: str, judge: Judge | None) -> Score:
        if judge is None:
            raise ValueError("this case needs a judge model (--judge-model)")
        assert case.rubric is not None  # check() ran when the dataset loaded
        verdict = await judge.grade(
            question=case.question(),
            answer=output,
            rubric=case.rubric,
            reference=case.expected if isinstance(case.expected, str) else None,
        )
        passed = verdict.rating >= self._pass_score(case)
        return Score((verdict.rating - 1) / 4, passed, verdict.reason, verdict)


for _scorer in (
    ExactMatch(),
    NormalizedMatch(),
    Contains(),
    RegexMatch(),
    ValidJson(),
    LLMJudge(),
):
    register(_scorer)
