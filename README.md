
# LLM Gateway with Evaluation Harness

A backend and AI infrastructure project built with Python and FastAPI.

**Status:** In development — Phase 0 (Project Setup)

## Overview

This project aims to build an API gateway that sits between client applications and multiple LLM providers, such as OpenAI and Anthropic.

The gateway will provide a unified API for routing requests, handling provider failures, caching responses, enforcing rate limits, and tracking token usage and costs.

An evaluation harness will compare models and prompts using labeled test datasets.

## Planned Features

- Multi-provider LLM routing
- Automatic retries and failover
- Circuit breaker
- Redis response caching
- API key authentication and rate limiting
- Token and cost tracking
- Metrics and monitoring
- Automated model evaluation
- AWS deployment using Terraform

## Tech Stack

- Python, FastAPI, httpx
- PostgreSQL, Redis
- Docker, Docker Compose
- pytest
- Prometheus, Grafana
- AWS, Terraform
- GitHub Actions

## Development Roadmap

- [ ] Phase 0: Project setup
- [ ] Phase 1: Core API gateway
- [ ] Phase 2: Reliability and failover
- [ ] Phase 3: Caching
- [ ] Phase 4: Authentication, limits, and cost
- [ ] Phase 5: Observability
- [ ] Phase 6: Evaluation harness
- [ ] Phase 7: Cloud deployment
- [ ] Phase 8: Benchmarks and documentation

## Current Progress

Setting up the development environment, repository structure, Docker Compose, and automated checks.
