# Evaluation harness

The harness measures answer quality. It sends each case in a dataset to the gateway's own `POST /v1/chat/completions` with a normal API key, so evals go through the same auth, rate limits, quotas, cache and cost tracking as real traffic. It scores each answer, stores the run in Postgres, and prints a report.

```bash
python -m app.evals validate                                   # check every dataset
python -m app.evals run --dataset factual_qa \
    --model openai-default --model anthropic-default \
    --judge-model openai-default --no-cache --markdown docs/evals/
python -m app.evals list                                       # recent runs
python -m app.evals report 3                                   # one stored run
python -m app.evals compare 5 6 --fail-on-regression           # two runs side by side
```

Each stored run belongs to the user who owns that key, so they can see it in the web UI (`/ui/evals`) and through `GET /v1/evals/runs`. Other users cannot. The key comes from `--key` or the `EVAL_API_KEY` environment variable. The variable is better, because it keeps the key out of your shell history. The key is sent only in the `Authorization` header and is never printed, logged or stored.

## Datasets

A dataset is a JSONL file with one case per line. The starter datasets live in [`app/evals/datasets/`](../../app/evals/datasets/), and you can refer to them by name (`--dataset factual_qa`) or pass any file path.

| Dataset | Cases | What it checks |
|---|---|---|
| `factual_qa` | 11 | Short facts (exact, normalized, contains, regex) and two open answers graded by a judge |
| `instruction_following` | 9 | Output format and constraints: one word, a CSV list, all caps, five words, bullets, a persona |
| `json_output` | 7 | Valid JSON with the right keys and values, arrays, nesting, escaped quotes |
| `mock_smoke` | 6 | Expects the Mock provider's fixed reply; CI runs it on every push |

The three starter datasets have 27 cases in total, so a run against a real provider costs a fraction of a cent.

### Case fields

```json
{"id": "capital-france", "prompt": "What is the capital of France? Reply with the city name only.",
 "expected": "Paris", "scorer": "normalized", "tags": ["geography"]}
```

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | Unique within the file. Letters, digits, `.`, `_` and `-`, up to 100 characters |
| `prompt` or `messages` | one of them | A single user prompt, or a full chat (`[{"role": "user", "content": "..."}]`) that ends with a user message |
| `system` | no | A system message, used with `prompt` |
| `scorer` | yes | One of the scorers below |
| `expected` | depends on the scorer | A string or a list of strings |
| `rubric` | for `judge` | What a good answer looks like |
| `options` | no | Scorer settings (see below). Unknown options are rejected, so typos are caught |
| `tags` | no | Labels, used to select cases with `--tag` |
| `max_tokens` | no | Overrides `--max-tokens` for this case |

The whole file is validated before anything is sent. Every problem is reported at once, with its line number and case id:

```
error: invalid dataset my.jsonl:
  line 4 (id 'gold'): unknown option(s) ['case_sensitiv'] for scorer 'exact' (allowed: case_sensitive)
  line 9 (id 'bad-regex'): 'expected' is not a valid regex (missing ), unterminated subpattern at position 0)
```

## Scorers

Every scorer returns a score from 0 to 1 and a pass or fail. A case where the gateway returned an error counts as failed, with a score of 0.

| Scorer | Passes when | `expected` | Options |
|---|---|---|---|
| `exact` | The answer equals an expected string (surrounding whitespace ignored) | string or list | `case_sensitive` (default true) |
| `normalized` | They are equal after lowercasing and removing punctuation and extra spaces | string or list | |
| `contains` | The answer contains the keywords. The score is the fraction found | list of keywords | `mode`: `all` (default) or `any`; `case_sensitive` (default false) |
| `regex` | The pattern matches anywhere, or the whole answer with `full_match` | one pattern | `full_match`, `ignore_case`, `multiline`, `dotall` |
| `json` | The answer is valid JSON (a ```` ```json ```` fence is allowed) that meets the checks. The score is the fraction of checks passed | | `required_keys`, `values` (key: exact value), `equals` (the whole value), `allow_fences` (default true) |
| `judge` | A judge model rates the answer at least `pass_score` out of 5 against the rubric | optional reference answer | `pass_score` (1 to 5, default 4) |

### LLM as judge

`judge` cases need `--judge-model`, which is a model alias called through the gateway like any other request. The judge receives the question, the rubric, the reference answer if there is one, and the answer. It has to reply with `{"score": 1-5, "reason": "..."}` and nothing else. A rating of 1 to 5 maps to a score of 0, 0.25, 0.5, 0.75 or 1. A reply that isn't a valid verdict is recorded as a scoring error rather than a silent 0. The judge's tokens and cost are reported separately from the answers'.

### Adding a scorer

Subclass `Scorer` in [`app/evals/scorers.py`](../../app/evals/scorers.py) and register it. `check` runs when the dataset loads, and `score` runs for each answer.

```python
class StartsWith(Scorer):
    name = "starts_with"
    options = frozenset({"case_sensitive"})

    def check(self, case: EvalCase) -> None:
        expected_strings(case)  # raises ValueError if 'expected' is missing

    async def score(self, case: EvalCase, output: str, judge: Judge | None) -> Score:
        ok = output.strip().startswith(expected_strings(case)[0])
        return Score.from_bool(ok, "" if ok else "wrong start")


register(StartsWith())
```

## Running

| Flag | Meaning |
|---|---|
| `--model` | The model alias (route) to evaluate. Repeat it to run several models and get a side-by-side summary |
| `--judge-model` | The alias that grades `judge` cases |
| `--no-cache` | Sends `Cache-Control: no-store`, so every answer is fresh and latency is real |
| `--concurrency N` | Requests in flight at once (default 4) |
| `--tag T`, `--limit N` | Run a subset of the cases |
| `--temperature`, `--max-tokens`, `--timeout` | Request settings |
| `--markdown PATH` | Also write a Markdown report to a file, or to a directory with a timestamped name |
| `--min-pass-rate X` | Exit with status 1 if any model's pass rate is below X (from 0 to 1) |
| `--no-db` | Don't store anything; only print the report |

**Rate limits.** A 429 with a short `Retry-After` (a per-minute rate limit) is waited out and retried, up to five times. A 429 with a long `Retry-After` (a daily quota) fails the case instead of waiting until tomorrow. A 401 (a bad key) stops the whole run, which is then stored as `failed`.

**Tracing.** Every request carries `X-Request-ID: eval-r<run>-<case>`, so a case can be found in the gateway's logs.

**Cost.** Cost is computed from the token usage with the same price table the gateway bills with. Cache hits cost $0. Every eval request also creates an ordinary usage row, so `GET /v1/usage` and the dashboard include eval spend.

### In Docker

The `evals` service in Docker Compose runs the CLI on the gateway's network and writes reports to this folder:

```bash
export EVAL_API_KEY=llmgw_...
docker compose run --rm evals run --dataset mock_smoke --model mock-1 --markdown docs/evals/
docker compose run --rm evals compare 1 2
```

## Results

Each run is one row in `eval_runs`: dataset, dataset hash, model, judge, batch, status, settings, and start and end times. Each case is one row in `eval_results`: score, pass, latency, tokens, cost, judge cost, cache status, provider, request ID, the answer, and the reason or error. Migration `004_eval_runs.sql` creates both tables.

`compare` matches cases by id between two runs. It flags **regressed** cases (passed before, fail now) and **worse** cases (same pass or fail, but the score dropped by more than `--tolerance`, default 0.1). It also lists fixed, better, added and removed cases, and warns if the dataset file changed between the runs.

The gateway publishes the latest completed run of each dataset and model on `/metrics` as `gateway_eval_pass_rate`, `gateway_eval_mean_score` and `gateway_eval_last_run_timestamp_seconds`. The "Answer quality (evals)" row on the Grafana dashboard plots them.

### Sample reports

These came from one run of each starter dataset against OpenAI (`gpt-4.1-mini`) and Anthropic (`claude-haiku-5-5`) with `--no-cache`, for a total cost of about $0.003:

- [factual_qa](factual_qa-20261116-220316.md): 11/11 on both. The two open questions were graded by `openai-default` as the judge
- [instruction_following](instruction_following-20261116-220329.md): OpenAI 9/9, Anthropic 8/9 (it wrote a six-word sentence when asked for five)
- [json_output](json_output-20261116-220333.md): 7/7 on both
- [compare-5-6](compare-5-6.md): the instruction-following runs compared, with the one regression flagged
- [mock_smoke](mock_smoke-20261116-220159.md): the Mock provider, as CI runs it

One sample per model isn't a benchmark. Phase 8 covers repeated runs and larger datasets.
