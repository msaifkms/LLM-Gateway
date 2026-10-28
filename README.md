# LLM Gateway with Evaluation Harness

A backend infrastructure project designed to provide a unified API for interacting with multiple Large Language Model (LLM) providers.

The goal is to build a reliable, observable, and scalable LLM gateway with intelligent routing, caching, rate limiting, usage tracking, and automated model evaluation.

## Planned Features

- Unified API for multiple LLM providers (OpenAI, Anthropic)
- Provider routing, retries, and fallback mechanisms
- Redis-based response caching
- Rate limiting and usage quotas
- PostgreSQL usage and cost tracking
- Observability and performance monitoring
- Automated LLM evaluation harness
- Containerized deployment and AWS infrastructure

## Tech Stack

- **Backend:** Python, FastAPI, Uvicorn
- **Database:** PostgreSQL
- **Caching:** Redis
- **Infrastructure:** Docker, Docker Compose
- **Testing:** pytest, Ruff, mypy
- **CI:** GitHub Actions, pre-commit
- **Cloud (planned):** AWS, Terraform

## Getting Started

### Prerequisites

- Docker Desktop
- Docker Compose

### Run Locally

Clone the repository:

```bash
git clone https://github.com/msaifkms/LLM-Gateway.git
cd LLM-Gateway
```

Build and start the services:

```bash
docker compose up --build -d
```

Test the API:

```bash
curl http://localhost:8000/healthz
```

Expected response:

```json
{"status": "ok"}
```

Stop the services:

```bash
docker compose down
```

## Development Progress

- [x] Phase 0 — Project setup, Docker, testing, and CI configuration
- [ ] Phase 1 — Core LLM Gateway API
- [ ] Phase 2 — Reliability and provider routing
- [ ] Phase 3 — Caching, rate limiting, and usage tracking
- [ ] Phase 4 — Evaluation and observability
- [ ] Phase 5 — Cloud deployment

## Project Status

**In development.**

The initial FastAPI application, containerized development environment, and automated quality checks have been configured. LLM provider integration is the next development milestone.

## License

MIT

