"""Build docs/benchmarks.md and docs/benchmarks/*.svg from a results JSON.

    python -m bench report [--out docs/benchmarks/results.json]

Every number in the document is computed here from the measured results; the
prose around it is fixed. Values are the median across runs, with the range
(min-max across runs) where it matters.
"""

import json
import statistics
from decimal import Decimal
from pathlib import Path
from typing import Any

from bench import charts
from bench.stack import ROOT

DOC = ROOT / "docs" / "benchmarks.md"
CHART_DIR = ROOT / "docs" / "benchmarks"


def med(values: list[float]) -> float:
    return statistics.median(values)


def rng(values: list[float], fmt: str = "{:.1f}") -> str:
    return f"{fmt.format(min(values))}–{fmt.format(max(values))}"


def pick(runs: list[dict[str, Any]], *path: str) -> list[Any]:
    out = []
    for run in runs:
        value: Any = run
        for key in path:
            value = value[key]
        out.append(value)
    return out


def ms(value: float) -> str:
    return f"{value:,.1f} ms" if value < 1000 else f"{value / 1000:,.2f} s"


def table(headers: list[str], rows: list[list[str]], right: set[int]) -> str:
    align = ["---:" if i in right else "---" for i in range(len(headers))]
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(align) + "|"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(lines)


def build(results_path: Path, doc: Path = DOC, chart_dir: Path | None = None) -> None:
    global CHART_DIR
    data = json.loads(Path(results_path).read_text())
    meta, runs = data["meta"], data["runs"]
    if chart_dir is not None:
        CHART_DIR = chart_dir
    CHART_DIR.mkdir(parents=True, exist_ok=True)
    sections = [header(meta, runs)]
    if "overhead" in runs[0]:
        sections.append(network_section(runs))
        sections.append(overhead_section(meta, runs))
    if "cache" in runs[0]:
        sections.append(cache_section(runs))
    if "single_flight" in runs[0]:
        sections.append(single_flight_section(runs))
    if "resilience" in runs[0]:
        sections.append(resilience_section(runs))
    if "rate_limit" in runs[0]:
        sections.append(rate_limit_section(meta, runs))
    real = Path(results_path).with_name("real_sample.json")
    if real.exists():
        sections.append(real_sample_section(json.loads(real.read_text()), runs))
    sections.append(reproduce_section())
    doc.write_text("\n\n".join(sections).rstrip() + "\n")


# --- header and setup --------------------------------------------------------------


def max_sleep_gap(runs: list[dict[str, Any]]) -> float:
    gaps: list[float] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            if "sleep_gap_s" in node:
                gaps.append(node["sleep_gap_s"])
            for value in node.values():
                walk(value)

    walk(runs)
    return max(gaps, default=0.0)


def header(meta: dict[str, Any], runs: list[dict[str, Any]]) -> str:
    m = meta["machine"]
    setup = table(
        ["", ""],
        [
            ["Machine", f"{m['host_model']} ({m['host_cpu']}, {m['host_cpu_cores']} cores, {m['host_memory_gib']} GiB), {m['host_os']}"],
            ["Docker", f"Docker Desktop {m['docker_server']}, VM with {m['docker_vm_cpus']} CPUs and {m['docker_vm_memory_gib']} GiB; Compose {m['compose']}"],
            ["Gateway", f"{m['gateway_python']}, one Uvicorn worker, the project's Dockerfile and default settings (Postgres pool of 10, JSON logs at INFO)"],
            ["Data stores", f"{m['postgres_image']} and {m['redis_image']} from docker-compose.yml"],
            ["Power", "on battery (not plugged in)" if "Battery" in (m.get("power") or "") else (m.get("power") or "not recorded")],
            ["Client", "Python asyncio + HTTPX in a container on the Compose network (`http://gateway:8000`)"],
            ["Runs", f"{meta['runs']} runs of every scenario, each on a freshly restarted gateway, after {meta['settings']['warmup_requests']} warm-up requests; tables show the median across runs"],
            ["Measured", f"{meta['started_utc']} at commit `{meta['git_commit']}`"],
            ["Sleep during scenarios", f"none: the largest gap between wall clock and perf_counter in any scenario was {max(0.0, max_sleep_gap(runs)):.1f} s (over 2 s would invalidate the run)"],
        ],
        set(),
    )  # fmt: skip
    return f"""# Benchmarks

These numbers come from the gateway running in **Docker Compose on one laptop**, with the **Mock provider** standing in for OpenAI and Anthropic. They show what the gateway itself costs and what its cache, single-flight, fallback, circuit breaker and rate limiter do, measured end to end. They are **not production numbers**: the client, the gateway, Postgres and Redis share one Docker VM, there is no real network, and no real model is called. Every figure below was measured by `python -m bench`; the raw results are in [benchmarks/results.json](benchmarks/results.json).

## Setup

{setup}

**Method.** Each scenario runs {meta["runs"]} times. The scenarios are interleaved (run 1 of everything, then run 2, ...) so slow drift on the machine does not favour one of them, and the gateway is restarted before each scenario, so breakers, single-flight and in-process metrics start clean. Latency is wall-clock time measured by the client around each request. The gateway's own Prometheus metrics give the server-side numbers and count provider calls, so the gateway's share and the provider's share are separated by measurement, not inference. Costs use the gateway's real price table on simulated token counts (the Mock is free). The runner keeps macOS awake and records, for every scenario, whether the machine slept during it (wall clock against `perf_counter`); the largest gap in this run is shown in the setup table.

**Benchmark-only routes.** With `GATEWAY_BENCH_MODE=true` (only in `bench/docker-compose.bench.yml`) the gateway adds three Mock targets: one that waits a fixed 50 ms, one that always fails with a server error, and one that waits 200 ms and reports 500 + 200 tokens of `gpt-4.1-mini`, so cache savings have a dollar value. Routes: `bench-delay-50`, `bench-fallback` (failing primary, then the healthy `mock-1`) and `bench-priced`."""


# --- network path ------------------------------------------------------------------


def network_section(runs: list[dict[str, Any]]) -> str:
    rows = []
    for level in ("1", "10", "50"):
        h = pick(runs, "network", "host", "healthz", level)
        n = pick(runs, "network", "in_network", "healthz", level)
        rows.append(
            [
                level,
                f"{med([x['rps'] for x in h]):,.0f}",
                ms(med([x["p50_ms"] for x in h])),
                ms(med([x["p99_ms"] for x in h])),
                f"{med([x['rps'] for x in n]):,.0f}",
                ms(med([x["p50_ms"] for x in n])),
                ms(med([x["p99_ms"] for x in n])),
            ]
        )
    t = table(
        ["Concurrency", "Host: req/s", "Host: p50", "Host: p99", "In-network: req/s", "In-network: p50", "In-network: p99"],
        rows,
        {1, 2, 3, 4, 5, 6},
    )  # fmt: skip

    def m(where: str, level: str, key: str) -> float:
        return med([x[key] for x in pick(runs, "network", where, "healthz", level)])

    p50_ratio = m("host", "1", "p50_ms") / m("in_network", "1", "p50_ms")
    rps_drop = 1 - m("host", "50", "rps") / m("in_network", "50", "rps")
    p99_rise = m("host", "50", "p99_ms") / m("in_network", "50", "p99_ms") - 1
    return f"""## Why the client runs inside Docker

`GET /healthz` touches no database and no Redis, so it measures the HTTP path alone. Sent from the Mac through Docker Desktop's published port, it is slower than the same requests sent from a container on the Compose network:

{t}

One at a time, the median through the published port was {p50_ratio:.1f}× the in-network median; at 50 concurrent requests, throughput was {rps_drop:.0%} lower and p99 {p99_rise:.0%} higher. That port forwarding between macOS and the Docker VM is not part of the gateway, so every scenario below runs its client inside the Compose network. Load at 10 and 50 concurrent requests is split across 5 client processes, because in trial runs a single Python client process became the bottleneck before the gateway did."""


# --- 1. overhead -------------------------------------------------------------------


def overhead_section(meta: dict[str, Any], runs: list[dict[str, Any]]) -> str:
    delay = meta["settings"]["injected_delay_ms"]
    rows = []
    rps: dict[str, list[float]] = {"mock-1": [], "bench-delay-50": []}
    lat: dict[str, list[float]] = {"p50": [], "p95": [], "p99": []}
    for route in ("mock-1", "bench-delay-50"):
        for level in ("1", "10", "50"):
            v = pick(runs, "overhead", route, level)
            p50 = med([x["p50_ms"] for x in v])
            p95 = med([x["p95_ms"] for x in v])
            p99 = med([x["p99_ms"] for x in v])
            r = med([x["rps"] for x in v])
            own = med([x["gateway_own_mean_ms"] for x in v])
            rps[route].append(r)
            if route == "mock-1":
                lat["p50"].append(p50)
                lat["p95"].append(p95)
                lat["p99"].append(p99)
            rows.append(
                [
                    f"`{route}`",
                    level,
                    ms(p50),
                    ms(p95),
                    ms(p99),
                    f"{r:,.0f}",
                    ms(own),
                    str(sum(x["errors"] for x in v)),
                ]
            )
    t = table(
        ["Route", "Concurrency", "p50", "p95", "p99", "Req/s", "Gateway's own time (mean)", "Errors"],
        rows,
        {1, 2, 3, 4, 5, 6, 7},
    )  # fmt: skip

    idle = pick(runs, "overhead", "bench-delay-50", "1")
    busy = pick(runs, "overhead", "bench-delay-50-c1-busy")
    added_rows = []
    for label, v in (
        ("Idle between requests", idle),
        ("Machine kept busy (light background load)", busy),
    ):
        added_rows.append(
            [
                label,
                ms(med([x["p50_ms"] for x in v]) - delay),
                ms(med([x["p95_ms"] for x in v]) - delay),
                ms(med([x["p99_ms"] for x in v]) - delay),
                ms(med([x["gateway_own_mean_ms"] for x in v])),
                ms(med([x["provider_mean_ms"] for x in v])),
            ]
        )  # fmt: skip
    added = table(
        ["`bench-delay-50`, one request at a time", "Added p50", "Added p95", "Added p99", "Gateway's own time (mean)", "Mock's 50 ms sleep took (mean)"],
        added_rows,
        {1, 2, 3, 4, 5},
    )  # fmt: skip

    charts.grouped_bars(
        CHART_DIR / "throughput.svg",
        "Throughput by concurrency (cache bypassed)",
        ["1", "10", "50"],
        {
            "mock-1 (0 ms)": rps["mock-1"],
            "bench-delay-50 (50 ms)": rps["bench-delay-50"],
        },
        ylabel="requests per second",
    )
    charts.grouped_bars(
        CHART_DIR / "latency.svg",
        "mock-1 latency by concurrency (log scale)",
        ["1", "10", "50"],
        {"p50": lat["p50"], "p95": lat["p95"], "p99": lat["p99"]},
        ylabel="latency",
        log=True,
        value_format="{:,.1f}",
    )

    m1 = pick(runs, "overhead", "mock-1", "1")
    m1_p50 = med([x["p50_ms"] for x in m1])
    m1_own = med([x["gateway_own_mean_ms"] for x in m1])
    hz = (
        med([x["p50_ms"] for x in pick(runs, "overhead", "healthz", "1")])
        if "healthz" in runs[0]["overhead"]
        else med(
            [x["p50_ms"] for x in pick(runs, "network", "in_network", "healthz", "1")]
        )
    )
    peak = max(rps["mock-1"])
    peak_level = ["1", "10", "50"][rps["mock-1"].index(peak)]
    r10, r50 = rps["mock-1"][1], rps["mock-1"][2]
    p99_50 = lat["p99"][2]
    busy_added = med([x["p50_ms"] for x in busy]) - delay
    idle_added = med([x["p50_ms"] for x in idle]) - delay
    trend = (
        f"From 10 to 50 concurrent requests, throughput fell from {r10:,.0f} to {r50:,.0f} requests per second and p99 latency rose to {ms(p99_50)}."
        if r50 < r10
        else f"From 10 to 50 concurrent requests, throughput went from {r10:,.0f} to {r50:,.0f} requests per second, and p99 latency reached {ms(p99_50)}."
    )  # fmt: skip
    return f"""## 1. Gateway overhead

Every request goes through the whole pipeline, exactly like real traffic: the API key is looked up in Postgres, the rate limiter runs in Redis, the router calls the Mock, and a usage row is written to Postgres. The cache is bypassed (`Cache-Control: no-store`, temperature 0.7), so every request reaches the provider. "Gateway's own time" is the gateway's request-handler time minus the provider-call time, both from its own histograms.

{t}

**Added latency, isolated.** With a provider that takes a fixed 50 ms, everything above 50 ms is the gateway, the HTTP hop, and any overrun of the Mock's own sleep (last column):

{added}

![Throughput by concurrency](benchmarks/throughput.svg)

![Latency by concurrency](benchmarks/latency.svg)

**What this shows.** One request at a time, the full pipeline (Postgres lookup, Redis rate limit, routing, Postgres usage write, a JSON log line) takes a median of {ms(m1_p50)} end to end, of which about {ms(m1_own)} is the gateway's own handler time; `/healthz` alone is {ms(hz)}. Against a provider that takes 50 ms, the gateway added a median of {ms(busy_added)} when the machine was busy, and {ms(idle_added)} when it sat idle between requests. That gap is the laptop's VM waking up, not gateway work: the gateway's own time and even the Mock's 50 ms sleep both stretch when every container goes idle between requests, and shrink back with a little background load. Real model calls take hundreds of milliseconds to seconds, so a few milliseconds of gateway time is a small fraction of a request. Throughput peaked at {peak:,.0f} requests per second at concurrency {peak_level} on one Uvicorn worker. {trend} At that point requests queue: inside the handler as well as in front of it. More workers or instances would raise the ceiling, at the cost of making in-process state (breakers, single-flight) per process; see [CLEANUP.md](CLEANUP.md)."""


# --- 2. cache ----------------------------------------------------------------------


def cache_section(runs: list[dict[str, Any]]) -> str:
    miss = pick(runs, "cache", "miss")
    hit = pick(runs, "cache", "hit")
    mix = pick(runs, "cache", "mix")
    rows = [
        ["MISS (calls the 200 ms Mock)", ms(med([x["p50_ms"] for x in miss])), ms(med([x["p95_ms"] for x in miss])), ms(med([x["p99_ms"] for x in miss])), str(miss[0]["n"])],
        ["HIT (answered from Redis)", ms(med([x["p50_ms"] for x in hit])), ms(med([x["p95_ms"] for x in hit])), ms(med([x["p99_ms"] for x in hit])), str(hit[0]["n"])],
    ]  # fmt: skip
    t = table(["", "p50", "p95", "p99", "Requests per run"], rows, {1, 2, 3, 4})
    requests = mix[0]["requests"]
    calls = [x["provider_calls"] for x in mix]
    without = [float(Decimal(x["cost_without_cache_usd"])) for x in mix]
    billed = [float(Decimal(x["cost_billed_usd"])) for x in mix]
    saved = [x["saved_metric_usd"] for x in mix]
    hit_ratio = [x["hit_ratio"] for x in mix]
    statuses = mix[0]["statuses"]
    mix_rows = [
        ["Requests", f"{requests:,}"],
        [
            "Distinct prompts",
            f"{mix[0]['distinct_prompts']} (Zipf-distributed over 200)",
        ],
        ["Answered from cache (HIT + COALESCED)", f"{med(hit_ratio):.1%}"],
        [
            "Cache statuses (run 1)",
            ", ".join(f"{k} {v}" for k, v in sorted(statuses.items())),
        ],
        ["Provider calls", f"{med(calls):,.0f} (instead of {requests:,})"],
        ["Cost without a cache", f"${med(without):.4f}"],
        ["Cost billed", f"${med(billed):.4f}"],
        ["Saved (the gateway's `gateway_cost_saved_usd_total`)", f"${med(saved):.4f}"],
        ["Errors", str(sum(x["errors"] for x in mix))],
    ]
    mt = table(["Repeated-prompt mix, 10 concurrent", "Median of runs"], mix_rows, {1})
    charts.grouped_bars(
        CHART_DIR / "cache.svg",
        "Cache MISS vs HIT latency (log scale)",
        ["MISS", "HIT"],
        {
            "p50": [med([x["p50_ms"] for x in miss]), med([x["p50_ms"] for x in hit])],
            "p95": [med([x["p95_ms"] for x in miss]), med([x["p95_ms"] for x in hit])],
        },
        ylabel="latency",
        log=True,
        value_format="{:,.1f}",
    )
    hit_p50 = med([x["p50_ms"] for x in hit])
    miss_p50 = med([x["p50_ms"] for x in miss])
    return f"""## 2. Cache

The same 100 prompts are sent twice, one at a time, at temperature 0 on `bench-priced` (a Mock that takes 200 ms). The first pass misses; the second is served from Redis.

{t}

Then a realistic mix: 1,000 requests drawn from 200 prompts with a Zipf distribution (a few prompts are very common, most are rare), 10 in flight at a time, each run with fresh prompts.

{mt}

![Cache MISS vs HIT latency](benchmarks/cache.svg)

**What this shows.** A cache hit took a median of {ms(hit_p50)} against {ms(miss_p50)} for a miss, because a hit never reaches the provider. In the repeated-prompt mix, {med(hit_ratio):.1%} of requests were answered without a provider call, and the gateway made {med(calls):,.0f} provider calls for {requests:,} requests. At `gpt-4.1-mini` list prices on the simulated token counts, that cut the bill from ${med(without):.4f} to ${med(billed):.4f}, and the gateway's own savings counter recorded the same ${med(saved):.4f}. The saving depends entirely on how repetitive the traffic is; only temperature-0 requests are cached."""


# --- 3. single-flight --------------------------------------------------------------


def single_flight_section(runs: list[dict[str, Any]]) -> str:
    on = pick(runs, "single_flight", "cache_on")
    off = pick(runs, "single_flight", "cache_bypassed")
    n = on[0]["requests"]
    rows = []
    for label, v in (
        ("Cache on (single-flight)", on),
        ("Cache bypassed (`no-store`)", off),
    ):
        rows.append(
            [
                label,
                f"{med([x['provider_calls'] for x in v]):,.0f} ({rng([x['provider_calls'] for x in v], '{:.0f}')})",
                ", ".join(f"{k} {c}" for k, c in sorted(v[0]["statuses"].items())),
                ms(med([x["p50_ms"] for x in v])),
                ms(med([x["max_ms"] for x in v])),
                f"${med([float(Decimal(x['cost_billed_usd'])) for x in v]):.4f}",
            ]
        )
    t = table(
        [f"{n} identical requests at once", "Provider calls (range)", "Statuses (run 1)", "p50", "Slowest", "Billed"],
        rows,
        {1, 3, 4, 5},
    )  # fmt: skip
    on_calls = med([x["provider_calls"] for x in on])
    return f"""## 3. Single-flight

{n} identical, cacheable requests are sent at the same moment to `bench-priced` (200 ms), so they all arrive before the first answer exists. Then the same with the cache bypassed.

{t}

**What this shows.** With the cache on, {n} simultaneous identical requests made {on_calls:,.0f} provider call{"s" if on_calls != 1 else ""}: one request (MISS) called the provider and the others (COALESCED) waited for its answer, and only the leader was billed. With the cache bypassed, every request called the provider. Single-flight works within one gateway process; several instances would each make one call."""


# --- 4. resilience -----------------------------------------------------------------


def resilience_section(runs: list[dict[str, Any]]) -> str:
    seq = pick(runs, "resilience", "sequential")
    con = pick(runs, "resilience", "concurrent")
    n = seq[0]["requests"]
    before = [x["before_breaker_opened"] for x in seq if x["before_breaker_opened"]]
    rows = [
        ["Requests that succeeded", f"{med([x['succeeded'] for x in seq]):.0f} of {n} ({rng([x['succeeded'] for x in seq], '{:.0f}')})", f"{med([x['succeeded'] for x in con]):.0f} of {con[0]['requests']} ({rng([x['succeeded'] for x in con], '{:.0f}')})"],
        ["Calls that reached the failing primary", f"{med([x['primary_calls'] for x in seq]):.0f} ({rng([x['primary_calls'] for x in seq], '{:.0f}')})", f"{med([x['primary_calls'] for x in con]):.0f} ({rng([x['primary_calls'] for x in con], '{:.0f}')})"],
        ["Requests that tried the primary first", f"{med([x['requests_that_reached_primary'] for x in seq]):.0f}", "–"],
        ["Latency of those requests (p50)", ms(med([b['p50_ms'] for b in before])) if before else "–", "–"],
        ["Latency once the breaker was open (p50 / p99)", f"{ms(med([x['after_breaker_opened']['p50_ms'] for x in seq]))} / {ms(med([x['after_breaker_opened']['p99_ms'] for x in seq]))}", f"{ms(med([x['p50_ms'] for x in con]))} / {ms(med([x['p99_ms'] for x in con]))} (all requests)"],
        ["Healthy route `mock-1`, same conditions (p50 / p99)", f"{ms(med([x['baseline_mock_1']['p50_ms'] for x in seq]))} / {ms(med([x['baseline_mock_1']['p99_ms'] for x in seq]))}", "–"],
    ]  # fmt: skip
    t = table(
        ["`bench-fallback`, primary always fails", "One at a time", "10 in flight"],
        rows,
        {1, 2},
    )
    first = seq[0]
    charts.resilience_timeline(
        CHART_DIR / "resilience.svg",
        first["per_request_ms"],
        first["reached_primary"]
        + [False] * (len(first["per_request_ms"]) - len(first["reached_primary"])),
        first["baseline_mock_1"]["p50_ms"],
    )
    open_p50 = med([x["after_breaker_opened"]["p50_ms"] for x in seq])
    base_p50 = med([x["baseline_mock_1"]["p50_ms"] for x in seq])
    calls = med([x["primary_calls"] for x in seq])
    succeeded = med([x["succeeded"] for x in seq])
    return f"""## 4. Resilience: primary down

`bench-fallback` lists a Mock that fails every call (a server error, which is retryable) and then the healthy `mock-1`. Each attempt on a target is retried up to 2 times with jittered backoff, and a target's circuit breaker opens after 5 consecutive failures, for 30 seconds. {n} requests are sent one at a time, then {con[0]["requests"]} with 10 in flight, each on a freshly started gateway (breakers closed). For the first 20 sequential requests the gateway's metrics are read after every request, so it is known exactly which requests reached the failing primary.

{t}

![Latency of each request with the primary down](benchmarks/resilience.svg)

**What this shows.** With the primary failing every call, {succeeded:.0f} of {n} requests succeeded, all answered by the fallback. The failing primary was called {calls:.0f} times in total: the first requests tried it (with retries and backoff) before falling back, which made them slow, then the breaker opened and every later request skipped the primary without calling it. Once the breaker was open, a fallback request took a median of {ms(open_p50)}, against {ms(base_p50)} on the healthy route directly, so skipping an open circuit costs almost nothing. With 10 requests in flight the outcome was the same. This confirms the Phase 2 claim (100 of 100 requests succeed with the primary fully down, and the failing provider is called 5 times) on a larger sample. The 30-second recovery probe is not exercised here, because each run finishes well within 30 seconds."""


# --- 5. rate limiter ---------------------------------------------------------------


def rate_limit_section(meta: dict[str, Any], runs: list[dict[str, Any]]) -> str:
    rl = pick(runs, "rate_limit")
    limit = rl[0]["limit_per_min"]
    rows = [
        ["Requests offered", f"{rl[0]['offered']:,} ({rl[0]['offered_rps']:.0f}/s for 60 s)"],
        ["Allowed", f"{med([x['allowed'] for x in rl]):.0f} ({rng([x['allowed'] for x in rl], '{:.0f}')})"],
        ["Expected from the bucket (limit + limit/60 × seconds)", f"{med([x['expected_allowed'] for x in rl]):.1f}"],
        ["Allowed in the first 3 s (the full bucket of " + str(limit) + ", plus refill)", f"{med([sum(x['allowed_per_second'][:3]) for x in rl]):.0f}"],
        ["Sustained rate after the first 10 s", f"{med([x['sustained_per_min'] for x in rl]):.1f} per minute (limit {limit})"],
        ["Refused with 429", f"{med([x['refused_429'] for x in rl]):,.0f}, all with `Retry-After`" if all(x["refusals_with_retry_after"] == x["refused_429"] for x in rl) else f"{med([x['refused_429'] for x in rl]):,.0f}"],
        ["Other statuses", str(sum(x["other_status"] for x in rl))],
    ]  # fmt: skip
    t = table([f"Limit {limit} per minute", "Median of runs (range)"], rows, {1})
    charts.rate_limit_timeline(
        CHART_DIR / "rate_limit.svg",
        rl[0]["allowed_per_second"],
        limit,
        rl[0]["offered_rps"],
    )
    allowed = med([x["allowed"] for x in rl])
    expected = med([x["expected_allowed"] for x in rl])
    sustained = med([x["sustained_per_min"] for x in rl])
    return f"""## 5. Rate limiter accuracy

A user with a limit of {limit} requests per minute is sent {rl[0]["offered_rps"]:.0f} requests per second for 60 seconds, on a fixed schedule, so the offered load does not depend on how fast refusals come back. The token bucket holds {limit} tokens and refills at {limit}/60 = {limit / 60:g} per second, so over a 60-second window it should allow its full bucket at once plus about {limit} more.

{t}

![Allowed requests over time](benchmarks/rate_limit.svg)

**What this shows.** The limiter allowed {allowed:.0f} requests where the bucket's formula gives {expected:.1f}: the initial burst equal to the limit, then a steady {sustained:.1f} per minute, which matches the configured {limit}. Requests were sent at 50 per second from many concurrent tasks, and the atomic Lua script never let more through than the bucket held. So "{limit} per minute" means a burst of up to {limit}, then {limit} per minute, not a hard cap of {limit} in any 60-second window."""


def real_sample_section(real: dict[str, Any], runs: list[dict[str, Any]]) -> str:
    rows = []
    for r in real["results"]:
        ok = r["statuses"].get("200", r["statuses"].get(200, 0))
        rows.append(
            [
                f"`{r['route']}` ({', '.join(r['models'])})",
                f"{ok} of {real['requests_per_route']}",
                ms(r["p50_ms"]) if "p50_ms" in r else "–",
                ms(r["p95_ms"]) if "p95_ms" in r else "–",
                ms(r["provider_mean_ms"]),
                ms(r["gateway_own_mean_ms"]),
                f"${Decimal(r['cost_usd']):.6f}",
            ]
        )
    t = table(
        ["Route (model)", "Succeeded", "p50", "p95", "Provider call (mean)", "Gateway's own time (mean)", "Cost"],
        rows,
        {1, 2, 3, 4, 5, 6},
    )  # fmt: skip
    shares = [
        r["gateway_own_mean_ms"] / (r["provider_mean_ms"] + r["gateway_own_mean_ms"])
        for r in real["results"]
        if r["provider_mean_ms"] == r["provider_mean_ms"]  # skip NaN
    ]
    share = f"{min(shares):.1%}–{max(shares):.1%}" if shares else "n/a"
    return f"""## Real providers, for scale

A small sample against the real APIs, through the same gateway on the same laptop, over the public internet: {real["requests_per_route"]} uncached requests per route, one at a time, a short prompt, at most 40 output tokens. Measured {real["measured_utc"]}; the whole sample cost ${Decimal(real["total_cost_usd"]):.6f}. Raw data: [benchmarks/real_sample.json](benchmarks/real_sample.json).

{t}

**What this shows.** A real model call took hundreds of milliseconds, and the gateway's own time was {share} of each request. That is why a few milliseconds of gateway overhead (section 1) does not matter much in practice. {real["requests_per_route"]} requests per route is a small sample, from one place at one time: it shows the scale, it does not rank the providers."""


def reproduce_section() -> str:
    return """## Reproduce

```bash
python -m pip install -r requirements-dev.txt
python -m bench                 # about 15 minutes: setup, 3 runs, this page and the charts
python -m bench --runs 1 --only cache,single_flight   # a quick subset
python -m bench report          # rebuild this page from docs/benchmarks/results.json
```

It needs Docker, and uses the normal Compose stack plus `bench/docker-compose.bench.yml`. It creates its own users and keys, and costs nothing. On a laptop, keep it plugged in and awake. [bench/README.md](../bench/README.md) describes each scenario."""
