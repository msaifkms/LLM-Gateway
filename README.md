# LLM Gateway with Evaluation Harness

A backend infrastructure project that provides a unified API for interacting with multiple Large Language Model (LLM) providers.

The goal is to build a reliable, observable, and scalable LLM gateway with intelligent routing, caching, authentication, rate limiting, cost tracking, and automated model evaluation.

The current implementation supports OpenAI, Anthropic, and a Mock provider through one standardized endpoint. It adds priority fallback routing, retries, circuit breakers, a Redis response cache, API key authentication, per-key rate limits, daily quotas, per-request cost tracking, and a global kill switch.

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
- Automated unit, API, fault-injection, and real-Redis/Postgres integration tests

**Planned**

- Observability: structured logs, metrics, tracing
- Automated LLM evaluation harness
- AWS infrastructure and deployment (Terraform)
- Benchmarks and final documentation
- Configurable routes from a YAML file

## Tech Stack

- **Backend:** Python, FastAPI, Uvicorn, Pydantic, HTTPX
- **Database:** PostgreSQL 16 (asyncpg)
- **Cache and rate limiting:** Redis 7 (redis-py asyncio)
- **Infrastructure:** Docker, Docker Compose
- **Testing:** pytest, Ruff, mypy
- **CI:** GitHub Actions (with Redis and Postgres services), pre-commit
- **Cloud (planned):** AWS, Terraform

## Request Pipeline

```
Client request
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

Tests use simulated providers and fake clocks, so they cost nothing, need no network, and run in seconds.

## Development Progress

- [x] Phase 0: Project setup, Docker, testing, and CI
- [x] Phase 1: Core gateway API and provider integrations
- [x] Phase 2: Reliability, retries, fallback, and circuit breakers
- [x] Phase 3: Redis caching
- [x] Phase 4: Authentication, rate limiting, quotas, and cost tracking
- [ ] Phase 5: Observability and metrics
- [ ] Phase 6: Evaluation harness
- [ ] Phase 7: Cloud deployment
- [ ] Phase 8: Benchmarking and final documentation

## Project Status

**Phase 4 complete.**

The gateway authenticates callers, limits their rate and daily spend, records every paid call with its cost, and can be shut off instantly. All of it is covered by unit tests and by integration tests against real Redis and PostgreSQL.

**Current limitations**

- Routes are defined in code rather than a configuration file.
- Circuit breaker and single-flight state live in process memory and are not shared between gateway instances.
- Rate limits need Redis; without Redis there are none.
- Quotas are soft: a request is checked before it runs, so a user can overshoot by one request or one concurrent burst.
- The kill switch needs PostgreSQL, takes up to 2 seconds to propagate, and does not cancel requests already in flight.
- Prices are kept in code (`app/usage/pricing.py`). Models not in the table are recorded as unpriced with cost 0 and a warning. Verify the prices against the providers' pricing pages before relying on them.
- Savings figures assume a cached answer would have cost the same as the original call.
- Anthropic prompt-cache pricing is not modeled.
- Anthropic's current API version does not accept `temperature`, so Anthropic answers are never cached.
- The cache TTL is global, and a hanging Redis can add about one second to a cacheable request.
- Only text-based, non-streaming chat completions are supported.

This project is under active development and is not yet intended for production use.

## License

MIT
