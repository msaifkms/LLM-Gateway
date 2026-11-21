# Cleanup and future work

What has been tidied up, and what is deliberately left for later, with the reason for each. The [design decisions](design.md) explain why things are built the way they are; this file is about what is not built yet.

## Done

**Code and tests**

- Tests no longer emit warnings, and any new warning fails the test run (`filterwarnings = ["error"]` in `pyproject.toml`). The pytest-asyncio event-loop scope is set explicitly. Starlette's TestClient runs on `httpx2`, as Starlette now asks; the gateway itself still uses `httpx`.
- Duplicated test helpers (a fake clock and a recording sleep, copied into several test files) are in one shared module, `tests/helpers.py`.
- Coverage of `app/` is measured in CI with pytest-cov, with Redis and Postgres running so every integration test counts. It was 96% (lines and branches) when the gate was set at 90%.
- Every dependency is pinned, direct and transitive, in `requirements.txt` and `requirements-dev.txt`. CI installs from them.
- The container is hardened: it runs as a non-root user (uid 10001), is a multi-stage build on a base image pinned by digest, has a health check, and on ECS has a read-only root filesystem.

**Operations**

- The AWS deployment was run end to end and torn down, and a check afterwards found nothing left in the account ([deploy.md](deploy.md#results-from-a-live-run)).
- The benchmark suite is reproducible with one command against the Mock provider ([benchmarks.md](benchmarks.md)).

## Left for later

Each item is real work rather than a quick fix, so it is recorded here instead of half-done.

| Item | Why it is not done | What it would take |
|---|---|---|
| Routes from a YAML file | Routes are a Python dict (`app/core/routes.py`). Five routes do not justify a config format, a loader and its validation yet. | A loader that builds the existing `Route` dataclass (which already validates itself), a schema, and a reload story (restart, or a signal). |
| Streaming responses (`stream: true`) | Caching, single-flight, usage recording and cost all assume one complete response. Streaming changes each of them. | Server-sent events from both providers; recording usage when the stream ends (or is cut off); deciding whether partial streams can be cached (they should not be). |
| Distributed tracing | The request ID on every log line covers a single process. There is no span tree across the gateway, Redis, Postgres and the provider call. | OpenTelemetry instrumentation for FastAPI, HTTPX, asyncpg and redis-py; a collector; and sampling. |
| Circuit breaker state shared across instances | Breakers live in process memory. With N instances, a dead provider is tried up to 5 times by *each* instance before every breaker opens. | Breaker state in Redis (with care, since Redis is optional and fails open), or accepting per-instance breakers and documenting N × 5. |
| Single-flight across instances | Each instance coalesces only its own requests, so a stampede costs at most one provider call per instance. | A Redis lock with expiry for crashed leaders, and waiters that poll or subscribe for the result. The benchmark suggests the in-process version already removes most of the waste. |
| More throughput per instance | One Uvicorn worker process levelled off at 828–848 chat requests/s on the benchmark laptop (from 10 to 50 concurrent requests). Past that, requests queue and p99 latency reaches 141 ms at 50 ([benchmarks](benchmarks.md#1-gateway-overhead)). | Several workers or instances behind the load balancer (which makes breakers, single-flight and metrics per process), and less work per request, for example batching usage writes to Postgres. Profile first. |
| Hard quotas | Quotas are checked before a request and recorded after it, so a user can overshoot by one request or one concurrent burst. | Reserving an estimated cost before the call and settling it afterwards. |
| Verified TLS to RDS | The gateway uses `sslmode=require` (encrypted, certificate not verified). Traffic stays inside the VPC. | Ship the RDS CA bundle in the image and use `verify-full`. |
| `/metrics` authentication | It holds only aggregate counters, and the AWS load balancer blocks it, but locally it is open. | A bearer token or network-level restriction for the scrape. |
| Prices from configuration | Prices are a dict in `app/usage/pricing.py` and must be checked by hand against the providers' pages. | A price file with an effective date, so a price change does not need a code change. |
| Model-quality comparisons at scale | The real-provider eval results come from one run per model on 27 small cases: enough to show the harness works, not to rank models. | Larger datasets, several runs per model, and confidence intervals on pass rates (a few dollars per run). |
| A multi-turn Playground | The web UI sends a single user message, with no system prompt or history. | A message list in the page, kept in the tab only, like the key. |
| HTTPS by default on AWS | It needs a domain and an ACM certificate. The Terraform supports both (`certificate_arn`), but defaults to HTTP. | A domain, then setting one variable. |
