"""Eval command line.

    python -m app.evals validate
    python -m app.evals run --dataset factual_qa --model chat-default \\
        --judge-model chat-default --no-cache --markdown docs/evals/
    python -m app.evals run --dataset json_output --model openai-default \\
        --model anthropic-default          # several models, side by side
    python -m app.evals list
    python -m app.evals report 12
    python -m app.evals compare 11 12 --fail-on-regression

The API key comes from --key or, better (it stays out of shell history),
the EVAL_API_KEY environment variable. Results are stored in Postgres
(DATABASE_URL) unless --no-db is given.
"""

import argparse
import asyncio
import os
import sys
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import asyncpg
import httpx

from app.auth.keys import looks_like_api_key
from app.cli import positive_int
from app.core.config import Settings
from app.db.pool import create_db_pool
from app.evals.client import GatewayClient, GatewayError
from app.evals.dataset import Dataset, DatasetError, builtin_datasets, load_dataset
from app.evals.judge import GatewayJudge
from app.evals.report import (
    RunInfo,
    compare_runs,
    fmt_ms,
    fmt_pct,
    render_comparison,
    render_run,
    render_side_by_side,
    summarize,
    table,
)
from app.evals.runner import CaseResult, RunSettings, needs_judge, run_cases
from app.evals.store import EvalStore, PostgresEvalStore

DEFAULT_URL = "http://localhost:8000"
DB_ERRORS = (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError)


def fraction(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number") from None
    if not 0.0 <= value <= 1.0:
        raise argparse.ArgumentTypeError("must be between 0 and 1")
    return value


def temperature(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number") from None
    if not 0.0 <= value <= 2.0:
        raise argparse.ArgumentTypeError("must be between 0 and 2")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.evals", description="LLM Gateway evaluation harness"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="run a dataset through the gateway")
    run.add_argument(
        "--dataset", required=True, help="a .jsonl path or a built-in dataset name"
    )
    run.add_argument(
        "--model",
        action="append",
        required=True,
        help="model alias (route) to evaluate; repeat to compare several",
    )
    run.add_argument("--key", help="gateway API key (default: $EVAL_API_KEY)")
    run.add_argument(
        "--url", help=f"gateway URL (default: $GATEWAY_URL or {DEFAULT_URL})"
    )
    run.add_argument("--judge-model", help="model alias that grades 'judge' cases")
    run.add_argument(
        "--concurrency", type=positive_int, default=4, help="requests in flight"
    )
    run.add_argument(
        "--no-cache",
        action="store_true",
        help="send Cache-Control: no-store (fresh answers, real latency)",
    )
    run.add_argument("--temperature", type=temperature, default=0.0)
    run.add_argument("--max-tokens", type=positive_int, help="default per case")
    run.add_argument(
        "--tag", action="append", help="only cases with this tag (repeatable)"
    )
    run.add_argument("--limit", type=positive_int, help="only the first N cases")
    run.add_argument(
        "--timeout", type=positive_int, default=60, help="seconds per request"
    )
    run.add_argument("--markdown", help="write a Markdown report to this file or dir")
    run.add_argument(
        "--min-pass-rate",
        type=fraction,
        help="exit with status 1 if any model's pass rate is below this (0-1)",
    )
    run.add_argument(
        "--no-db", action="store_true", help="do not store results in Postgres"
    )

    report = commands.add_parser("report", help="show a stored run")
    report.add_argument("run_id", type=positive_int)
    report.add_argument("--markdown", help="also write it to this file or dir")

    compare = commands.add_parser("compare", help="compare two stored runs")
    compare.add_argument("base_id", type=positive_int, help="the baseline run")
    compare.add_argument("new_id", type=positive_int, help="the run to check")
    compare.add_argument(
        "--tolerance",
        type=fraction,
        default=0.1,
        help="score changes up to this much count as unchanged",
    )
    compare.add_argument("--markdown", help="also write it to this file or dir")
    compare.add_argument(
        "--fail-on-regression",
        action="store_true",
        help="exit with status 1 if any case regressed",
    )

    listing = commands.add_parser("list", help="list recent runs")
    listing.add_argument("--dataset", help="only runs of this dataset")
    listing.add_argument("--limit", type=positive_int, default=20)

    validate = commands.add_parser("validate", help="check dataset files")
    validate.add_argument(
        "datasets", nargs="*", help="paths or names (default: every built-in)"
    )
    return parser


def safe_url(url: str) -> str:
    """The URL without any user:password part, for storing with a run."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{host}{port}{parts.path}".rstrip("/")


def markdown_path(target: str, stem: str) -> Path:
    """A .md path as given, or ``<dir>/<stem>.md`` for a directory."""
    path = Path(target)
    if path.suffix.lower() != ".md":
        path = path / f"{stem}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def write_markdown(target: str, stem: str, title: str, body: str) -> None:
    path = markdown_path(target, stem)
    path.write_text(f"# {title}\n\n{body}")
    print(f"Wrote {path}")


def fail(message: str, code: int = 2) -> int:
    print(f"error: {message}", file=sys.stderr)
    return code


class Progress:
    """One line per finished case on stderr. Shows ids, never prompts or answers."""

    def __init__(self, total: int) -> None:
        self.total = total
        self.done = 0

    def __call__(self, result: CaseResult) -> None:
        self.done += 1
        verdict = "ERROR" if result.error else ("PASS" if result.passed else "FAIL")
        print(
            f"  [{self.done}/{self.total}] {result.case_id:<32} {verdict:<5} "
            f"{fmt_ms(result.latency_ms)}",
            file=sys.stderr,
        )


async def run_one_model(
    dataset: Dataset,
    settings: RunSettings,
    client: GatewayClient,
    store: EvalStore | None,
    batch_id: str,
) -> tuple[RunInfo, list[CaseResult]]:
    info = RunInfo(
        dataset=dataset.name,
        dataset_sha256=dataset.sha256,
        model=settings.model,
        judge_model=settings.judge_model,
        batch_id=batch_id,
        status="running",
        started_at=datetime.now(UTC),
        settings=settings.as_json(),
    )
    if store is not None:
        info = await store.create_run(info)
    tag = f"r{info.id}" if info.id is not None else batch_id[:8]
    judge = (
        GatewayJudge(client, settings.judge_model, no_store=settings.no_cache)
        if settings.judge_model
        else None
    )

    print(f"Running {dataset.name} on {settings.model} ...", file=sys.stderr)
    results: list[CaseResult] = []
    status = "failed"
    try:
        results = await run_cases(
            dataset.cases,
            settings,
            client,
            judge=judge,
            run_tag=tag,
            on_result=Progress(len(dataset.cases)),
        )
        status = "completed"
    finally:
        # Close the run even on a bad key or Ctrl-C, so it is not left "running".
        if store is not None and info.id is not None:
            info = await store.finish_run(info.id, status, results)
    if store is None:
        info = replace(info, status=status)
    return info, results


async def cmd_run(
    args: argparse.Namespace,
    store: EvalStore | None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    try:
        dataset = load_dataset(args.dataset).select(args.tag, args.limit)
    except DatasetError as exc:
        print(f"error: invalid dataset {exc.source}:", file=sys.stderr)
        for problem in exc.problems:
            print(f"  {problem}", file=sys.stderr)
        return 2
    if not dataset.cases:
        return fail("no cases match the --tag filter")
    if needs_judge(dataset.cases) and not args.judge_model:
        return fail(f"{dataset.name} has 'judge' cases; pass --judge-model")

    key = args.key or os.getenv("EVAL_API_KEY", "")
    if not looks_like_api_key(key):
        return fail("pass a gateway API key with --key or EVAL_API_KEY")
    url = args.url or os.getenv("GATEWAY_URL") or DEFAULT_URL
    models = list(dict.fromkeys(args.model))  # repeated --model counts once
    batch_id = uuid.uuid4().hex

    runs: list[tuple[RunInfo, list[CaseResult]]] = []
    async with httpx.AsyncClient(
        base_url=url, timeout=httpx.Timeout(args.timeout), transport=transport
    ) as http:
        client = GatewayClient(http, key)
        for model in models:
            settings = RunSettings(
                model=model,
                concurrency=args.concurrency,
                temperature=args.temperature,
                max_tokens=args.max_tokens,
                no_cache=args.no_cache,
                judge_model=args.judge_model,
                extra={
                    "gateway_url": safe_url(url),
                    "tags": args.tag or [],
                    "limit": args.limit,
                    "cases": len(dataset.cases),
                },
            )
            try:
                runs.append(
                    await run_one_model(dataset, settings, client, store, batch_id)
                )
            except GatewayError as exc:
                return fail(f"the gateway refused the run ({exc})", 1)

    for info, results in runs:
        print()
        print(render_run(info, results), end="")
    if len(runs) > 1:
        print()
        print(render_side_by_side(runs), end="")

    if args.markdown:
        sections = [render_run(info, results, markdown=True) for info, results in runs]
        if len(runs) > 1:
            sections.insert(0, render_side_by_side(runs, markdown=True))
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        write_markdown(
            args.markdown,
            f"{dataset.name}-{stamp}",
            f"Eval report: {dataset.name}",
            "\n".join(sections),
        )

    if args.min_pass_rate is not None:
        low = [
            (info.model, summarize(results).pass_rate)
            for info, results in runs
            if summarize(results).pass_rate < args.min_pass_rate
        ]
        for model, rate in low:
            print(
                f"FAIL: {model} pass rate {fmt_pct(rate)} is below "
                f"{fmt_pct(args.min_pass_rate)}",
                file=sys.stderr,
            )
        if low:
            return 1
    return 0


async def cmd_report(args: argparse.Namespace, store: EvalStore) -> int:
    info = await store.get_run(args.run_id)
    if info is None:
        return fail(f"no run with id {args.run_id}", 1)
    results = await store.get_results(args.run_id)
    print(render_run(info, results), end="")
    if args.markdown:
        write_markdown(
            args.markdown,
            f"run-{info.id}",
            f"Eval report: run {info.id}",
            render_run(info, results, markdown=True),
        )
    return 0


async def cmd_compare(args: argparse.Namespace, store: EvalStore) -> int:
    base = await store.get_run(args.base_id)
    new = await store.get_run(args.new_id)
    for run_id, info in [(args.base_id, base), (args.new_id, new)]:
        if info is None:
            return fail(f"no run with id {run_id}", 1)
    assert base is not None and new is not None
    comparison = compare_runs(
        base,
        await store.get_results(base.id or 0),
        new,
        await store.get_results(new.id or 0),
        tolerance=args.tolerance,
    )
    print(render_comparison(comparison), end="")
    if args.markdown:
        write_markdown(
            args.markdown,
            f"compare-{base.id}-{new.id}",
            f"Eval comparison: run {base.id} vs run {new.id}",
            render_comparison(comparison, markdown=True),
        )
    if args.fail_on_regression and comparison.regressions:
        return 1
    return 0


async def cmd_list(args: argparse.Namespace, store: EvalStore) -> int:
    runs = await store.list_runs(dataset=args.dataset, limit=args.limit)
    if not runs:
        print("No runs.")
        return 0
    rows = [
        [
            str(run.info.id),
            run.info.started_at.strftime("%Y-%m-%d %H:%M")
            if run.info.started_at
            else "",
            run.info.dataset,
            run.info.model,
            run.info.status,
            str(run.cases),
            fmt_pct(run.passed / run.cases) if run.cases else "-",
        ]
        for run in runs
    ]
    headers = ["Run", "Started (UTC)", "Dataset", "Model", "Status", "Cases", "Pass"]
    print(table(headers, rows, markdown=False))
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    names = args.datasets or builtin_datasets()
    bad = 0
    for name in names:
        try:
            dataset = load_dataset(name)
        except DatasetError as exc:
            bad += 1
            print(f"{exc.source}: INVALID")
            for problem in exc.problems:
                print(f"  {problem}")
            continue
        scorers = Counter(case.scorer for case in dataset.cases)
        detail = ", ".join(f"{n} {s}" for s, n in sorted(scorers.items()))
        print(f"{dataset.name}: {len(dataset.cases)} cases ({detail})")
    return 1 if bad else 0


async def dispatch(
    args: argparse.Namespace,
    store: EvalStore | None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    if args.command == "run":
        return await cmd_run(args, store, transport)
    assert store is not None  # every other command needs the database
    if args.command == "report":
        return await cmd_report(args, store)
    if args.command == "compare":
        return await cmd_compare(args, store)
    return await cmd_list(args, store)


async def run_with_database(
    args: argparse.Namespace,
    database_url: str | None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    if args.command == "run" and args.no_db:
        return await dispatch(args, None, transport)
    if not database_url:
        hint = " (or pass --no-db)" if args.command == "run" else ""
        return fail(f"DATABASE_URL is not set{hint}")

    pool = await create_db_pool(database_url, max_size=2)
    try:
        return await dispatch(args, PostgresEvalStore(pool), transport)
    except DB_ERRORS:
        return fail("could not reach the database", 1)
    finally:
        await pool.close()


def main(
    argv: Sequence[str] | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "validate":
        return cmd_validate(args)
    database_url = Settings.from_env().database_url
    return asyncio.run(run_with_database(args, database_url, transport))
