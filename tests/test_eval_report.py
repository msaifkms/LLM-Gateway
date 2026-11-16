from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from app.evals.report import (
    Change,
    RunInfo,
    classify,
    compare_runs,
    percentile,
    render_comparison,
    render_run,
    render_side_by_side,
    summarize,
    table,
)
from app.evals.runner import CaseResult


def result(
    case_id: str, passed: bool = True, score: float | None = None, **kw: Any
) -> CaseResult:
    fields: dict[str, Any] = {
        "latency_ms": 100.0,
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "cost_usd": Decimal("0.001"),
        "output": "answer",
        **kw,
    }
    return CaseResult(
        case_id=case_id,
        scorer="exact",
        score=(1.0 if passed else 0.0) if score is None else score,
        passed=passed,
        **fields,
    )


INFO = RunInfo(
    dataset="factual_qa",
    model="chat-default",
    id=3,
    started_at=datetime(2026, 11, 16, 12, 0, tzinfo=UTC),
    settings={"no_cache": True},
)


def test_percentile_is_nearest_rank() -> None:
    values = [float(v) for v in range(1, 101)]

    assert percentile(values, 0.5) == 50
    assert percentile(values, 0.95) == 95
    assert percentile([7.0], 0.95) == 7
    assert percentile([], 0.5) is None
    assert percentile([3.0, 1.0, 2.0], 0.5) == 2  # sorts first


def test_summary_counts_errors_as_failures_and_adds_up_spend() -> None:
    results = [
        result("a"),
        result("b", passed=False, score=0.5),
        result(
            "c",
            passed=False,
            error="HTTP 502: Upstream error",
            latency_ms=None,
            prompt_tokens=0,
            completion_tokens=0,
            cost_usd=Decimal(0),
        ),
        result("d", latency_ms=300.0, judge_tokens=50, judge_cost_usd=Decimal("0.002")),
    ]

    s = summarize(results)

    assert (s.cases, s.passed, s.errors) == (4, 2, 1)
    assert s.pass_rate == 0.5
    assert s.mean_score == pytest.approx((1 + 0.5 + 0 + 1) / 4)
    assert s.p50_ms == 100  # of the three cases that got an answer
    assert s.p95_ms == 300
    assert (s.prompt_tokens, s.completion_tokens, s.judge_tokens) == (30, 15, 50)
    assert s.cost_usd == Decimal("0.003")
    assert s.judge_cost_usd == Decimal("0.002")
    assert s.total_cost_usd == Decimal("0.005")


def test_an_empty_summary_is_all_zeros() -> None:
    s = summarize([])

    assert (s.cases, s.pass_rate, s.mean_score, s.p50_ms) == (0, 0.0, 0.0, None)


def test_text_tables_align_and_markdown_tables_escape_pipes() -> None:
    text = table(["A", "Long header"], [["x", "1"], ["longer", "2"]], markdown=False)
    md = table(["A", "B"], [["a|b", "c"]], markdown=True)

    assert text.splitlines() == [
        "A       Long header",
        "------  -----------",
        "x       1",
        "longer  2",
    ]
    assert md.splitlines() == ["| A | B |", "|---|---|", "| a\\|b | c |"]


def test_a_run_report_shows_the_summary_and_every_case() -> None:
    results = [
        result("capital", reason=""),
        result("gold", passed=False, reason="got 'Ag'"),
        result("down", passed=False, error="HTTP 503: x", latency_ms=None),
    ]

    text = render_run(INFO, results)
    md = render_run(INFO, results, markdown=True)

    assert "Eval: factual_qa on chat-default" in text
    assert "Run: 3" in text
    assert "Cache: bypassed (no-store)" in text
    assert "Pass rate" in text and "33.3%" in text
    for line in ("capital", "gold", "down", "got 'Ag'", "HTTP 503: x", "ERROR"):
        assert line in text
    assert md.startswith("## Eval: factual_qa on chat-default")
    assert "| Metric | Value |" in md


def test_long_or_multiline_notes_are_clipped_to_one_line() -> None:
    long_reason = "line one\nline two " + "x" * 200

    text = render_run(INFO, [result("a", passed=False, reason=long_reason)])

    row = next(line for line in text.splitlines() if line.startswith("a "))
    assert "line one line two" in row
    assert row.endswith("…")


def test_side_by_side_puts_models_in_columns() -> None:
    a = RunInfo(dataset="d", model="openai-default")
    b = RunInfo(dataset="d", model="anthropic-default")
    runs = [
        (a, [result("x"), result("y", passed=False)]),
        (b, [result("x"), result("y", passed=False, error="HTTP 502: e")]),
    ]

    text = render_side_by_side(runs)

    assert "Side by side: d" in text
    header = next(line for line in text.splitlines() if line.startswith("Metric"))
    assert "openai-default" in header and "anthropic-default" in header
    y_row = next(line for line in text.splitlines() if line.startswith("y "))
    assert "FAIL 0.00" in y_row and "ERROR" in y_row


@pytest.mark.parametrize(
    ("before", "after", "change"),
    [
        ((True, 1.0), (False, 0.0), Change.REGRESSED),
        ((False, 0.0), (True, 1.0), Change.FIXED),
        ((False, 0.8), (False, 0.5), Change.WORSE),
        ((False, 0.5), (False, 0.8), Change.BETTER),
        ((True, 1.0), (True, 0.95), Change.SAME),  # within tolerance
        ((True, 1.0), (True, 1.0), Change.SAME),
    ],
)
def test_classify(
    before: tuple[bool, float], after: tuple[bool, float], change: Change
) -> None:
    base = result("c", passed=before[0], score=before[1])
    new = result("c", passed=after[0], score=after[1])

    assert classify(base, new, tolerance=0.1) is change


def test_compare_flags_regressions_and_lists_added_and_removed_cases() -> None:
    base = RunInfo(dataset="d", model="m1", id=1, dataset_sha256="aaa")
    new = RunInfo(dataset="d", model="m2", id=2, dataset_sha256="aaa")
    comparison = compare_runs(
        base,
        [result("keep"), result("break"), result("gone"), result("dip", False, 0.9)],
        new,
        [
            result("keep"),
            result("break", passed=False, reason="got 'nope'"),
            result("dip", False, 0.4),
            result("new"),
        ],
    )

    changes = {d.case_id: d.change for d in comparison.deltas}
    assert changes == {
        "keep": Change.SAME,
        "break": Change.REGRESSED,
        "gone": Change.REMOVED,
        "dip": Change.WORSE,
        "new": Change.ADDED,
    }
    assert [d.case_id for d in comparison.regressions] == ["break", "dip"]
    assert not comparison.dataset_changed

    text = render_comparison(comparison)
    assert "Compare: run 1 (m1) vs run 2 (m2)" in text
    assert "Regressions (2)" in text
    assert "REGRESSED" in text and "got 'nope'" in text
    assert "Warning" not in text


def test_compare_warns_when_the_dataset_changed() -> None:
    base = RunInfo(dataset="d", model="m", id=1, dataset_sha256="aaa")
    new = RunInfo(dataset="d", model="m", id=2, dataset_sha256="bbb")
    other = RunInfo(dataset="e", model="m", id=3, dataset_sha256="aaa")

    changed = compare_runs(base, [result("a")], new, [result("a")])
    different = compare_runs(base, [result("a")], other, [result("a")])

    assert changed.dataset_changed
    assert "the dataset file changed" in render_comparison(changed)
    assert "different datasets (d vs e)" in render_comparison(different)
    assert "No regressions." in render_comparison(changed, markdown=True)
