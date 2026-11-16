# Eval comparison: run 5 vs run 6

## Compare: run 5 (openai-default) vs run 6 (anthropic-default)

| Metric | run 5 (openai-default) | run 6 (anthropic-default) |
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

### Regressions (1)

| Case | Change | Before | After | Why now |
|---|---|---|---|---|
| five-word-sentence | REGRESSED | PASS 1.00 | FAIL 0.00 | no match |

### Other cases

| Case | Change | Before | After |
|---|---|---|---|
| reply-yes-only | same | PASS 1.00 | PASS 1.00 |
| count-to-five | same | PASS 1.00 | PASS 1.00 |
| translate-french | same | PASS 1.00 | PASS 1.00 |
| primary-colors-csv | same | PASS 1.00 | PASS 1.00 |
| all-caps-sentence | same | PASS 1.00 | PASS 1.00 |
| three-bullets | same | PASS 1.00 | PASS 1.00 |
| starts-with-certainly | same | PASS 1.00 | PASS 1.00 |
| pirate-persona | same | PASS 1.00 | PASS 1.00 |
