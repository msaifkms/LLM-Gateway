"""Summaries, side-by-side tables and run comparisons, as text or Markdown."""

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from app.evals.runner import CaseResult

# Summaries.


def percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile: the smallest value with q of the data at or below."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q * len(ordered)))
    return ordered[rank - 1]


@dataclass(frozen=True)
class Summary:
    cases: int
    passed: int
    errors: int
    mean_score: float
    p50_ms: float | None
    p95_ms: float | None
    prompt_tokens: int
    completion_tokens: int
    judge_tokens: int
    cost_usd: Decimal
    judge_cost_usd: Decimal

    @property
    def pass_rate(self) -> float:
        return self.passed / self.cases if self.cases else 0.0

    @property
    def total_cost_usd(self) -> Decimal:
        return self.cost_usd + self.judge_cost_usd


def summarize(results: Sequence[CaseResult]) -> Summary:
    """Errors count as failures with score 0; latency covers answered cases."""
    latencies = [r.latency_ms for r in results if r.latency_ms is not None]
    return Summary(
        cases=len(results),
        passed=sum(r.passed for r in results),
        errors=sum(r.error is not None for r in results),
        mean_score=sum(r.score for r in results) / len(results) if results else 0.0,
        p50_ms=percentile(latencies, 0.50),
        p95_ms=percentile(latencies, 0.95),
        prompt_tokens=sum(r.prompt_tokens for r in results),
        completion_tokens=sum(r.completion_tokens for r in results),
        judge_tokens=sum(r.judge_tokens for r in results),
        cost_usd=sum((r.cost_usd for r in results), Decimal(0)),
        judge_cost_usd=sum((r.judge_cost_usd for r in results), Decimal(0)),
    )


@dataclass(frozen=True)
class RunInfo:
    dataset: str
    model: str
    dataset_sha256: str = ""
    id: int | None = None
    judge_model: str | None = None
    batch_id: str | None = None
    status: str = "completed"
    started_at: datetime | None = None
    finished_at: datetime | None = None
    settings: dict[str, Any] = field(default_factory=dict)
    #: Owner of the API key the run used; None if unknown (or not stored).
    user_id: int | None = None

    @property
    def label(self) -> str:
        return f"run {self.id}" if self.id is not None else self.model


# Formatting.


def fmt_ms(value: float | None) -> str:
    return "-" if value is None else f"{value:,.0f} ms"


def fmt_usd(value: Decimal) -> str:
    return f"${value:.6f}"


def fmt_pct(value: float) -> str:
    return f"{value:.1%}"


def clip(text: str, limit: int = 70) -> str:
    text = " ".join(text.split())  # one line, whatever the answer looked like
    return text if len(text) <= limit else text[: limit - 1] + "…"


def table(headers: list[str], rows: list[list[str]], *, markdown: bool) -> str:
    if markdown:

        def line(cells: list[str]) -> str:
            return "| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |"

        out = [line(headers), "|" + "|".join("---" for _ in headers) + "|"]
        return "\n".join(out + [line(row) for row in rows])

    widths = [
        max(len(cells[i]) for cells in [headers, *rows]) for i in range(len(headers))
    ]

    def pad(cells: list[str]) -> str:
        cells = [c.ljust(w) for c, w in zip(cells, widths, strict=True)]
        return "  ".join(cells).rstrip()

    rule = "  ".join("-" * w for w in widths)
    return "\n".join([pad(headers), rule, *(pad(row) for row in rows)])


def heading(text: str, level: int, *, markdown: bool) -> str:
    if markdown:
        return f"{'#' * level} {text}"
    return text if level > 2 else f"{text}\n{'=' * len(text)}"


def summary_values(s: Summary) -> list[tuple[str, str]]:
    """(label, value) rows for one summary, in display order."""
    return [
        ("Cases", str(s.cases)),
        ("Passed", str(s.passed)),
        ("Pass rate", fmt_pct(s.pass_rate)),
        ("Mean score", f"{s.mean_score:.3f}"),
        ("Errors", str(s.errors)),
        ("Latency p50", fmt_ms(s.p50_ms)),
        ("Latency p95", fmt_ms(s.p95_ms)),
        (
            "Tokens (prompt + completion)",
            f"{s.prompt_tokens:,} + {s.completion_tokens:,}",
        ),
        ("Judge tokens", f"{s.judge_tokens:,}"),
        ("Cost (answers)", fmt_usd(s.cost_usd)),
        ("Cost (judge)", fmt_usd(s.judge_cost_usd)),
        ("Cost (total)", fmt_usd(s.total_cost_usd)),
    ]


def summary_table(
    columns: Sequence[tuple[str, Summary]], *, markdown: bool = False
) -> str:
    """Metric rows, one column per run: a single run or several side by side."""
    headers = ["Metric", *(name for name, _ in columns)]
    values = [summary_values(summary) for _, summary in columns]
    rows = [
        [label, *(column[i][1] for column in values)]
        for i, (label, _) in enumerate(values[0])
    ]
    return table(headers, rows, markdown=markdown)


def case_rows(results: Sequence[CaseResult]) -> list[list[str]]:
    rows = []
    for r in results:
        verdict = "ERROR" if r.error else ("PASS" if r.passed else "FAIL")
        note = r.error or r.reason
        rows.append(
            [
                r.case_id,
                r.scorer,
                verdict,
                f"{r.score:.2f}",
                fmt_ms(r.latency_ms),
                str(r.total_tokens),
                r.cache_status or "-",
                clip(note) if note else "",
            ]
        )
    return rows


CASE_HEADERS = ["Case", "Scorer", "Result", "Score", "Latency", "Tokens", "Cache"]


def render_run(
    info: RunInfo, results: Sequence[CaseResult], *, markdown: bool = False
) -> str:
    summary = summarize(results)
    when = info.started_at.strftime("%Y-%m-%d %H:%M UTC") if info.started_at else ""
    facts = [
        f"Dataset: {info.dataset}",
        f"Model: {info.model}",
        f"Run: {info.id if info.id is not None else 'not stored'}",
        f"Status: {info.status}",
    ]
    if info.judge_model:
        facts.append(f"Judge: {info.judge_model}")
    if when:
        facts.append(f"Started: {when}")
    if info.settings.get("no_cache"):
        facts.append("Cache: bypassed (no-store)")
    joiner = " · " if markdown else "\n"
    parts = [
        heading(f"Eval: {info.dataset} on {info.model}", 2, markdown=markdown),
        joiner.join(facts),
        summary_table([("Value", summary)], markdown=markdown),
        heading("Cases", 3, markdown=markdown),
        table([*CASE_HEADERS, "Notes"], case_rows(results), markdown=markdown),
    ]
    return "\n\n".join(parts) + "\n"


def render_side_by_side(
    runs: Sequence[tuple[RunInfo, Sequence[CaseResult]]], *, markdown: bool = False
) -> str:
    """Several models on one dataset: a summary table and a per-case grid."""
    dataset = runs[0][0].dataset
    columns = [(info.model, summarize(results)) for info, results in runs]
    by_model = [{r.case_id: r for r in results} for _, results in runs]
    case_ids = list(dict.fromkeys(r.case_id for _, rs in runs for r in rs))

    def cell(result: CaseResult | None) -> str:
        if result is None:
            return "-"
        if result.error:
            return "ERROR"
        return f"{'PASS' if result.passed else 'FAIL'} {result.score:.2f}"

    grid = [[cid, *(cell(m.get(cid)) for m in by_model)] for cid in case_ids]
    headers = ["Case", *(info.model for info, _ in runs)]
    parts = [
        heading(f"Side by side: {dataset}", 2, markdown=markdown),
        summary_table(columns, markdown=markdown),
        heading("Cases", 3, markdown=markdown),
        table(headers, grid, markdown=markdown),
    ]
    return "\n\n".join(parts) + "\n"


# Comparing two runs.


class Change(StrEnum):
    REGRESSED = "regressed"  # passed before, fails now
    WORSE = "worse"  # same pass/fail, but the score dropped
    FIXED = "fixed"  # failed before, passes now
    BETTER = "better"  # same pass/fail, but the score rose
    SAME = "same"
    ADDED = "added"  # only in the new run
    REMOVED = "removed"  # only in the base run


FLAGGED = frozenset({Change.REGRESSED, Change.WORSE})


@dataclass(frozen=True)
class CaseDelta:
    case_id: str
    change: Change
    base: CaseResult | None
    new: CaseResult | None


@dataclass(frozen=True)
class Comparison:
    base: RunInfo
    new: RunInfo
    deltas: list[CaseDelta]
    base_summary: Summary
    new_summary: Summary

    @property
    def regressions(self) -> list[CaseDelta]:
        return [d for d in self.deltas if d.change in FLAGGED]

    @property
    def dataset_changed(self) -> bool:
        return bool(
            self.base.dataset_sha256
            and self.new.dataset_sha256
            and self.base.dataset_sha256 != self.new.dataset_sha256
        )


def classify(base: CaseResult, new: CaseResult, tolerance: float) -> Change:
    if base.passed and not new.passed:
        return Change.REGRESSED
    if new.passed and not base.passed:
        return Change.FIXED
    if new.score < base.score - tolerance:
        return Change.WORSE
    if new.score > base.score + tolerance:
        return Change.BETTER
    return Change.SAME


def compare_runs(
    base: RunInfo,
    base_results: Sequence[CaseResult],
    new: RunInfo,
    new_results: Sequence[CaseResult],
    *,
    tolerance: float = 0.1,
) -> Comparison:
    """Match cases by id. A score change within ``tolerance`` counts as the same."""
    old = {r.case_id: r for r in base_results}
    now = {r.case_id: r for r in new_results}
    deltas: list[CaseDelta] = []
    for case_id in dict.fromkeys([*old, *now]):
        before, after = old.get(case_id), now.get(case_id)
        if before is None:
            change = Change.ADDED
        elif after is None:
            change = Change.REMOVED
        else:
            change = classify(before, after, tolerance)
        deltas.append(CaseDelta(case_id, change, before, after))
    return Comparison(
        base, new, deltas, summarize(base_results), summarize(new_results)
    )


def _short(result: CaseResult | None) -> str:
    if result is None:
        return "-"
    if result.error:
        return "ERROR"
    return f"{'PASS' if result.passed else 'FAIL'} {result.score:.2f}"


def render_comparison(comparison: Comparison, *, markdown: bool = False) -> str:
    c = comparison
    base_name = f"{c.base.label} ({c.base.model})"
    new_name = f"{c.new.label} ({c.new.model})"
    parts = [heading(f"Compare: {base_name} vs {new_name}", 2, markdown=markdown)]
    if c.base.dataset != c.new.dataset:
        parts.append(
            f"Warning: different datasets ({c.base.dataset} vs {c.new.dataset})."
        )
    elif c.dataset_changed:
        parts.append("Warning: the dataset file changed between these runs.")
    parts.append(
        summary_table(
            [(base_name, c.base_summary), (new_name, c.new_summary)],
            markdown=markdown,
        )
    )

    flagged = c.regressions
    parts.append(
        heading(f"Regressions ({len(flagged)})", 3, markdown=markdown)
        if flagged
        else "No regressions."
    )
    if flagged:
        parts.append(
            table(
                ["Case", "Change", "Before", "After", "Why now"],
                [
                    [
                        d.case_id,
                        d.change.value.upper(),
                        _short(d.base),
                        _short(d.new),
                        clip((d.new.error or d.new.reason) if d.new else ""),
                    ]
                    for d in flagged
                ],
                markdown=markdown,
            )
        )

    others = [d for d in c.deltas if d.change not in FLAGGED]
    if others:
        parts.append(heading("Other cases", 3, markdown=markdown))
        parts.append(
            table(
                ["Case", "Change", "Before", "After"],
                [
                    [d.case_id, d.change.value, _short(d.base), _short(d.new)]
                    for d in others
                ],
                markdown=markdown,
            )
        )
    return "\n\n".join(parts) + "\n"
