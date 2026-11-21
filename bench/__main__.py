"""python -m bench: run the benchmark suite and write the report.

    python -m bench                    # setup + 3 runs of every scenario + report
    python -m bench --runs 1 --only cache,single_flight
    python -m bench report             # rebuild docs/benchmarks.md from results.json

Needs Docker. Starts (or reuses) the Compose stack with the benchmark-only
Mock routes turned on. Costs nothing: only the Mock provider is called.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from bench import scenarios, stack

RESULTS = stack.ROOT / "docs" / "benchmarks" / "results.json"
RATE_LIMIT_PER_MIN = 120
ALL = ["overhead", "cache", "single_flight", "resilience", "rate_limit"]


def log(message: str) -> None:
    print(
        f"[{datetime.now().strftime('%H:%M:%S')}] {message}",
        file=sys.stderr,
        flush=True,
    )


def fresh(name: str, keys: dict[str, str], *args: str) -> dict[str, object]:
    """Run one scenario inside the Compose network, on a freshly restarted gateway."""
    stack.restart_gateway()
    started, wall = time.perf_counter(), time.time()
    result = stack.run_in_network([name, *args], keys)
    elapsed = time.perf_counter() - started
    result["seconds_total"] = round(elapsed, 1)
    # perf_counter stops while macOS sleeps; the wall clock does not. A gap means
    # the machine slept (pausing Docker) during the scenario: discard the run.
    result["sleep_gap_s"] = round((time.time() - wall) - elapsed, 1)
    if result["sleep_gap_s"] > 2:
        log(f"WARNING: the machine slept for {result['sleep_gap_s']} s during {name}")
    return result


def host_network() -> dict[str, object]:
    """The same /healthz levels from the host, through Docker Desktop's port
    forwarding, for comparison with the in-network numbers."""
    return {"base_url": stack.HOST_URL, "healthz": scenarios.healthz_levels()}


def keep_awake() -> None:
    """Stop macOS from sleeping during the run (it pauses Docker's VM and the
    clocks, which ruins the numbers). caffeinate exits when this process does."""
    if sys.platform == "darwin" and shutil.which("caffeinate"):
        subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])


def run(args: argparse.Namespace) -> Path:
    keep_awake()
    only = args.only.split(",") if args.only else ALL
    unknown = set(only) - set(ALL)
    if unknown:
        raise SystemExit(f"unknown scenario(s): {sorted(unknown)}; choose from {ALL}")

    log("starting the stack (docker compose, benchmark mode)")
    stack.up()
    stack.migrate()
    stack.wait_ready()
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    key = stack.create_user_with_key("bench", stack.UNLIMITED_PER_MIN)
    rl_keys = [
        stack.create_user_with_key(f"bench-rl-{stamp}-{i}", RATE_LIMIT_PER_MIN)
        for i in range(args.runs)
    ]
    commit = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        cwd=stack.ROOT,
        check=False,
    ).stdout.strip()
    meta = {
        "started_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "git_commit": commit,
        "runs": args.runs,
        "scenarios": only,
        "client": "bench-client container on the Compose network (http://gateway:8000)",
        "machine": stack.machine_info(),
        "settings": {
            "overhead_levels": scenarios.OVERHEAD_LEVELS,
            "warmup_requests": scenarios.WARMUP_REQUESTS,
            "injected_delay_ms": scenarios.INJECTED_DELAY_MS,
            "priced_delay_ms": scenarios.PRICED_DELAY_MS,
            "rate_limit_per_min": RATE_LIMIT_PER_MIN,
        },
    }
    keys = {"key": key}

    runs = []
    for i in range(args.runs):
        tag = f"run {i + 1}/{args.runs}"
        result: dict[str, object] = {}
        if "overhead" in only:
            log(f"{tag}: network path (/healthz from the host and in-network)")
            stack.restart_gateway()
            result["network"] = {
                "host": host_network(),
                "in_network": fresh("network", keys),
            }
            log(f"{tag}: gateway overhead")
            result["overhead"] = fresh("overhead", keys)
        if "cache" in only:
            log(f"{tag}: cache")
            result["cache"] = fresh("cache", keys)
        if "single_flight" in only:
            log(f"{tag}: single-flight")
            result["single_flight"] = fresh("single_flight", keys)
        if "resilience" in only:
            log(f"{tag}: resilience (sequential, then concurrent)")
            result["resilience"] = {
                "sequential": fresh("resilience_sequential", keys),
                "concurrent": fresh("resilience_concurrent", keys),
            }
        if "rate_limit" in only:
            log(f"{tag}: rate limiter (60 s)")
            result["rate_limit"] = fresh(
                "rate_limit", {"rate_limit_key": rl_keys[i]}, str(RATE_LIMIT_PER_MIN)
            )
        runs.append(result)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"meta": meta, "runs": runs}, indent=1) + "\n")
    log(f"wrote {out}")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m bench", description=__doc__.split("\n")[0]
    )
    parser.add_argument("command", nargs="?", choices=["run", "report"], default="run")
    parser.add_argument(
        "--runs", type=int, default=3, help="repeat each scenario (default 3)"
    )
    parser.add_argument("--only", help=f"comma-separated subset of {','.join(ALL)}")
    parser.add_argument("--out", default=str(RESULTS), help="results JSON path")
    parser.add_argument("--no-report", action="store_true", help="skip docs and charts")
    args = parser.parse_args(argv)
    if args.runs < 1:
        parser.error("--runs must be at least 1")

    path = Path(args.out) if args.command == "report" else run(args)
    if args.command == "report" or not args.no_report:
        from bench import report  # matplotlib is only needed here

        report.build(path)
        log("wrote docs/benchmarks.md and docs/benchmarks/*.svg")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
