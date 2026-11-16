"""The eval CLI, driven against the real app in-process (Mock provider, no network)."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fake_eval_store import FakeEvalStore

from app.evals.cli import build_parser, dispatch, main, markdown_path, safe_url
from app.evals.store import EvalStore
from app.main import app, lifespan

KEY = "llmgw_test-key"


def run_cli(
    *argv: str,
    store: EvalStore | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    """Run a command with the gateway's lifespan up and requests sent to it in-process."""
    args = build_parser().parse_args(argv)

    async def scenario() -> int:
        async with lifespan(app):
            return await dispatch(
                args, store, transport or httpx.ASGITransport(app=app)
            )

    return asyncio.run(scenario())


def write_dataset(tmp_path: Path, *cases: dict[str, object]) -> str:
    path = tmp_path / "custom.jsonl"
    path.write_text("".join(json.dumps(case) + "\n" for case in cases))
    return str(path)


def test_run_mock_smoke_passes_and_prints_a_report(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = run_cli(
        "run", "--dataset", "mock_smoke", "--model", "mock-1", "--key", KEY,
        "--min-pass-rate", "1",
    )  # fmt: skip

    assert code == 0
    out = capsys.readouterr()
    assert "Eval: mock_smoke on mock-1" in out.out
    assert "100.0%" in out.out
    assert "[6/6]" in out.err  # progress goes to stderr
    assert "Hello from the Mock LLM" not in out.err  # no answers in progress lines


def test_run_with_a_store_records_the_run_and_every_result(
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = FakeEvalStore()

    code = run_cli(
        "run", "--dataset", "mock_smoke", "--model", "mock-1", "--key", KEY,
        "--concurrency", "2", "--no-cache", "--tag", "smoke",
        store=store,
    )  # fmt: skip

    assert code == 0
    run = store.runs[1]
    assert (run.dataset, run.model, run.status) == ("mock_smoke", "mock-1", "completed")
    assert run.settings["concurrency"] == 2
    assert run.settings["no_cache"] is True
    assert run.settings["tags"] == ["smoke"]
    assert run.settings["gateway_url"] == "http://localhost:8000"
    assert KEY not in json.dumps(run.settings)
    results = store.results[1]
    assert len(results) == 6
    assert all(r.passed for r in results)
    assert {r.cache_status for r in results} == {"BYPASS"}
    assert all(r.request_id == f"eval-r1-{r.case_id}" for r in results)
    assert "Run: 1" in capsys.readouterr().out


def test_several_models_run_side_by_side_in_one_batch(
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = FakeEvalStore()

    code = run_cli(
        "run", "--dataset", "mock_smoke", "--model", "mock-1", "--model", "chat-test",
        "--model", "mock-1", "--key", KEY,
        store=store,
    )  # fmt: skip

    assert code == 0
    assert [r.model for r in store.runs.values()] == ["mock-1", "chat-test"]
    assert len({r.batch_id for r in store.runs.values()}) == 1
    out = capsys.readouterr().out
    assert "Side by side: mock_smoke" in out


def test_a_low_pass_rate_fails_the_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The Mock never returns JSON, so every json_output case fails.
    code = run_cli(
        "run", "--dataset", "json_output", "--model", "mock-1", "--key", KEY,
        "--min-pass-rate", "0.5",
    )  # fmt: skip

    assert code == 1
    assert "FAIL: mock-1 pass rate 0.0% is below 50.0%" in capsys.readouterr().err


def test_markdown_reports_are_written(tmp_path: Path) -> None:
    out_dir = tmp_path / "docs" / "evals"

    code = run_cli(
        "run", "--dataset", "mock_smoke", "--model", "mock-1", "--model", "chat-test",
        "--key", KEY, "--markdown", str(out_dir),
    )  # fmt: skip

    assert code == 0
    [report] = list(out_dir.glob("mock_smoke-*.md"))
    text = report.read_text()
    assert text.startswith("# Eval report: mock_smoke")
    assert "## Side by side: mock_smoke" in text
    assert "## Eval: mock_smoke on chat-test" in text
    assert "| exact-reply | exact | PASS |" in text


def test_judge_datasets_need_a_judge_model(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = run_cli("run", "--dataset", "factual_qa", "--model", "mock-1", "--key", KEY)

    assert code == 2
    assert "pass --judge-model" in capsys.readouterr().err


def test_a_mock_judge_is_a_scoring_error_not_a_crash(tmp_path: Path) -> None:
    store = FakeEvalStore()
    dataset = write_dataset(
        tmp_path, {"id": "j", "prompt": "Q", "scorer": "judge", "rubric": "R"}
    )

    code = run_cli(
        "run", "--dataset", dataset, "--model", "mock-1", "--judge-model", "mock-1",
        "--key", KEY,
        store=store,
    )  # fmt: skip

    assert code == 0
    [result] = store.results[1]
    assert result.error == "scorer: judge reply had no JSON object"
    assert store.runs[1].judge_model == "mock-1"


def test_an_invalid_dataset_lists_its_problems(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset = write_dataset(tmp_path, {"id": "x", "prompt": "Q", "scorer": "regex"})

    code = run_cli("run", "--dataset", dataset, "--model", "mock-1", "--key", KEY)

    assert code == 2
    assert "needs a non-empty 'expected'" in capsys.readouterr().err


def test_a_key_is_required(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("EVAL_API_KEY", raising=False)

    assert run_cli("run", "--dataset", "mock_smoke", "--model", "mock-1") == 2
    assert "EVAL_API_KEY" in capsys.readouterr().err


def test_the_key_can_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVAL_API_KEY", KEY)

    assert run_cli("run", "--dataset", "mock_smoke", "--model", "mock-1") == 0


def test_a_rejected_key_stops_the_run_and_marks_it_failed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = FakeEvalStore()
    denied = httpx.MockTransport(
        lambda request: httpx.Response(
            401, json={"detail": "Invalid or missing API key"}
        )
    )

    code = run_cli(
        "run", "--dataset", "mock_smoke", "--model", "mock-1", "--key", KEY,
        store=store, transport=denied,
    )  # fmt: skip

    assert code == 1
    assert store.runs[1].status == "failed"
    assert store.results[1] == []
    assert "HTTP 401" in capsys.readouterr().err


def test_no_cases_left_after_filtering_is_an_error() -> None:
    code = run_cli(
        "run", "--dataset", "mock_smoke", "--model", "mock-1", "--key", KEY,
        "--tag", "nope",
    )  # fmt: skip

    assert code == 2


def test_report_compare_and_list_read_stored_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = FakeEvalStore()
    dataset = write_dataset(
        tmp_path,
        {"id": "a", "prompt": "Q", "scorer": "contains", "expected": ["mock"]},
        {"id": "b", "prompt": "Q", "scorer": "contains", "expected": ["hello"]},
    )
    run_cli("run", "--dataset", dataset, "--model", "mock-1", "--key", KEY, store=store)
    # Same file, edited so case "b" now fails: a regression, and a changed dataset.
    Path(dataset).write_text(
        json.dumps(
            {"id": "a", "prompt": "Q", "scorer": "contains", "expected": ["mock"]}
        )
        + "\n"
        + json.dumps({"id": "b", "prompt": "Q", "scorer": "exact", "expected": "bye"})
        + "\n"
    )
    run_cli(
        "run", "--dataset", dataset, "--model", "chat-test", "--key", KEY, store=store
    )
    capsys.readouterr()

    assert run_cli("report", "1", store=store) == 0
    assert "Eval: custom on mock-1" in capsys.readouterr().out

    assert run_cli("compare", "1", "2", "--markdown", str(tmp_path), store=store) == 0
    out = capsys.readouterr().out
    assert "Regressions (1)" in out
    assert "the dataset file changed" in out
    assert (tmp_path / "compare-1-2.md").exists()
    assert run_cli("compare", "1", "2", "--fail-on-regression", store=store) == 1
    assert run_cli("compare", "2", "1", "--fail-on-regression", store=store) == 0

    capsys.readouterr()
    assert run_cli("list", store=store) == 0
    listing = capsys.readouterr().out.splitlines()
    assert listing[0].split()[:2] == ["Run", "Started"]
    assert listing[2].split()[0] == "2"  # newest first
    assert "50.0%" in listing[2]
    assert "100.0%" in listing[3]


def test_report_and_compare_of_a_missing_run(
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = FakeEvalStore()

    assert run_cli("report", "9", store=store) == 1
    assert run_cli("compare", "1", "9", store=store) == 1
    assert "no run with id" in capsys.readouterr().err
    assert run_cli("list", store=store) == 0


def test_validate_checks_every_built_in_dataset(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["validate"]) == 0

    out = capsys.readouterr().out
    assert "factual_qa: 11 cases" in out
    assert "json_output: 7 cases (7 json)" in out


def test_validate_reports_bad_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = write_dataset(tmp_path, {"id": "x", "prompt": "Q", "scorer": "nope"})

    assert main(["validate", bad, "mock_smoke"]) == 1

    out = capsys.readouterr().out
    assert "INVALID" in out
    assert "unknown scorer 'nope'" in out
    assert "mock_smoke: 6 cases" in out


def test_commands_need_a_database_unless_told_not_to(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", "")

    assert main(["list"]) == 2
    assert main(["run", "--dataset", "mock_smoke", "--model", "m", "--key", KEY]) == 2
    err = capsys.readouterr().err
    assert "DATABASE_URL is not set" in err
    assert "(or pass --no-db)" in err


@pytest.mark.parametrize(
    "argv",
    [
        ["run", "--model", "m"],  # a dataset is required
        ["run", "--dataset", "d"],  # a model is required
        ["run", "--dataset", "d", "--model", "m", "--concurrency", "0"],
        ["run", "--dataset", "d", "--model", "m", "--min-pass-rate", "1.5"],
        ["run", "--dataset", "d", "--model", "m", "--temperature", "3"],
        ["compare", "1"],
        ["report", "abc"],
    ],
)
def test_bad_arguments_are_rejected(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(argv)

    assert exc.value.code == 2


def test_stored_urls_drop_credentials() -> None:
    assert safe_url("http://user:secret@gw:8000/") == "http://gw:8000"
    assert safe_url("https://gw.example.com") == "https://gw.example.com"


def test_markdown_paths(tmp_path: Path) -> None:
    assert markdown_path(str(tmp_path / "a.md"), "x") == tmp_path / "a.md"
    assert markdown_path(str(tmp_path / "dir"), "x") == tmp_path / "dir" / "x.md"
    assert (tmp_path / "dir").is_dir()
