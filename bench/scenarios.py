"""The benchmark scenarios. Each returns a JSON-ready dict for one run.

Every number here is measured: client-side wall-clock latency (perf_counter),
response headers (X-Cache, X-Provider, X-Cost-USD), or deltas of the gateway's
own Prometheus counters around the scenario.
"""

import asyncio
import multiprocessing as mp
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

from bench import stack
from bench.stats import counter_total, latency_summary, provider_calls, zipf_sample

CHAT = "/v1/chat/completions"
INJECTED_DELAY_MS = 50.0  # mock-delay-50 (app/providers/registry.py)
PRICED_DELAY_MS = 200.0  # mock-priced

#: (concurrency, measured requests) for the overhead scenario.
OVERHEAD_LEVELS = [(1, 300), (10, 2000), (50, 4000)]
WARMUP_REQUESTS = 50


@dataclass(frozen=True)
class Sample:
    index: int
    started_s: float  # since the scenario's clock started
    latency_s: float
    status: int
    cache: str | None
    provider: str | None
    cost_usd: Decimal
    retry_after: str | None


def client(concurrency: int) -> httpx.AsyncClient:
    limits = httpx.Limits(
        max_connections=concurrency + 10, max_keepalive_connections=concurrency + 10
    )
    return httpx.AsyncClient(
        base_url=stack.BASE_URL, limits=limits, timeout=httpx.Timeout(60.0)
    )


async def chat(
    http: httpx.AsyncClient,
    key: str,
    model: str,
    prompt: str,
    *,
    index: int = 0,
    clock0: float = 0.0,
    temperature: float = 0.0,
    no_store: bool = False,
) -> Sample:
    headers = {"Authorization": f"Bearer {key}"}
    if no_store:
        headers["Cache-Control"] = "no-store"
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
    }
    started = time.perf_counter()
    response = await http.post(CHAT, json=body, headers=headers)
    latency = time.perf_counter() - started
    return Sample(
        index=index,
        started_s=started - clock0,
        latency_s=latency,
        status=response.status_code,
        cache=response.headers.get("x-cache"),
        provider=response.headers.get("x-provider"),
        cost_usd=Decimal(response.headers.get("x-cost-usd", "0")),
        retry_after=response.headers.get("retry-after"),
    )


async def closed_loop(
    n: int, concurrency: int, call: Callable[[int], Awaitable[Sample]]
) -> tuple[list[Sample], float]:
    """`concurrency` workers each send the next request as soon as theirs returns."""
    samples: list[Sample] = []
    next_index = 0

    async def worker() -> None:
        nonlocal next_index
        while next_index < n:
            i = next_index
            next_index += 1
            samples.append(await call(i))

    started = time.perf_counter()
    await asyncio.gather(*(worker() for _ in range(concurrency)))
    return samples, time.perf_counter() - started


def ok_latencies(samples: list[Sample]) -> list[float]:
    return [s.latency_s for s in samples if s.status == 200]


# --- load from several processes ----------------------------------------------

#: Client processes per concurrency level. One Python process tops out around
#: 1,100-1,500 requests/s (measured), so higher levels split the load.
PROCESSES = {1: 1, 10: 5, 50: 5}


@dataclass(frozen=True)
class Spec:
    """What each request is (picklable, so worker processes can rebuild it)."""

    kind: str  # "healthz" or "chat"
    base_url: str
    key: str = ""
    route: str = ""
    prefix: str = ""


def _child(spec: Spec, n: int, c: int, barriers: tuple[Any, Any], queue: Any) -> None:
    async def go() -> None:
        limits = httpx.Limits(max_connections=c + 5, max_keepalive_connections=c + 5)
        async with httpx.AsyncClient(
            base_url=spec.base_url, limits=limits, timeout=httpx.Timeout(60.0)
        ) as http:

            async def one(i: int) -> tuple[float, int]:
                started = time.perf_counter()
                if spec.kind == "healthz":
                    response = await http.get("/healthz")
                else:
                    response = await http.post(
                        CHAT,
                        json={
                            "model": spec.route,
                            "messages": [
                                {"role": "user", "content": f"{spec.prefix} {i}"}
                            ],
                            "temperature": 0.7,
                        },
                        headers={
                            "Authorization": f"Bearer {spec.key}",
                            "Cache-Control": "no-store",
                        },
                    )
                return time.perf_counter() - started, response.status_code

            async def loop(count: int) -> list[tuple[float, int]]:
                out: list[tuple[float, int]] = []
                next_index = 0

                async def worker() -> None:
                    nonlocal next_index
                    while next_index < count:
                        i = next_index
                        next_index += 1
                        out.append(await one(i))

                await asyncio.gather(*(worker() for _ in range(c)))
                return out

            await loop(max(WARMUP_REQUESTS // 2, c))
            barriers[0].wait()  # warmed up; the parent reads the metrics
            barriers[1].wait()  # go
            started = time.perf_counter()
            samples = await loop(n)
            queue.put((samples, started, time.perf_counter()))

    asyncio.run(go())


def multi_process_level(
    spec: Spec, n: int, concurrency: int
) -> tuple[list[float], int, float, str, str]:
    """Closed loop at `concurrency`, split across PROCESSES[concurrency] processes.

    Returns (ok latencies, error count, wall seconds, metrics before, metrics after).
    perf_counter is a system-wide monotonic clock on Linux, so start and end
    times from different processes are comparable.
    """
    procs = PROCESSES.get(concurrency, 1)
    ctx = mp.get_context("spawn")
    barriers = (ctx.Barrier(procs + 1), ctx.Barrier(procs + 1))
    queue = ctx.Queue()
    shares = [
        concurrency // procs + (1 if i < concurrency % procs else 0)
        for i in range(procs)
    ]
    counts = [n // procs + (1 if i < n % procs else 0) for i in range(procs)]
    workers = [
        ctx.Process(target=_child, args=(spec, counts[i], shares[i], barriers, queue))
        for i in range(procs)
    ]
    for w in workers:
        w.start()
    barriers[0].wait()
    before = stack.metrics_text() if spec.kind == "chat" else ""
    barriers[1].wait()
    results = [queue.get() for _ in workers]
    for w in workers:
        w.join()
    after = stack.metrics_text() if spec.kind == "chat" else ""
    latencies = [
        lat for samples, _, _ in results for lat, status in samples if status == 200
    ]
    errors = sum(status != 200 for samples, _, _ in results for _, status in samples)
    wall = max(end for _, _, end in results) - min(start for _, start, _ in results)
    return latencies, errors, wall, before, after


# --- 1. gateway overhead ---------------------------------------------------------

PROVIDER_OF = {"mock-1": "mock-1", "bench-delay-50": "mock-delay-50"}


def histogram_mean_ms(
    before: str, after: str, name: str, labels: dict[str, str]
) -> float:
    """Mean of a Prometheus histogram over an interval, from _sum and _count deltas."""
    total = histogram_part(after, name, labels, "_sum") - histogram_part(
        before, name, labels, "_sum"
    )
    count = histogram_part(after, name, labels, "_count") - histogram_part(
        before, name, labels, "_count"
    )
    return round(total / count * 1000, 3) if count else float("nan")


def histogram_part(text: str, name: str, labels: dict[str, str], suffix: str) -> float:
    from prometheus_client.parser import text_string_to_metric_families

    value = 0.0
    for family in text_string_to_metric_families(text):
        if family.name != name:
            continue
        for sample in family.samples:
            if sample.name.endswith(suffix) and all(
                sample.labels.get(k) == v for k, v in labels.items()
            ):
                value += sample.value
    return value


def healthz_levels() -> dict[str, dict[str, float]]:
    out = {}
    for concurrency, n in OVERHEAD_LEVELS:
        latencies, errors, wall, _, _ = multi_process_level(
            Spec("healthz", stack.BASE_URL), n, concurrency
        )
        out[str(concurrency)] = latency_summary(latencies) | {
            "rps": round(n / wall, 1),
            "errors": errors,
            "client_processes": PROCESSES.get(concurrency, 1),
        }
    return out


async def network() -> dict[str, object]:
    """/healthz (no database, no Redis) at each level: the cost of the HTTP path."""
    return {"base_url": stack.BASE_URL, "healthz": healthz_levels()}


async def overhead(key: str) -> dict[str, object]:
    """Latency and throughput through the full pipeline, cache bypassed.

    Every request is authenticated (Postgres), rate limited (Redis), routed,
    and its usage written to Postgres, exactly like real traffic. Besides the
    client's view, the gateway's own histograms give the time spent inside the
    request handler and inside the provider call, so the gateway's own share
    (handler minus provider) is measured, not inferred.
    """
    result: dict[str, object] = {}

    for route in ("mock-1", "bench-delay-50"):
        levels: dict[str, object] = {}
        for concurrency, n in OVERHEAD_LEVELS:
            spec = Spec("chat", stack.BASE_URL, key, route, f"overhead c{concurrency}")
            latencies, errors, wall, before, after = multi_process_level(
                spec, n, concurrency
            )
            levels[str(concurrency)] = summarize_level(
                route, latencies, errors, n, wall, before, after
            ) | {"client_processes": PROCESSES.get(concurrency, 1)}
        result[route] = levels

    result["bench-delay-50-c1-busy"] = await delay_route_while_busy(key)
    return result


def summarize_level(
    route: str,
    latencies: list[float],
    errors: int,
    n: int,
    elapsed: float,
    before: str,
    after: str,
) -> dict[str, object]:
    server = histogram_mean_ms(
        before, after, "gateway_http_request_duration_seconds",
        {"route": "/v1/chat/completions"},
    )  # fmt: skip
    provider = histogram_mean_ms(
        before, after, "gateway_provider_attempt_duration_seconds",
        {"provider": PROVIDER_OF[route]},
    )  # fmt: skip
    return latency_summary(latencies) | {
        "rps": round(n / elapsed, 1),
        "errors": errors,
        "server_mean_ms": server,
        "provider_mean_ms": provider,
        "gateway_own_mean_ms": round(server - provider, 3),
    }


async def delay_route_while_busy(key: str, n: int = 300) -> dict[str, object]:
    """The 50 ms route one request at a time, while two light /healthz loops keep
    the machine from going idle between requests (see docs/benchmarks.md)."""
    async with client(4) as http:
        stop = False

        async def busy() -> None:
            # Light, paced load (about 200 requests/s per loop): enough to keep
            # the machine awake, not enough to compete for the client's CPU.
            while not stop:
                await http.get("/healthz")
                await asyncio.sleep(0.005)

        loops = [asyncio.create_task(busy()) for _ in range(2)]

        def call(i: int) -> Awaitable[Sample]:
            return chat(
                http, key, "bench-delay-50", f"busy {i}", temperature=0.7, no_store=True
            )

        await closed_loop(WARMUP_REQUESTS, 1, call)
        before = stack.metrics_text()
        samples, elapsed = await closed_loop(n, 1, call)
        after = stack.metrics_text()
        stop = True
        await asyncio.gather(*loops)
    return summarize_level(
        "bench-delay-50",
        ok_latencies(samples),
        sum(s.status != 200 for s in samples),
        n,
        elapsed,
        before,
        after,
    )


# --- 2. cache --------------------------------------------------------------------


async def cache(key: str) -> dict[str, object]:
    """HIT vs MISS latency, and a repeated-prompt mix with a Zipf shape."""
    ns = uuid.uuid4().hex[:8]  # fresh prompts, so nothing is cached from before
    async with client(10) as http:
        for i in range(10):  # warm-up on prompts not reused below
            await chat(http, key, "bench-priced", f"{ns} warmup {i}")

        misses = [
            await chat(http, key, "bench-priced", f"{ns} question {i}")
            for i in range(100)
        ]
        hits = [
            await chat(http, key, "bench-priced", f"{ns} question {i}")
            for i in range(100)
        ]

        before = stack.metrics_text()
        picks = zipf_sample(200, 1000, seed=42)

        def call(i: int) -> Awaitable[Sample]:
            return chat(http, key, "bench-priced", f"{ns} popular {picks[i]}")

        mix, elapsed = await closed_loop(len(picks), 10, call)
        after = stack.metrics_text()

    per_request = max(s.cost_usd for s in misses)  # what one provider call costs
    statuses = Counter(s.cache for s in mix)
    billed = sum((s.cost_usd for s in mix), Decimal(0))
    calls = provider_calls(after, "mock-priced") - provider_calls(before, "mock-priced")
    saved = counter_total(after, "gateway_cost_saved_usd") - counter_total(
        before, "gateway_cost_saved_usd"
    )
    return {
        "miss": latency_summary(ok_latencies(misses))
        | {"statuses": dict(Counter(s.cache for s in misses))},
        "hit": latency_summary(ok_latencies(hits))
        | {"statuses": dict(Counter(s.cache for s in hits))},
        "mix": {
            "requests": len(mix),
            "distinct_prompts": len(set(picks)),
            "statuses": dict(statuses),
            "errors": sum(s.status != 200 for s in mix),
            "provider_calls": calls,
            "hit_ratio": round(
                (statuses.get("HIT", 0) + statuses.get("COALESCED", 0)) / len(mix), 4
            ),
            "cost_per_provider_call_usd": str(per_request),
            "cost_without_cache_usd": str(per_request * len(mix)),
            "cost_billed_usd": str(billed),
            "saved_metric_usd": round(saved, 8),
            "seconds": round(elapsed, 2),
        },
    }


# --- 3. single-flight ------------------------------------------------------------


async def single_flight(key: str, n: int = 50) -> dict[str, object]:
    """n identical requests at once: with the cache on, and with it bypassed."""
    out: dict[str, object] = {}
    async with client(n) as http:
        await chat(http, key, "bench-priced", "warmup single-flight")
        for label, no_store in (("cache_on", False), ("cache_bypassed", True)):
            prompt = f"single-flight {uuid.uuid4().hex}"
            before = stack.metrics_text()
            samples = await asyncio.gather(
                *(
                    chat(http, key, "bench-priced", prompt, no_store=no_store)
                    for _ in range(n)
                )
            )
            after = stack.metrics_text()
            out[label] = {
                "requests": n,
                "provider_calls": provider_calls(after, "mock-priced")
                - provider_calls(before, "mock-priced"),
                "statuses": dict(Counter(s.cache for s in samples)),
                "errors": sum(s.status != 200 for s in samples),
                "cost_billed_usd": str(sum((s.cost_usd for s in samples), Decimal(0))),
            } | latency_summary(ok_latencies(list(samples)))
    return out


# --- 4. resilience ---------------------------------------------------------------


async def resilience_sequential(key: str, n: int = 200) -> dict[str, object]:
    """One request at a time on a route whose primary always fails.

    Call on a freshly started gateway (closed breakers). For the first requests
    the gateway's metrics are read after each one, so it is known exactly which
    requests reached the failing primary.
    """
    async with client(1) as http:
        baseline = [
            await chat(
                http, key, "mock-1", f"baseline {i}", temperature=0.7, no_store=True
            )
            for i in range(100)
        ]
        clock0 = time.perf_counter()
        samples: list[Sample] = []
        touched: list[bool] = []
        calls_before = provider_calls(stack.metrics_text(), "mock-down")
        last = calls_before
        for i in range(n):
            samples.append(
                await chat(
                    http,
                    key,
                    "bench-fallback",
                    f"fallback {i}",
                    index=i,
                    clock0=clock0,
                    temperature=0.7,
                    no_store=True,
                )
            )
            if i < 20:
                now = provider_calls(stack.metrics_text(), "mock-down")
                touched.append(now > last)
                last = now
        final = stack.metrics_text()

    primary_calls = provider_calls(final, "mock-down") - calls_before
    tried = [s.latency_s for s, t in zip(samples, touched, strict=False) if t]
    skipped = [s.latency_s for s in samples[len(touched) :]] + [
        s.latency_s for s, t in zip(samples, touched, strict=False) if not t
    ]
    return {
        "requests": n,
        "succeeded": sum(s.status == 200 for s in samples),
        "answered_by": dict(Counter(s.provider for s in samples)),
        "primary_calls": primary_calls,
        "primary_calls_after_first_20": provider_calls(final, "mock-down") - last,
        "requests_that_reached_primary": sum(touched),
        "circuit_open_rejections": counter_total(
            final,
            "gateway_provider_attempts",
            {"provider": "mock-down", "outcome": "CircuitOpenError"},
        ),
        "baseline_mock_1": latency_summary(ok_latencies(baseline)),
        "before_breaker_opened": latency_summary(tried) if tried else None,
        "after_breaker_opened": latency_summary(skipped),
        "per_request_ms": [round(s.latency_s * 1000, 3) for s in samples],
        "reached_primary": touched,
    }


async def resilience_concurrent(
    key: str, n: int = 200, c: int = 10
) -> dict[str, object]:
    """The same failing primary, 10 requests in flight at a time (fresh gateway)."""
    async with client(c) as http:
        before = stack.metrics_text()

        def call(i: int) -> Awaitable[Sample]:
            return chat(
                http,
                key,
                "bench-fallback",
                f"fallback-c {i}",
                temperature=0.7,
                no_store=True,
            )

        samples, elapsed = await closed_loop(n, c, call)
        after = stack.metrics_text()
    return {
        "requests": n,
        "concurrency": c,
        "succeeded": sum(s.status == 200 for s in samples),
        "answered_by": dict(Counter(s.provider for s in samples)),
        "primary_calls": provider_calls(after, "mock-down")
        - provider_calls(before, "mock-down"),
        "seconds": round(elapsed, 2),
    } | latency_summary(ok_latencies(samples))


# --- 5. rate limiter -------------------------------------------------------------


async def rate_limit(
    key: str, limit_per_min: int, *, offered_rps: float = 50, duration_s: float = 60
) -> dict[str, object]:
    """Offer far more than the limit for a minute; count what gets through.

    Requests are sent on a fixed schedule (open loop), so the offered load does
    not depend on how fast refusals come back. The bucket starts full.
    """
    total = int(offered_rps * duration_s)
    interval = 1 / offered_rps
    async with client(200) as http:
        clock0 = time.perf_counter()

        async def send(i: int) -> Sample:
            delay = clock0 + i * interval - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
            return await chat(
                http,
                key,
                "mock-1",
                f"rl {i}",
                index=i,
                clock0=clock0,
                temperature=0.7,
                no_store=True,
            )

        samples = await asyncio.gather(*(send(i) for i in range(total)))

    allowed = [s for s in samples if s.status == 200]
    refused = [s for s in samples if s.status == 429]
    first_s = min(s.started_s for s in samples)
    last_s = max(s.started_s for s in samples)
    window = last_s - first_s
    rate = limit_per_min / 60
    sustained = [s for s in allowed if s.started_s - first_s >= 10]
    timeline = Counter(int(s.started_s - first_s) for s in allowed)
    return {
        "limit_per_min": limit_per_min,
        "offered_rps": offered_rps,
        "offered": total,
        "allowed": len(allowed),
        "refused_429": len(refused),
        "other_status": len(samples) - len(allowed) - len(refused),
        "refusals_with_retry_after": sum(s.retry_after is not None for s in refused),
        "window_s": round(window, 3),
        # A full bucket (capacity = limit) plus refill at limit/60 per second.
        "expected_allowed": round(limit_per_min + rate * window, 1),
        "allowed_first_second": sum(s.started_s - first_s < 1 for s in allowed),
        "sustained_per_min": round(len(sustained) / (window - 10) * 60, 1),
        "allowed_per_second": [timeline.get(t, 0) for t in range(int(window) + 1)],
    }
