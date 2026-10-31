# LLM Gateway with Evaluation Harness

A backend infrastructure project designed to provide a unified API for interacting with multiple Large Language Model (LLM) providers.

The goal is to build a reliable, observable, and scalable LLM gateway with intelligent routing, caching, rate limiting, usage tracking, and automated model evaluation.

The current implementation supports OpenAI, Anthropic, and a Mock provider through a single standardized API endpoint, with priority-based fallback routing, automatic retries, and per-provider circuit breakers.

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
- `X-Provider` response header showing which provider answered
- Request and response validation using Pydantic
- Asynchronous HTTP communication using HTTPX
- Token usage normalization
- Automated unit, API, and fault-injection tests

**Planned**

- Configurable provider routing from a YAML file
- Redis-based response caching
- Rate limiting and usage quotas
- API key authentication
- PostgreSQL usage and cost tracking
- Observability and performance monitoring
- Automated LLM evaluation harness
- AWS infrastructure and deployment

## Tech Stack

- **Backend:** Python, FastAPI, Uvicorn, Pydantic, HTTPX
- **Database:** PostgreSQL (integration planned)
- **Caching:** Redis (integration planned)
- **Infrastructure:** Docker, Docker Compose
- **Testing:** pytest, Ruff, mypy
- **CI:** GitHub Actions, pre-commit
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

Send a request using the Mock provider:

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
    ]
  }'
```

The gateway returns a standardized JSON response containing the generated message, model information, and token usage. The `X-Provider` header shows which provider answered.

**Supported model aliases:**

- `chat-default`: Anthropic with automatic fallback to OpenAI
- `chat-test`: Mock provider
- `mock-1`: Simulated LLM responses
- `openai-default`: OpenAI only
- `anthropic-default`: Anthropic only

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

Automated tests use simulated provider responses and a Mock provider with injected failures, so they cost nothing, need no network access, and run in seconds. Time-dependent behavior (backoff, breaker cooldowns, deadlines) is tested with a fake clock instead of real waiting.

## Development Progress

- [x] Phase 0: Project setup, Docker, testing, and CI configuration
- [x] Phase 1: Core LLM Gateway API and provider integrations
- [x] Phase 2: Reliability, retries, fallback, and circuit breakers
- [ ] Phase 3: Redis caching
- [ ] Phase 4: Authentication, rate limiting, quotas, and cost tracking
- [ ] Phase 5: Observability and metrics
- [ ] Phase 6: Evaluation harness
- [ ] Phase 7: Cloud deployment
- [ ] Phase 8: Benchmarking and final documentation

## Project Status

**Phase 2 complete.**

The gateway routes requests across OpenAI, Anthropic, and Mock providers with automatic retries, fallback, circuit breaking, and timeouts. Failure behavior is covered by automated fault-injection tests, including a scenario where the primary provider fails every call and every request is still served by the fallback.

PostgreSQL and Redis are configured as Docker services but are not yet integrated into the request pipeline.

**Current limitations**

- Routes are defined in code rather than a configuration file.
- Circuit breaker state is kept in process memory, so it is not shared between gateway instances.
- Anthropic's current API version (`2026-10-21`) does not accept a `temperature` parameter, so the gateway's `temperature` field has no effect on Anthropic requests.
- No caching, authentication, rate limiting, or persistent usage and cost tracking yet.
- Only text-based, non-streaming chat completions are supported.
- Provider-specific functionality may not be fully represented in the normalized API.

This project is under active development and is not yet intended for production use.

## License

MIT
