# LLM Gateway with Evaluation Harness

LLM Gateway is a service that sits between an application and AI model providers such as OpenAI and Anthropic. The application sends every request to one place, and the gateway checks who is calling, limits how much they can use, reuses answers it has already paid for, switches to a backup provider when one fails, and records what each request cost. It also includes an evaluation harness that scores the answers, so different models can be compared on the same questions.

**Highlights**

- **617 automated tests.** 568 run anywhere, and 49 more run against real Redis and PostgreSQL. CI runs all of them on every push.
- **Failover.** In a test where the primary provider fails every call, all 100 requests are answered by the backup provider. The circuit breaker opens after 5 failures, so the failing provider is called 5 times in total.
- **Caching.** A repeated request is answered from Redis without calling a provider, and costs $0. When 8 identical cacheable requests arrive at once, the gateway makes 1 provider call and gives all 8 the same answer.
- **Answer quality.** On the 27 starter eval cases, OpenAI `gpt-4.1-mini` passed 27 and Anthropic `claude-haiku-5-5` passed 26. The whole run cost $0.0026, and the harness's cost figures matched the gateway's own billing records.
- **A quality gate in CI.** Every push starts a real gateway, issues an API key, runs an eval against the Mock provider, and fails the build if the pass rate drops below 100%.
- **Observable.** 20 Prometheus metrics, all on a Grafana dashboard generated from code. A test fails if any metric is missing from it.

```mermaid
flowchart LR
    app[Client application] -->|HTTP + API key| gateway
    evals["Eval harness<br/>python -m app.evals"] -->|same API + API key| gateway

    subgraph gateway[Gateway - FastAPI]
        direction LR
        auth[Auth] --> limit[Rate limit] --> cache[Cache] --> router["Router<br/>retries, breakers, fallback"]
    end

    router --> openai[OpenAI]
    router --> anthropic[Anthropic]
    router --> mock[Mock provider]

    limit <--> redis[(Redis)]
    cache <--> redis
    auth <--> postgres[("PostgreSQL<br/>keys, usage, eval results")]
    evals --> postgres
    prometheus[Prometheus] -->|scrapes /metrics| gateway
    grafana[Grafana] --> prometheus
```

The current implementation supports OpenAI, Anthropic, and a Mock provider through one standardized endpoint. It adds priority fallback routing, retries, circuit breakers, a Redis response cache, API key authentication, per-key rate limits, daily quotas, per-request cost tracking, a global kill switch, structured logs, Prometheus metrics, a Grafana dashboard, and an evaluation harness that measures answer quality through the gateway's own API.

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
- Prometheus metrics (`GET /metrics`): traffic, latency, cache, providers, breakers, rate limits, quotas, kill switch, spend, failures and eval scores
- Grafana dashboard generated from code, with Prometheus and Grafana in Docker Compose
- Evaluation harness (`python -m app.evals`):
  - JSONL datasets with a loader that validates every case before anything is sent
  - Scorers: exact, normalized, contains, regex, JSON, and LLM-as-judge
  - Every case goes through the gateway's own API with an API key, like real traffic
  - Runs and per-case results stored in PostgreSQL
  - Reports with pass rate, mean score, latency, tokens, cost and errors
  - Run comparison that flags regressed cases
  - Several models in one command, side by side
  - Markdown reports (in `docs/evals/`)
  - Eval scores on the Grafana dashboard
  - An eval gate in CI against the Mock provider
- Automated unit, API, fault-injection, and real-Redis/Postgres integration tests

**Planned**

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
| `gateway_eval_pass_rate`, `gateway_eval_mean_score` | Pass rate and mean score (0 to 1) of the latest completed eval run, by dataset and model |
| `gateway_eval_last_run_timestamp_seconds` | When that run finished (the dashboard shows hours since the last eval) |

The eval CLI runs as a separate process that Prometheus does not scrape, so the gateway reads the latest eval results from PostgreSQL when `/metrics` is scraped, at most once every 30 seconds. They appear in the "Answer quality (evals)" row of the dashboard.

## Evaluation

Earlier phases measure speed, cost and reliability. The evaluation harness measures answer quality. It sends each case in a dataset to `POST /v1/chat/completions` with an ordinary API key, so evals go through authentication, rate limits, quotas, the cache and cost tracking just like real traffic. It scores each answer, stores the run and every result in PostgreSQL, and prints a report.

### Datasets

A dataset is a JSONL file with one case per line. Each case has an `id`, a `prompt` (optionally with a `system` message) or a full `messages` list, an `expected` answer or a `rubric`, a `scorer`, and optional `options`, `tags` and `max_tokens`:

```json
{"id": "capital-france", "prompt": "What is the capital of France? Reply with the city name only.", "expected": "Paris", "scorer": "normalized", "tags": ["geography"]}
```

The whole file is checked before anything is sent. Every problem is reported at once with its line number and case id, for example:

```
line 4 (id 'gold'): unknown option(s) ['case_sensitiv'] for scorer 'exact' (allowed: case_sensitive)
```

The starter datasets are in `app/evals/datasets/`, and you can use them by name:

| Dataset | Cases | What it checks |
|---|---|---|
| `factual_qa` | 11 | Short facts, plus two open questions graded by a judge |
| `instruction_following` | 9 | Output format and constraints (one word, a CSV list, all caps, exactly five words, bullets, a persona) |
| `json_output` | 7 | Valid JSON with the right keys and values |
| `mock_smoke` | 6 | The Mock provider's fixed reply; CI runs this one |

### Scorers

Each scorer gives a score from 0 to 1 and a pass or fail. A case where the gateway returned an error counts as failed with a score of 0.

- `exact`: the answer equals an expected string, ignoring surrounding whitespace.
- `normalized`: the answer matches after lowercasing and removing punctuation and extra spaces.
- `contains`: the answer contains the expected keywords (all of them, or any one with `"mode": "any"`).
- `regex`: a regular expression matches the answer, or the whole answer with `"full_match": true`.
- `json`: the answer is valid JSON, optionally with `required_keys`, exact `values`, or an `equals` value.
- `judge`: a judge model, called through the gateway, rates the answer from 1 to 5 against the `rubric` and gives a reason. It passes at 4 or higher by default.

New scorers are a subclass of `Scorer` in `app/evals/scorers.py` and a `register()` call.

### Commands

The CLI reads the API key from `EVAL_API_KEY` (or `--key`) and stores results in the database named by `DATABASE_URL`.

```bash
export EVAL_API_KEY=llmgw_...   # a key from "python -m app.cli issue-key"

# Check every built-in dataset (no gateway or database needed)
python -m app.evals validate

# Run one dataset on two models, with a judge, fresh answers and a Markdown report
python -m app.evals run --dataset factual_qa \
    --model openai-default --model anthropic-default \
    --judge-model openai-default --no-cache --concurrency 4 --markdown docs/evals/

# Fail (exit status 1) if any model's pass rate is below 90%
python -m app.evals run --dataset instruction_following --model chat-default --min-pass-rate 0.9

# List recent runs, show one, and compare two
python -m app.evals list
python -m app.evals report 3
python -m app.evals compare 5 6 --fail-on-regression
```

| `run` flag | Meaning |
|---|---|
| `--dataset` | A built-in dataset name or a path to a `.jsonl` file |
| `--model` | The model alias to evaluate. Repeat it to run several models and get a side-by-side summary |
| `--judge-model` | The model alias that grades `judge` cases (required if the dataset has any) |
| `--no-cache` | Send `Cache-Control: no-store`, so answers are fresh and latency is real |
| `--concurrency` | How many requests are in flight at once (default 4) |
| `--markdown` | Also write a Markdown report, to a `.md` file or into a directory |
| `--min-pass-rate` | Exit with status 1 if any model's pass rate is below this value (0 to 1) |
| `--tag`, `--limit` | Run only cases with a tag, or only the first N cases |
| `--temperature`, `--max-tokens`, `--timeout` | Request settings (temperature defaults to 0) |
| `--url` | The gateway URL (default: `GATEWAY_URL`, or `http://localhost:8000`) |
| `--no-db` | Print the report without storing anything |

`compare` matches cases by id. It flags a case as **regressed** if it passed before and fails now, and as **worse** if its score dropped by more than `--tolerance` (default 0.1). It also warns when the dataset file changed between the two runs. With `--fail-on-regression`, it exits with status 1 if anything was flagged.

The harness waits out a rate limit with a short `Retry-After` and retries, up to 5 times. A daily quota fails the case instead of waiting, and a rejected API key stops the run. Every request carries `X-Request-ID: eval-r<run>-<case>`, so a case can be found in the gateway logs. Cost is computed with the gateway's own price table, and cache hits cost $0.

### In Docker

The `evals` Compose service runs the same CLI on the gateway's network, with `GATEWAY_URL` and `DATABASE_URL` already set:

```bash
export EVAL_API_KEY=llmgw_...
docker compose run --rm evals validate
docker compose run --rm evals run --dataset mock_smoke --model mock-1 --model chat-test --markdown docs/evals/
docker compose run --rm evals compare 1 2
```

Reports written with `--markdown docs/evals/` land in the `docs/evals/` folder of your checkout. [docs/evals/README.md](docs/evals/README.md) has the full dataset format, every scorer option, and the sample reports.

### Results from real providers

One run of each starter dataset with `--no-cache`, on OpenAI (`gpt-4.1-mini`) and Anthropic (`claude-haiku-5-5`), with `openai-default` as the judge. The whole run cost $0.0026.

| Dataset | `openai-default` | `anthropic-default` |
|---|---|---|
| `factual_qa` | 11/11 | 11/11 |
| `instruction_following` | 9/9 | 8/9 (wrote a six-word sentence when asked for five) |
| `json_output` | 7/7 | 7/7 |

One sample per model is not a benchmark. Repeated runs and larger datasets are planned for Phase 8.

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
| `EVAL_API_KEY` | Gateway API key the eval CLI uses (instead of `--key`) | |
| `GATEWAY_URL` | Gateway the eval CLI calls (instead of `--url`) | `http://localhost:8000` (in the `evals` service: `http://gateway:8000`) |

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

Tests use simulated providers and fake clocks, so they cost nothing, need no network, and run in seconds. The eval tests drive the real app in-process with the Mock provider.

### The CI eval step, locally

CI also starts a real gateway and runs an eval against the Mock provider. To do the same with the test containers running:

```bash
export DATABASE_URL=postgresql://postgres:postgres@localhost:5432/gateway_test
export REDIS_URL=redis://localhost:6379/14
python -m app.db.migrate
uvicorn app.main:app --port 8000 &
python -m app.cli create-user ci-evals --rate-limit 600
export EVAL_API_KEY=$(python -m app.cli issue-key ci-evals | awk '$1 == "Key" && $2 == ":" {print $3}')
python -m app.evals run --dataset mock_smoke --model mock-1 --model chat-test --no-cache --min-pass-rate 1
python -m app.evals list
```

The integration tests reset the `gateway_test` database, so run this after them, or pick another user name if `ci-evals` already exists. Stop the gateway afterwards with `kill %1`.

## Development Progress

- [x] Phase 0: Project setup, Docker, testing, and CI
- [x] Phase 1: Core gateway API and provider integrations
- [x] Phase 2: Reliability, retries, fallback, and circuit breakers
- [x] Phase 3: Redis caching
- [x] Phase 4: Authentication, rate limiting, quotas, and cost tracking
- [x] Phase 5: Observability and metrics
- [x] Phase 6: Evaluation harness
- [ ] Phase 7: Cloud deployment
- [ ] Phase 8: Benchmarking and final documentation

## Project Status

**Phase 6 complete.**

Earlier phases made the gateway fast, cheap to run, reliable and observable. Phase 6 adds a way to measure answer quality: datasets of questions run through the gateway like real traffic, every answer is scored by rule or by an LLM judge, and the results are stored, reported, compared run against run, and charted on the dashboard. A model or setting that makes answers worse shows up as flagged cases, and CI fails the build if the harness itself breaks.

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
- The eval results above come from one run per model on 27 small cases. They show the harness works; they do not rank the models.
- An LLM judge can be biased or inconsistent, and it may favour answers that sound like its own. Judge scores are a signal, not ground truth. A judge reply that is not valid JSON counts as a scoring error.
- The Mock provider always gives the same reply and reports 0 tokens, so it can test the harness but cannot measure quality, tokens or cost. Those need OpenAI or Anthropic keys.
- The eval metrics show only the latest completed run per dataset and model (from the last 30 days), not a history. Use `python -m app.evals list` and `compare` for history.
- Eval results stored in PostgreSQL include each model's answers (but never API keys or headers), so do not put sensitive data in eval datasets.
- Only text-based, non-streaming chat completions are supported.

This project is under active development and is not yet intended for production use.

## License

MIT
