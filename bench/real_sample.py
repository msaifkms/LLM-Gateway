"""A small latency sample against real providers, through the gateway.

    python -m bench.real_sample --requests 10

This one COSTS MONEY (a fraction of a cent with the defaults): it sends real
requests to openai-default and anthropic-default, using the provider keys in
your .env. Run it only on purpose. It is not part of `python -m bench`.

For each route it reports the client's latency, the gateway's own time (its
request-handler time minus the provider-call time, from its histograms), and
the cost the gateway charged (X-Cost-USD). Results are written next to the
main benchmark results and rendered into docs/benchmarks.md by the report.
"""

import argparse
import asyncio
import json
import sys
import time
from decimal import Decimal

from bench import stack
from bench.scenarios import CHAT, client, histogram_mean_ms
from bench.stats import latency_summary

OUT = stack.ROOT / "docs" / "benchmarks" / "real_sample.json"
ROUTES = ("openai-default", "anthropic-default")
PROMPT = "In one short sentence, what does an API gateway do?"


async def sample(key: str, route: str, n: int) -> dict[str, object]:
    latencies: list[float] = []
    cost = Decimal(0)
    statuses: dict[int, int] = {}
    models: set[str] = set()
    async with client(1) as http:
        before = stack.metrics_text()
        for i in range(n):
            started = time.perf_counter()
            response = await http.post(
                CHAT,
                json={
                    "model": route,
                    # A different prompt each time, so nothing could be reused.
                    "messages": [{"role": "user", "content": f"{PROMPT} ({i})"}],
                    "temperature": 0,
                    "max_tokens": 40,
                },
                headers={"Authorization": f"Bearer {key}", "Cache-Control": "no-store"},
            )
            latency = time.perf_counter() - started
            statuses[response.status_code] = statuses.get(response.status_code, 0) + 1
            if response.status_code == 200:
                latencies.append(latency)
                cost += Decimal(response.headers.get("x-cost-usd", "0"))
                models.add(response.json()["model"])
        after = stack.metrics_text()
    server = histogram_mean_ms(
        before, after, "gateway_http_request_duration_seconds", {"route": CHAT}
    )
    provider = histogram_mean_ms(
        before, after, "gateway_provider_attempt_duration_seconds", {"provider": route}
    )
    result: dict[str, object] = {
        "route": route,
        "models": sorted(models),
        "statuses": statuses,
        "server_mean_ms": server,
        "provider_mean_ms": provider,
        "gateway_own_mean_ms": round(server - provider, 3),
        "cost_usd": str(cost),
    }
    if latencies:
        result |= latency_summary(latencies)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bench.real_sample")
    parser.add_argument("--requests", type=int, default=10)
    args = parser.parse_args(argv)
    if not 1 <= args.requests <= 50:
        parser.error("--requests must be between 1 and 50 (this costs money)")

    stack.up()
    stack.migrate()
    stack.wait_ready()
    key = stack.create_user_with_key("bench-real", stack.UNLIMITED_PER_MIN)
    results = []
    for route in ROUTES:
        stack.restart_gateway()
        results.append(asyncio.run(sample(key, route, args.requests)))
    total = sum(Decimal(str(r["cost_usd"])) for r in results)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(
            {
                "measured_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "requests_per_route": args.requests,
                "machine": stack.machine_info(),
                "results": results,
                "total_cost_usd": str(total),
            },
            indent=1,
        )
        + "\n"
    )
    for r in results:
        print(
            f"{r['route']}: p50 {r.get('p50_ms', 'n/a')} ms, gateway own "
            f"{r['gateway_own_mean_ms']} ms, statuses {r['statuses']}, cost ${r['cost_usd']}",
            file=sys.stderr,
        )
    print(f"total cost ${total}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
