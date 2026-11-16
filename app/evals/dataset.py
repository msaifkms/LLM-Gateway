"""Eval datasets: one JSON object per line (JSONL), validated before anything runs.

A case looks like this (one line in the file):

    {"id": "capital-france", "prompt": "Capital of France? One word.",
     "expected": "Paris", "scorer": "normalized", "tags": ["geography"]}

Each case has either a "prompt" (optionally with a "system" message) or a full
"messages" list. "expected", "rubric" and "options" mean whatever the scorer says
they mean; the scorer checks them when the file is loaded, so a typo in case 17
fails before case 1 is sent and money is spent.
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.api.schemas import ChatMessage

#: Starter datasets that ship with the code, addressable by name ("factual_qa").
BUILTIN_DIR = Path(__file__).parent / "datasets"


class DatasetError(Exception):
    """A dataset could not be loaded. ``problems`` lists every issue found."""

    def __init__(self, source: str, problems: list[str]) -> None:
        self.source = source
        self.problems = problems
        super().__init__(f"{source}: " + "; ".join(problems))


class EvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,100}$")
    prompt: str | None = Field(default=None, min_length=1)
    system: str | None = Field(default=None, min_length=1)
    messages: list[ChatMessage] | None = Field(default=None, min_length=1)
    scorer: str = Field(min_length=1)
    expected: str | list[str] | None = None
    rubric: str | None = Field(default=None, min_length=1)
    options: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    max_tokens: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _prompt_or_messages(self) -> Self:
        if (self.prompt is None) == (self.messages is None):
            raise ValueError("give exactly one of 'prompt' or 'messages'")
        if self.system is not None and self.messages is not None:
            raise ValueError("'system' goes with 'prompt'; put it in 'messages'")
        if self.messages is not None and self.messages[-1].role != "user":
            raise ValueError("the last message must be from the user")
        return self

    def chat_messages(self) -> list[ChatMessage]:
        """The messages to send to the gateway."""
        if self.messages is not None:
            return list(self.messages)
        assert self.prompt is not None  # guaranteed by the validator
        messages = [ChatMessage(role="user", content=self.prompt)]
        if self.system is not None:
            messages.insert(0, ChatMessage(role="system", content=self.system))
        return messages

    def question(self) -> str:
        """The user's last message: what the answer is answering (for the judge)."""
        return self.chat_messages()[-1].content


@dataclass(frozen=True)
class Dataset:
    name: str
    path: Path
    #: Hash of the file's bytes, stored with each run so that comparing two runs
    #: can warn when the dataset changed between them.
    sha256: str
    cases: list[EvalCase]

    def select(self, tags: list[str] | None = None, limit: int | None = None) -> Self:
        """A copy keeping cases with any of ``tags``, then the first ``limit``."""
        cases = self.cases
        if tags:
            wanted = set(tags)
            cases = [case for case in cases if wanted & set(case.tags)]
        if limit is not None:
            cases = cases[:limit]
        return type(self)(self.name, self.path, self.sha256, cases)


def resolve_path(name_or_path: str) -> Path:
    """A file path as given, or the name of a built-in dataset."""
    path = Path(name_or_path)
    if path.suffix == ".jsonl" or path.exists():
        return path
    return BUILTIN_DIR / f"{name_or_path}.jsonl"


def builtin_datasets() -> list[str]:
    return sorted(path.stem for path in BUILTIN_DIR.glob("*.jsonl"))


def _describe(error: ValidationError) -> str:
    parts = []
    for item in error.errors():
        where = ".".join(str(part) for part in item["loc"])
        message = item["msg"].removeprefix("Value error, ")
        parts.append(f"{where}: {message}" if where else message)
    return "; ".join(parts)


def parse_dataset(text: str, source: str) -> list[EvalCase]:
    """Parse and validate JSONL text. Raises DatasetError listing every problem."""
    # Imported here: scorers import EvalCase from this module.
    from app.evals.scorers import check_case

    cases: list[EvalCase] = []
    problems: list[str] = []
    seen: dict[str, int] = {}

    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        where = f"line {number}"
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            problems.append(f"{where}: not valid JSON ({exc.msg})")
            continue
        if not isinstance(raw, dict):
            problems.append(f"{where}: expected a JSON object")
            continue
        if isinstance(raw.get("id"), str):
            where = f"{where} (id {raw['id']!r})"
        try:
            case = EvalCase.model_validate(raw)
        except ValidationError as exc:
            problems.append(f"{where}: {_describe(exc)}")
            continue
        try:
            check_case(case)
        except (ValueError, TypeError) as exc:
            problems.append(f"{where}: {exc}")
            continue
        if case.id in seen:
            problems.append(
                f"{where}: duplicate id, first used on line {seen[case.id]}"
            )
            continue
        seen[case.id] = number
        cases.append(case)

    if not cases and not problems:
        problems.append("the dataset has no cases")
    if problems:
        raise DatasetError(source, problems)
    return cases


def load_dataset(name_or_path: str) -> Dataset:
    path = resolve_path(name_or_path)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        known = ", ".join(builtin_datasets())
        raise DatasetError(
            str(path), [f"no such file (built-in datasets: {known})"]
        ) from None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise DatasetError(str(path), ["the file is not UTF-8 text"]) from None
    cases = parse_dataset(text, str(path))
    return Dataset(
        name=path.stem,
        path=path,
        sha256=hashlib.sha256(data).hexdigest(),
        cases=cases,
    )
