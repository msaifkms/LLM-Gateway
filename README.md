    # LLM Gateway with Evaluation Harness

A backend infrastructure project designed to provide a unified API for interacting with multiple Large Language Model (LLM) providers.

The goal is to build a reliable, observable, and scalable LLM gateway with intelligent routing, caching, rate limiting, usage tracking, and automated model evaluation.

The current implementation supports OpenAI, Anthropic, and a Mock provider through a single standardized API endpoint.

## Features

**Implemented**
- Unified chat completions API (`POST /v1/chat/completions`)
- OpenAI and Anthropic provider integrations
- Mock LLM provider for cost-free testing
- Provider selection through model aliases
- Request and response validation using Pydantic
- Asynchronous HTTP communication using HTTPX
- Basic error handling and token usage normalization
- Automated unit and API tests

**Planned**
- Configurable provider routing, retries, and automatic fallback
- Circuit breakers and timeout management
- Redis-based response caching
- Rate limiting and usage quotas
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
curl -X POST http://localhost:8000/v1/chat/completions \
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

The gateway returns a standardized JSON response containing the generated message, model information, and token usage.

**Supported model aliases:**
- `mock-1` — Simulated LLM responses
- `openai-default` — OpenAI
- `anthropic-default` — Anthropic

Interactive API documentation is available at:

`http://localhost:8000/docs`

Stop the services:

```bash
docker compose down
```

### Run Tests

From the configured local Python development environment:

```bash
python -m pytest -v
ruff check .
ruff format --check .
mypy app
```

Automated tests use simulated provider responses to avoid unnecessary API costs.

## Development Progress

- [x] Phase 0 — Project setup, Docker, testing, and CI configuration
- [x] Phase 1 — Core LLM Gateway API and provider integrations
- [ ] Phase 2 — Reliability, retries, fallback, and circuit breakers
- [ ] Phase 3 — Redis caching
- [ ] Phase 4 — Authentication, rate limiting, and usage tracking
- [ ] Phase 5 — Evaluation harness and observability
- [ ] Phase 6 — Cloud deployment and benchmarking

## Project Status

**Phase 1 implemented locally; final CI verification pending.**

The gateway currently supports OpenAI, Anthropic, and Mock providers through a unified FastAPI endpoint. Automated tests, Docker containerization, and basic upstream error handling are implemented.

PostgreSQL and Redis are configured as Docker services but are not yet integrated into the request pipeline.

Future development will focus on reliability, distributed systems functionality, performance monitoring, and AWS deployment.

This project is under active development and is not yet intended for production use.

## License

MIT