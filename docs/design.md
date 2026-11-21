# Design decisions

Short records of the decisions that shape the gateway, each with its context, the decision, why it was made, and what else was considered. They describe the code as it is; [CLEANUP.md](CLEANUP.md) lists what is deliberately left for later.

- [1. Fail-open cache, fail-closed auth and quotas](#1-fail-open-cache-fail-closed-auth-and-quotas)
- [2. Exact-match cache, temperature 0 only](#2-exact-match-cache-temperature-0-only)
- [3. Single-flight in process](#3-single-flight-in-process)
- [4. Token bucket in Redis, updated by a Lua script](#4-token-bucket-in-redis-updated-by-a-lua-script)
- [5. Soft daily quotas](#5-soft-daily-quotas)
- [6. Kill switch in Postgres, cached for 2 seconds](#6-kill-switch-in-postgres-cached-for-2-seconds)
- [7. Money as Decimal](#7-money-as-decimal)
- [8. Some metrics are read at scrape time](#8-some-metrics-are-read-at-scrape-time)
- [9. Evals go through the public API](#9-evals-go-through-the-public-api)
- [10. The dashboard is code](#10-the-dashboard-is-code)
- [11. No NAT Gateway on AWS](#11-no-nat-gateway-on-aws)
- [12. Secrets through SSM and RDS-managed Secrets Manager](#12-secrets-through-ssm-and-rds-managed-secrets-manager)
- [13. Manual deploys](#13-manual-deploys)

## 1. Fail-open cache, fail-closed auth and quotas

**Context.** The gateway depends on Redis (cache, rate limits) and Postgres (keys, usage, quotas, kill switch). Either can be slow or down.

**Decision.** Redis failures *fail open*: a cache error is treated as a miss, and the request goes to the provider. Rate limiting fails open by default (`RATE_LIMIT_FAIL_OPEN=true`), and can be set to refuse with 503 instead. Postgres failures *fail closed*: if a key cannot be checked, the request gets a 503, never a free pass. Quota checks also fail closed. Redis calls have a 0.5 s timeout, so a slow Redis cannot slow requests down.

**Why.** Losing the cache costs money and latency but serves correct answers. Skipping authentication would let anyone in, and skipping quotas could run up an unbounded bill. Each dependency fails in the direction whose worst case is acceptable. Redis outages are counted (`gateway_redis_outages_total`), so failing open is visible.

**Alternatives.** Failing closed on Redis would turn a cache outage into a full outage. Failing open on Postgres would make the API keys meaningless during an outage. A local cache of key lookups would survive short Postgres outages, but a revoked key would keep working until its entry expired.

## 2. Exact-match cache, temperature 0 only

**Context.** Repeated identical requests are common (retries, evals, popular prompts), and every provider call costs money.

**Decision.** The cache key is a hash of the normalized request (model alias, messages, temperature, max tokens) and the user id. Only requests with `temperature == 0` are cached, and only on routes whose targets all honour temperature. A route can opt in explicitly (`CacheMode.TEMPERATURE_ZERO`) or out (`NEVER`). `Cache-Control: no-store` bypasses the cache.

**Why.** At temperature 0, a repeated request should get the same answer, so reusing it is safe. At higher temperatures the caller wants variety, and a cached answer would change behaviour. Scoping by user means one user can never receive another user's answer. Exact matching is simple to reason about and impossible to get subtly wrong.

**Alternatives.** A semantic cache (embedding similarity) would hit more often, but can return an answer to a *different* question. It also needs an embedding model and a threshold to tune. A shared cache across users would save more, but would leak data between them.

## 3. Single-flight in process

**Context.** When many identical cacheable requests arrive before the first one finishes, each misses the cache and calls the provider (a cache stampede).

**Decision.** Identical cacheable requests that are in flight at the same time in one gateway process share a single provider call: one leader calls, and the others wait for its result (`X-Cache: COALESCED`). The coordination is an in-memory dict of futures.

**Why.** It removes the stampede with no extra infrastructure and no network round trip. The benchmark measured 50 identical concurrent requests making 1 provider call, in each of three runs, against 50 calls with the cache bypassed ([benchmarks](benchmarks.md#3-single-flight)).

**Alternatives.** A Redis lock would coordinate across instances. But it adds a round trip to every miss, needs lock expiry for crashed leaders, and makes the cache path depend on Redis being healthy. With several instances, each one still coalesces its own requests, so the worst case is one provider call per instance. That is listed in [CLEANUP.md](CLEANUP.md).

## 4. Token bucket in Redis, updated by a Lua script

**Context.** Rate limits must hold across gateway instances and under concurrency.

**Decision.** Each user has a token bucket in Redis: capacity equal to the per-minute limit, refilling at limit/60 tokens per second. Check-and-spend is one Lua script, which Redis runs atomically, using Redis's own clock. Refused requests get 429 with `Retry-After`.

**Why.** A bucket allows short bursts while holding the long-run rate. Running it as one script means a hundred simultaneous requests cannot all see "1 token left". Using Redis's clock avoids clock skew between gateway instances. The benchmark measured 239 allowed requests in a 60-second window under 50 requests per second, against 240.0 expected from the bucket's formula, in each of three runs ([benchmarks](benchmarks.md#5-rate-limiter-accuracy)).

**Alternatives.** A fixed window counter (`INCR` with expiry) is simpler, but allows twice the limit across a window boundary. A sliding log is exact, but stores one entry per request. In-process limiting would not hold across instances.

## 5. Soft daily quotas

**Context.** Users can have daily token and USD caps. The real cost of a request is only known after the provider answers.

**Decision.** Before a provider call, the gateway checks the user's usage so far today (UTC) against their caps, and refuses with 429 if a cap is already reached. It does not reserve the request's cost in advance.

**Why.** Reserving an estimated cost would need a token estimate before the call, and a way to settle the difference afterwards. Checking first and recording after is simple and correct to within one request. Cache hits cost nothing and skip the check.

**Alternatives.** Hard quotas with reservations would close the gap, at the cost of an estimator and a two-phase write. The overshoot (one request, or one burst of concurrent requests) is documented as a limitation.

## 6. Kill switch in Postgres, cached for 2 seconds

**Context.** An operator needs to stop all chat traffic quickly, for example during a provider incident or a runaway cost, without a deploy.

**Decision.** The switch is a row in Postgres, flipped by the admin CLI. Each gateway process caches its value for up to 2 seconds. When on, chat requests get 503.

**Why.** Postgres is already required, is shared by every instance, and survives restarts. The 2-second cache keeps the check off the hot path (one query per process every 2 seconds, not one per request), while a flip still takes effect within seconds.

**Alternatives.** An environment variable needs a restart or redeploy. Redis would be faster, but Redis is optional here. A pub/sub push would be instant, but adds a long-lived connection to manage.

## 7. Money as Decimal

**Context.** The gateway computes and stores the cost of every request: fractions of a cent, summed over many requests.

**Decision.** Prices and costs are Python `Decimal`, stored as `NUMERIC(14, 8)` in Postgres, and sent in JSON as fixed-point strings (`"0.00120000"`, never `"1.2E-3"`). Floats are used only for Prometheus counters.

**Why.** Binary floats cannot represent most decimal amounts exactly, and the errors accumulate in sums. Fixed-point strings survive any JSON parser unchanged. The eval harness's cost figures matched the gateway's billing records to the cent because both use the same Decimal price table.

**Alternatives.** Integer micro-dollars would also be exact, but are easy to mix up with dollars. Floats were rejected for the reason above.

## 8. Some metrics are read at scrape time

**Context.** Some values change without a request: a circuit breaker moves from open to half-open as time passes, the kill switch can be flipped by the CLI, and eval runs are written by a separate process.

**Decision.** These gauges are updated when `/metrics` is scraped: breaker states from the router, the kill switch from its cache, and the latest eval scores from Postgres (at most once every 30 seconds). Counters and histograms are updated on the request path as usual.

**Why.** A gauge set only when something happens would show stale state, for example "open" long after a breaker could have recovered. Reading at scrape time gives the true current value, at a cost paid only when Prometheus asks.

**Alternatives.** A background task refreshing the gauges would add moving parts and still lag. Having the eval CLI push to a Pushgateway would add a service to run.

## 9. Evals go through the public API

**Context.** The eval harness needs model answers to score.

**Decision.** The harness calls the gateway's own `POST /v1/chat/completions` with an ordinary API key, like any client. It does not call providers directly or import the router.

**Why.** The eval then measures what users actually get: the same routing, fallback, cache, auth, rate limits, quotas and cost tracking. Eval traffic shows up in `/v1/usage` and the dashboards, and an eval that would hit a quota is refused like real traffic. Since migration 005, each run belongs to the key's owner, so users see their own runs in the web UI.

**Alternatives.** Calling providers directly would be faster to build, but would test something different from production. It would also miss routing and cache bugs, and would need its own cost accounting.

## 10. The dashboard is code

**Context.** Hand-edited Grafana JSON drifts from the metrics it queries, and the drift is only noticed when a panel goes blank.

**Decision.** `ops/grafana/build_dashboard.py` generates the dashboard JSON. Tests fail if the committed JSON is out of date, if a panel queries a metric that does not exist, or if a metric has no panel.

**Why.** Renaming or adding a metric then forces the dashboard to be updated in the same change. Reviewing a Python diff is easier than reviewing generated JSON.

**Alternatives.** Grafonnet (Jsonnet) or the Terraform Grafana provider would also work, but add a language or a running Grafana to the build.

## 11. No NAT Gateway on AWS

**Context.** Tasks in private subnets need a NAT Gateway to reach the internet: the LLM providers, and AWS APIs such as ECR and CloudWatch Logs. A NAT Gateway costs about $33 a month per AZ before data, which would be the largest line on this project's bill.

**Decision.** Gateway tasks run in public subnets with a public IP. Their security group allows inbound traffic only from the load balancer's security group, on port 8000, and outbound only HTTPS, Postgres and Redis. The database and Redis sit in private subnets with no route out.

**Why.** It costs about $3.65 a month per task (public IPv4) instead of $66 for two NAT Gateways. VPC interface endpoints would cost more than NAT, and still could not reach OpenAI or Anthropic. The security group, not the subnet, is what keeps the tasks closed. [deploy.md](deploy.md#design-decisions) has the comparison table.

**Alternatives.** Private subnets with NAT Gateways are the standard production layout, and the right choice with a budget. A NAT instance is cheaper than a NAT Gateway, but is a server to patch and keep alive.

## 12. Secrets through SSM and RDS-managed Secrets Manager

**Context.** The task needs the database password and the provider API keys. They must not be in git, and preferably not in Terraform state, since anyone who can read the state could read them.

**Decision.** RDS generates the database password and keeps it in a Secrets Manager secret that it manages (`manage_master_user_password`), so Terraform never sees it. Provider keys are SSM SecureString parameters that Terraform creates with a placeholder and then ignores, and the real values are set with the AWS CLI. ECS injects both at task start. API keys issued in the cloud are handed over through SSM (`issue-key --to-ssm`), so they are never printed into CloudWatch Logs.

**Why.** Secrets stay out of code, plans and state, and the task gets them without baking them into the image or the task definition's plain environment.

**Alternatives.** Terraform-generated passwords (`random_password`) end up in state. Plain environment variables in the task definition are visible to anyone who can describe it. ECS Exec for admin commands was avoided, because the AWS free plan may block the SSM session service.

## 13. Manual deploys

**Context.** The plan was GitHub Actions deploying with OIDC: short-lived credentials and no stored AWS keys. The project's AWS account is on the free plan, whose service control policy denies the IAM OIDC provider actions.

**Decision.** Deploys run from a laptop with `infra/scripts/deploy.sh`: build, push, register a task definition, run migrations as a one-off task, roll out, and check `/healthz`. The CI deploy job, and the Terraform for the OIDC role, are in place behind a repository variable and `enable_github_oidc`. The job skips cleanly when either is missing, or when the infrastructure is destroyed.

**Why.** Storing long-lived AWS keys in GitHub to get automatic deploys would be a worse trade than deploying by hand. The script is the same one CI would run, so switching later is a configuration change, not a rewrite.

**Alternatives.** Long-lived IAM user keys in GitHub secrets (rejected, as above). AWS CodePipeline (more infrastructure, and more cost, for one service).
