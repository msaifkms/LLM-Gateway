# LLM Gateway with Evaluation Harness

A backend infrastructure project designed to provide a unified API for interacting with multiple Large Language Model (LLM) providers.

The goal is to build a reliable, observable, and scalable LLM gateway with intelligent routing, caching, rate limiting, usage tracking, and automated model evaluation.

The current implementation supports OpenAI, Anthropic, and a Mock provider through a single standardized API endpoint, with priority-based fallback routing, automatic retries, per-provider circuit breakers, and a Redis response cache.

## Features

**Implemented**

- Unified chat completions API (`POST /v1/chat/completions`)
- OpenAI and Anthropic provider integrations
- Mock LLM provider with configurable latency and failure injection
- Priority routing with automatic fallback between providers
- Retries with exponential backoff and jitter, honoring `Retry-After`
- Per-provider circuit breakers (closed, open, half-open)
- Per-attempt timeouts and a whole-request deadline
- Error taxonomy separating retryable, fallback-eligible, and client errors
- Redis exact-match response cache with configurable TTL
- Cache rules per route, plus `Cache-Control: no-store` support
- Single-flight request coalescing (identical concurrent requests share one provider call)
- Fail-open caching: if Redis is down, the gateway keeps serving uncached
- Shared pooled HTTP client created at application startup
- `X-Provider` and `X-Cache` response headers
- Request and response validation using Pydantic
- Asynchronous HTTP communication using HTTPX
- Token usage normalization
- Automated unit, API, fault-injection, concurrency, and real-Redis integration tests

**Planned**

- Configurable provider routing from a YAML file
- Rate limiting and usage quotas
- API key authentication
- PostgreSQL usage and cost tracking
- Observability and performance monitoring
- Automated LLM evaluation harness
- AWS infrastructure and deployment

## Tech Stack

- **Backend:** Python, FastAPI, Uvicorn, Pydantic, HTTPX
- **Database:** PostgreSQL (integration planned)
- **Caching:** Redis (redis-py, asyncio)
- **Infrastructure:** Docker, Docker Compose
- **Testing:** pytest, Ruff, mypy
- **CI:** GitHub Actions (with a Redis service container), pre-commit
- **Cloud (planned):** AWS, Terraform

## How Requests Are Routed

Each model alias maps to a route: an ordered list of providers. The gateway tries them in order.

| Alias | Providers (in order) |
|---|---|
| `chat-default` | Anthropic, then OpenAI |
| `chat-test` | Mock |
| `mock-1` | Mock |
| `openai-default` | OpenAI |
| `anthropic-default` | Anthropic |

For each provider the gateway applies, from the outside in:

1. **Retry** (default: 2 retries) with exponential backoff and full jitter, only for transient errors (timeouts, connection errors, 429, 5xx). A `Retry-After` header is never undercut.
2. **Circuit breaker** (default: opens after 5 consecutive failures, 30 second cooldown, one half-open probe). An open circuit rejects calls instantly and the gateway moves on to the next provider.
3. **Timeout** (default: 20 seconds per attempt), inside an overall request deadline (default: 45 seconds).

A bad request (HTTP 400 from the provider) is never retried and never sent to another provider.

**Error responses**

| Situation | Status |
|---|---|
| Unsupported model alias | 400 |
| Provider rejected the request | 400 |
| Invalid request body | 422 |
| Provider timed out | 504 |
| Provider connection failure, server error, or malformed reply | 502 |
| Provider rate limit, authentication or configuration problem, or open circuit | 503 (with `Retry-After` when known) |

When every provider in a route fails, the error from the last one is returned. Error messages shown to clients are generic and never include provider responses or credentials.

## Caching

Identical requests are answered from Redis instead of calling a provider again.

**What is cached.** A response is cached only when the request is deterministic: `temperature` is `0`. The cache key is a SHA-256 hash of the route alias, messages, `temperature`, and `max_tokens`, so any change in those produces a different entry. Entries expire after `CACHE_TTL_SECONDS` (default 3600).

**Per-route rules.** Each route has a cache mode: `auto` (cache deterministic requests), `temperature_zero` (cache only at temperature 0), or `never`. Routes that can reach Anthropic need care: Anthropic's current API version ignores `temperature`, so its answers are not guaranteed to be deterministic and `anthropic-default` is never cached.

**Opting out.** Send `Cache-Control: no-store` to skip the cache for one request.

**The `X-Cache` header**

| Value | Meaning |
|---|---|
| `HIT` | Served from Redis; no provider was called |
| `MISS` | Not in the cache; a provider answered and the result was stored |
| `COALESCED` | An identical request was already in flight; this one shared its answer |
| `BYPASS` | Not cacheable (temperature above 0, `no-store`, a `never` route, or no Redis) |

**Stampede protection.** If many identical requests arrive while the answer is not cached yet, only the first calls the provider. The rest wait and share its result. Errors are shared too, and never cached.

**Fail-open.** Redis problems never fail a request. If Redis is unreachable, the gateway logs one warning per outage and serves every request uncached. Corrupt cache entries are treated as a miss.

## Getting Started

### Prerequisites

- Docker Desktop
- Docker Compose
- OpenAI or Anthropic API key (optional for Mock provider)

### Run Locally

Clone the repository:

```bash
git clone https://github.com/msaifkms/LLM-Gateway.git
cd LLM-Gateway
```

Create an environment configuration file:

```bash
cp .env.example .env
```

Add your API credentials to `.env` if using OpenAI or Anthropic. The Mock provider does not require API credentials.

Build and start the services:

```bash
docker compose up --build -d
```

Test the health endpoint:

```bash
curl http://localhost:8000/healthz
```

Expected response:

```json
{"status": "ok"}
```

### Test the LLM Gateway

Send a request using the Mock provider (send it twice to see the cache):

```bash
curl -i -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mock-1",
    "messages": [
      {
        "role": "user",
        "content": "Hello!"
      }
    ],
    "temperature": 0
  }'
```

The first call returns `X-Cache: MISS`; the second returns `X-Cache: HIT`. The `X-Provider` header shows which provider answered a miss.

**Supported model aliases:**

- `chat-default`: Anthropic with automatic fallback to OpenAI
- `chat-test`: Mock provider
- `mock-1`: Simulated LLM responses
- `openai-default`: OpenAI only
- `anthropic-default`: Anthropic only

**Configuration**

| Variable | Default | Purpose |
|---|---|---|
| `REDIS_URL` | unset (no caching) | Redis connection URL |
| `CACHE_TTL_SECONDS` | `3600` | How long cached responses live |

Interactive API documentation is available at:

http://localhost:8000/docs

Stop the services:

```bash
docker compose down
```

## Run Tests

From the configured local Python development environment:

```bash
python -m pytest -v
ruff check .
ruff format --check .
mypy app
```

Automated tests use simulated provider responses, a Mock provider with injected failures, and an in-memory fake Redis, so they cost nothing, need no network access, and run in seconds. Time-dependent behavior (backoff, breaker cooldowns, deadlines) is tested with a fake clock instead of real waiting.

**Tests against a real Redis** are skipped unless `TEST_REDIS_URL` is set. They use database 15 and flush it, so point them at a throwaway server:

```bash
docker run -d --name test-redis -p 6379:6379 redis:7-alpine
TEST_REDIS_URL=redis://localhost:6379/15 python -m pytest -q
docker rm -f test-redis
```

GitHub Actions runs these too, using a Redis service container.

## Development Progress

- [x] Phase 0: Project setup, Docker, testing, and CI configuration
- [x] Phase 1: Core LLM Gateway API and provider integrations
- [x] Phase 2: Reliability, retries, fallback, and circuit breakers
- [x] Phase 3: Redis caching
- [ ] Phase 4: Authentication, rate limiting, quotas, and cost tracking
- [ ] Phase 5: Observability and metrics
- [ ] Phase 6: Evaluation harness
- [ ] Phase 7: Cloud deployment
- [ ] Phase 8: Benchmarking and final documentation

## Project Status

**Phase 3 complete.**

Repeated deterministic requests are now served from Redis without calling a provider, identical concurrent requests share a single provider call, and the gateway keeps working if Redis goes down.

PostgreSQL is configured as a Docker service but is not yet integrated into the request pipeline.

**Current limitations**

- Routes are defined in code rather than a configuration file.
- Circuit breaker state and single-flight coalescing are kept in process memory, so they are not shared between gateway instances.
- Cache keys are not yet scoped to a user. This is safe while the gateway has no authentication, but must be fixed in Phase 4 so one user can never be served another user's cached answer.
- If Redis hangs rather than refusing connections, each cacheable request can wait up to about one second before falling back to the provider.
- Anthropic's current API version (`2026-10-21`) does not accept a `temperature` parameter, so the gateway's `temperature` field
