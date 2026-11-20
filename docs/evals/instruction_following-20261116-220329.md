# Eval report: instruction_following

## Side by side: instruction_following

| Metric | openai-default | anthropic-default |
|---|---|---|
| Cases | 9 | 9 |
| Passed | 9 | 8 |
| Pass rate | 100.0% | 88.9% |
| Mean score | 1.000 | 0.889 |
| Errors | 0 | 0 |
| Latency p50 | 636 ms | 623 ms |
| Latency p95 | 1,806 ms | 1,698 ms |
| Tokens (prompt + completion) | 221 + 190 | 309 + 449 |
| Judge tokens | 0 | 0 |
| Cost (answers) | $0.000392 | $0.000255 |
| Cost (judge) | $0.000000 | $0.000000 |
| Cost (total) | $0.000392 | $0.000255 |

### Cases

| Case | openai-default | anthropic-default |
|---|---|---|
| reply-yes-only | PASS 1.00 | PASS 1.00 |
| count-to-five | PASS 1.00 | PASS 1.00 |
| translate-french | PASS 1.00 | PASS 1.00 |
| primary-colors-csv | PASS 1.00 | PASS 1.00 |
| all-caps-sentence | PASS 1.00 | PASS 1.00 |
| five-word-sentence | PASS 1.00 | FAIL 0.00 |
| three-bullets | PASS 1.00 | PASS 1.00 |
| starts-with-certainly | PASS 1.00 | PASS 1.00 |
| pirate-persona | PASS 1.00 | PASS 1.00 |

## Eval: instruction_following on openai-default

Dataset: instruction_following · Model: openai-default · Run: 5 · Status: completed · Started: 2026-11-16 22:03 UTC · Cache: bypassed (no-store)

| Metric | Value |
|---|---|
| Cases | 9 |
| Passed | 9 |
| Pass rate | 100.0% |
| Mean score | 1.000 |
| Errors | 0 |
| Latency p50 | 636 ms |
| Latency p95 | 1,806 ms |
| Tokens (prompt + completion) | 221 + 190 |
| Judge tokens | 0 |
| Cost (answers) | $0.000392 |
| Cost (judge) | $0.000000 |
| Cost (total) | $0.000392 |

### Cases

| Case | Scorer | Result | Score | Latency | Tokens | Cache | Notes |
|---|---|---|---|---|---|---|---|
| reply-yes-only | exact | PASS | 1.00 | 869 ms | 21 | BYPASS |  |
| count-to-five | exact | PASS | 1.00 | 636 ms | 34 | BYPASS |  |
| translate-french | normalized | PASS | 1.00 | 517 ms | 22 | BYPASS |  |
| primary-colors-csv | regex | PASS | 1.00 | 560 ms | 31 | BYPASS |  |
| all-caps-sentence | regex | PASS | 1.00 | 545 ms | 33 | BYPASS |  |
| five-word-sentence | regex | PASS | 1.00 | 539 ms | 29 | BYPASS |  |
| three-bullets | regex | PASS | 1.00 | 1,806 ms | 98 | BYPASS |  |
| starts-with-certainly | regex | PASS | 1.00 | 1,161 ms | 56 | BYPASS |  |
| pirate-persona | contains | PASS | 1.00 | 1,025 ms | 87 | BYPASS |  |

## Eval: instruction_following on anthropic-default

Dataset: instruction_following · Model: anthropic-default · Run: 6 · Status: completed · Started: 2026-11-16 22:03 UTC · Cache: bypassed (no-store)

| Metric | Value |
|---|---|
| Cases | 9 |
| Passed | 8 |
| Pass rate | 88.9% |
| Mean score | 0.889 |
| Errors | 0 |
| Latency p50 | 623 ms |
| Latency p95 | 1,698 ms |
| Tokens (prompt + completion) | 309 + 449 |
| Judge tokens | 0 |
| Cost (answers) | $0.000255 |
| Cost (judge) | $0.000000 |
| Cost (total) | $0.000255 |

### Cases

| Case | Scorer | Result | Score | Latency | Tokens | Cache | Notes |
|---|---|---|---|---|---|---|---|
| reply-yes-only | exact | PASS | 1.00 | 618 ms | 32 | BYPASS |  |
| count-to-five | exact | PASS | 1.00 | 622 ms | 47 | BYPASS |  |
| translate-french | normalized | PASS | 1.00 | 620 ms | 36 | BYPASS |  |
| primary-colors-csv | regex | PASS | 1.00 | 623 ms | 45 | BYPASS |  |
| all-caps-sentence | regex | PASS | 1.00 | 704 ms | 88 | BYPASS |  |
| five-word-sentence | regex | FAIL | 0.00 | 621 ms | 51 | BYPASS | no match |
| three-bullets | regex | PASS | 1.00 | 993 ms | 159 | BYPASS |  |
| starts-with-certainly | regex | PASS | 1.00 | 643 ms | 86 | BYPASS |  |
| pirate-persona | contains | PASS | 1.00 | 1,698 ms | 214 | BYPASS |  |
