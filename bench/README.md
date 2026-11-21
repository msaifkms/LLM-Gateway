# Benchmark suite

```bash
python -m pip install -r requirements-dev.txt
python -m bench                 # setup, 3 runs of every scenario, docs/benchmarks.md and charts
python -m bench --runs 1 --only cache,single_flight
python -m bench report          # rebuild the page from docs/benchmarks/results.json
```

The suite runs against the normal Docker Compose stack with `bench/docker-compose.bench.yml` on top. It costs nothing: only the Mock provider is called. Results and the method are in [docs/benchmarks.md](../docs/benchmarks.md).

## What it does

1. Starts the stack with `GATEWAY_BENCH_MODE=true`, which adds three benchmark-only Mock targets and routes (see `app/providers/registry.py` and `app/core/routes.py`). Then it migrates and creates a `bench` user with a very high rate limit, plus one user per run for the rate-limit scenario.
2. Runs every scenario `--runs` times (default 3), interleaved. The gateway is restarted before each one, so circuit breakers, single-flight and in-process metrics start clean.
3. Runs the scenarios in a `bench-client` container on the Compose network, so requests reach `gateway:8000` directly instead of through Docker Desktop's port forwarding. Keys go to the container on stdin, never as arguments or environment variables.
4. Writes `docs/benchmarks/results.json`, then renders `docs/benchmarks.md` and the SVG charts from it.

## Scenarios

| Scenario | What is measured | How |
|---|---|---|
| Network path | `/healthz` from the host vs from inside the network | Shows why the client runs in a container |
| Gateway overhead | Latency (p50/p95/p99) and throughput at 1, 10 and 50 concurrent requests, cache bypassed, on `mock-1` (0 ms) and `bench-delay-50` (50 ms). Also the 50 ms route at concurrency 1 with light background load | Client-side timing, plus the gateway's request and provider-call histograms, so its own time is measured separately. 10 and 50 are split across 5 client processes |
| Cache | HIT vs MISS latency, and a 1,000-request Zipf mix over 200 prompts: hit ratio, provider calls, cost billed vs without a cache | `X-Cache` and `X-Cost-USD` headers, and `gateway_provider_attempts_total` and `gateway_cost_saved_usd_total` deltas |
| Single-flight | 50 identical simultaneous requests: provider calls with the cache on vs bypassed | Provider-call counter deltas |
| Resilience | 200 requests (sequential, then 10 in flight) on a route whose primary always fails: success rate, calls to the failing primary, latency before and after the breaker opens | Metrics read after each of the first 20 requests, so it is known which requests reached the primary |
| Rate limiter | 50 requests/s for 60 s against a 120/min limit: allowed vs the token bucket's expected count | Fixed-schedule (open-loop) load and status codes |

## Notes

- On a laptop, keep it plugged in and awake. The runner holds a `caffeinate` assertion on macOS, but a closed lid or a low battery can still put the machine to sleep. A sleep pauses Docker's VM in the middle of a measurement.
- Everything else on the machine competes for the same CPUs, so close heavy apps and run nothing else.
- The users and keys the suite creates stay in the Compose database. `docker compose down -v` removes them.
