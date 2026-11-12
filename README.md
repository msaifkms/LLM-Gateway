# LLM Gateway with Evaluation Harness

A backend infrastructure project that provides a unified API for interacting with multiple Large Language Model (LLM) providers.

The goal is to build a reliable, observable, and scalable LLM gateway with intelligent routing, caching, authentication, rate limiting, cost tracking, and automated model evaluation.

The current implementation supports OpenAI, Anthropic, and a Mock provider through one standardized endpoint. It adds priority fallback routing, retries, circuit breakers, a Redis response cache, API key authentication, per-key rate limits, daily quotas, per-request cost tracking, a global kill switch, structured logs, Prometheus metrics, and a Grafana dashboard.

![LLM Gateway dashboard](docs/dashboard.png)

## Features

**Implemented**

- Unified chat completions API (`POST /v1/chat/completions`)
- OpenAI and Anthropic provider integrations, plus a Mock provider with latency and failure injection
- Priority routing with fallback, retries with backoff and jitter, per-provider circuit breakers, timeouts and a request deadline
- Redis exact-match response cache (fail-open), `Cache-Control: no-store`, and single-flight stampede protection
- API key authentication (keys are shown once; only a SHA-256 hash is stored)
- Per-user cache scoping, so one user never receives another user's cached answer
- Per-key token-bucket rate limiting in Redis (atomic Lua script), with `X-RateLimit-*` and `Retry-After` headers
- Per-request usage and cost recording in PostgreSQL (`cache HIT` and `COALESCED` cost $0 and record the saving)
- Usage report endpoint (`GET /v1/usage`)
- Daily token and USD quotas per user (UTC day)
- Global kill switch stored in PostgreSQL
- Admin CLI for users, keys, limits and the kill switch
- Numbered SQL migrations
- Structured JSON logs with a request ID on every line (`X-Request-ID`)
- Prometheus metrics (`GET /metrics`): traffic, latency, cache, providers, breakers, rate limits, quotas, kill switch, spend and failures
- Grafana dashboard generated from code, with Prometheus and Grafana in Docker Compose
- Automated unit, API, fault-injection, and real-Redis/Postgres integration tests

**Planned**

- Automated LLM evaluation harness
- AWS infrastructure and deployment (Terraform)
- Benchmarks and final documentation
- Configurable routes from a YAML file
- Distributed tracing

## Tech Stack

- **Backend:** Python, FastAPI, Uvicorn, Pydantic, HTTPX
- **Database:** PostgreSQL 16 (asyncpg)
- **Cache and rate limiting:** Redis 7 (redis-py asyncio)
- **Observability:** structured JSON logging, Prometheus, Grafana
- **Infrastructure:** Docker, Docker Compose
- **Testing:** pytest, Ruff, mypy
- **CI:** GitHub Actions (with Redis and Postgres services), pre-commit
- **Cloud (planned):** AWS, Terraform

## Request Pipeline

```
Client request
   ↓
Request ID + timing (middleware)                    → X-Request-ID, log line, metrics
   ↓
Kill switch (Postgres flag, cached up to 2s)       → 503 if on
   ↓
Authenticate (Bearer API key)                       → 401 / 503
   ↓
Rate limit (Redis token bucket)                     → 429 + Retry-After
   ↓
Cache lookup (Redis, scoped to the user)            → HIT: return, cost $0
   ↓ (miss / bypass)
Quota check (daily tokens and USD from Postgres)    → 429 + Retry-After
   ↓
Single-flight (identical requests share one call)
   ↓
Router → retry → circuit breaker → timeout → provider
   ↓
Record usage and cost (Postgres) → response
```

### Model aliases

| Alias | Providers (in order) |
|---|---|
| `chat-default` | Anthropic, then OpenAI |
| `chat-test` | Mock |
| `mock-1` | Mock |
| `openai-default` | OpenAI |
| `anthropic-default` | Anthropic |

### Response headers

| Header | Meaning |
|---|---|
| `X-Request-ID` | ID of this request, also on every log line it produced (a valid incoming `X-Request-ID` is reused) |
| `X-Provider` | Which provider answered |
| `X-Cache` | `HIT`, `MISS`, `COALESCED` or `BYPASS` |
| `X-RateLimit-Limit` / `X-RateLimit-Remaining` | The key's per-minute limit and tokens left |
| `Retry-After` | Seconds to wait (rate limit, quota, kill switch, upstream limits) |

### Error responses

| Situation | Status |
|---|---|
| Unsupported model alias, or provider rejected the request | 400 |
| Missing, invalid or revoked API key | 401 |
| Invalid request body | 422 |
| Rate limit or daily quota reached | 429 (with `Retry-After`) |
| Provider timed out | 504 |
| Provider connection failure, server error, or malformed reply | 502 |
| Provider limit, provider auth or config problem, open circuit, kill switch on, database unavailable | 503 |

Error messages shown to clients are generic and never include provider responses or credentials.

## Endpoints

| Endpoint | Auth | Purpose |
|---|---|---|
| `POST /v1/chat/completions` | API key | Chat completion |
| `GET /v1/usage?days=7` | API key | Your tokens, cost, savings, by model and by UTC day (1 to 90 days) |
| `GET /healthz` | none | Liveness (process is up) |
| `GET /readyz` | none | Readiness (PostgreSQL reachable) |
| `GET /metrics` | none | Prometheus metrics (aggregate counters only, no prompts or keys) |
| `GET /docs` | none | Interactive API documentation |

## Getting Started

### Prerequisites

- Docker Desktop and Docker Compose
- OpenAI or Anthropic API key (optional; the Mock provider needs none)

### Run locally

```bash
git clone https://github.com/msaifkms/LLM-Gateway.git
cd LLM-Gateway
cp .env.example .env
docker compose up --build -d
docker compose run --rm gateway python -m app.db.migrate
```

Add provider credentials to `.env` if you want real providers.

### Create a user and an API key

```bash
docker compose run --rm gateway python -m app.cli create-user alice
docker compose run --rm gateway python -m app.cli issue-key alice
```

The key is printed once. Store it.

### Send a request

```bash
export KEY=llmgw_...
curl -i -X POST http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"mock-1","messages":[{"role":"user","content":"Hello!"}],"temperature":0}'

curl -H "Authorization: Bearer $KEY" "http://localhost:8000/v1/usage?days=7"
```

Stop the services with `docker compose down`.

## Observability

Every request writes one JSON log line with its request ID, method, path, status, duration, cache outcome and provider. Prompts, keys and headers are never logged.

To run Prometheus and Grafana alongside the gateway:

```bash
docker compose --profile observability up -d --build
docker compose run --rm gateway python -m app.db.migrate
python scripts/traffic.py --key $KEY --requests 400
```

- Grafana: http://localhost:3000 (the LLM Gateway dashboard is the home page; viewing needs no login)
- Prometheus: http://localhost:9090 (check http://localhost:9090/targets shows the gateway as UP)

Stop everything with `docker compose --profile observability down`.

The dashboard is generated from `ops/grafana/build_dashboard.py`. After changing a panel, run `python ops/grafana/build_dashboard.py`. A test fails if the JSON is out of date, if a panel queries a metric that does not exist, or if a metric is missing from the dashboard.

| Metric | What it shows |
|---|---|
| `gateway_http_requests_total`, `gateway_http_request_duration_seconds` | Traffic and latency by route template and status |
| `gateway_cache_requests_total` | Cache outcomes |
| `gateway_provider_attempts_total`, `gateway_provider_attempt_duration_seconds` | Provider attempts by outcome, and their latency |
| `gateway_fallbacks_total`, `gateway_circuit_breaker_state` | Fallbacks and breaker state per provider |
| `gateway_rate_limit_events_total`, `gateway_quota_rejections_total` | Rate-limit and quota refusals |
| `gateway_kill_switch_on`, `gateway_kill_switch_blocks_total` | Kill switch state and blocked requests |
| `gateway_tokens_total`, `gateway_cost_usd_total`, `gateway_cost_saved_usd_total` | Tokens and dollars billed, and dollars saved by the cache |
| `gateway_unpriced_requests_total`, `gateway_usage_write_failures_total`, `gateway_redis_outages_total` | Missing prices, lost usage rows, Redis outages |

## Admin CLI

```bash
python -m app.cli create-user NAME [--rate-limit N] [--daily-tokens N] [--daily-usd X]
python -m app.cli issue-key NAME
python -m app.cli revoke-key KEY_ID
python -m app.cli list-keys
python -m app.cli set-limits NAME [--rate-limit N] [--daily-tokens N|none] [--daily-usd X|none]
python -m app.cli kill-switch on|off|status [--reason TEXT]
```

In Docker, prefix with `docker compose run --rm gateway`. Keys can only be created from the CLI; there is no HTTP admin API.

## Configuration

| Variable | Purpose | Default |
|---|---|---|
| `DATABASE_URL` | PostgreSQL connection (required) | set in compose |
| `DB_POOL_MAX` | Maximum Postgres connections | see `.env.example` |
| `REDIS_URL` | Redis connection (optional; no Redis means no cache and no rate limits) | set in compose |
| `REDIS_TIMEOUT_S` | Redis connect/command timeout | see `.env.example` |
| `CACHE_TTL_SECONDS` | Cache entry lifetime | see `.env.example` |
| `RATE_LIMIT_FAIL_OPEN` | Allow requests if Redis is down (`true`) or reject them with 503 (`false`) | `true` |
| `LOG_LEVEL` | Log level | `INFO` |
| `LOG_FORMAT` | `json` for one JSON object per line, `text` for local reading | `json` |
| `OPENAI_API_KEY`, `OPENAI_MODEL` | OpenAI credentials and model | |
| `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` | Anthropic credentials and model | |

## Run Tests

Without services (integration tests are skipped):

```bash
python -m pytest -q
ruff check .
ruff format --check .
mypy app
```

With real Redis and Postgres:

```bash
docker run -d --name test-redis -p 6379:6379 redis:7-alpine
docker run -d --name test-postgres -p 5432:5432 -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=gateway_test postgres:16
TEST_REDIS_URL=redis://localhost:6379/15 \
TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/gateway_test \
python -m pytest -q
```

Next time, `docker start test-redis test-postgres` is enough. Stop them before running Docker Compose, since they use the same ports.

Tests use simulated providers and fake clocks, so they cost nothing, need no network, and run in seconds.

## Development Progress

- [x] Phase 0: Project setup, Docker, testing, and CI
- [x] Phase 1: Core gateway API and provider integrations
- [x] Phase 2: Reliability, retries, fallback, and circuit breakers
- [x] Phase 3: Redis caching
- [x] Phase 4: Authentication, rate limiting, quotas, and cost tracking
- [x] Phase 5: Observability and metrics
- [ ] Phase 6: Evaluation harness
- [ ] Phase 7: Cloud deployment
- [ ] Phase 8: Benchmarking and final documentation

## Project Status

**Phase 5 complete.**

Every request can be followed through the logs by its request ID, and the gateway's traffic, latency, cache efficiency, provider health, protections and spend are visible as Prometheus metrics and on a Grafana dashboard.

**Current limitations**

- Routes are defined in code rather than a configuration file.
- Circuit breaker and single-flight state live in process memory and are not shared between gateway instances.
- Metrics are per process: with several gateway instances, Prometheus must scrape each one.
- `/metrics` has no authentication. It holds only aggregate counters, but should not be exposed to the internet in a deployment.
- Rate limits need Redis; without Redis there are none.
- Quotas are soft: a request is checked before it runs, so a user can overshoot by one request or one concurrent burst.
- The kill switch needs PostgreSQL, takes up to 2 seconds to propagate, and does not cancel requests already in flight.
- Prices are kept in code (`app/usage/pricing.py`). Verify them against the providers' pricing pages before relying on them.
- Savings figures assume a cached answer would have cost the same as the original call.
- Anthropic's current API version does not accept `temperature`, so Anthropic answers are never cached.
- No distributed tracing yet.
- Only text-based, non-streaming chat completions are supported.

This project is under active development and is not yet intended for production use.

## License

MIT
