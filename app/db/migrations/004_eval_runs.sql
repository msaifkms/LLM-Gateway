-- One row per eval run: a dataset sent through one model alias (route).
-- Runs started together for several models share a batch_id.
CREATE TABLE eval_runs (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dataset         TEXT NOT NULL,
    -- Hash of the dataset file, so a comparison can tell the cases changed.
    dataset_sha256  TEXT NOT NULL,
    model           TEXT NOT NULL,
    judge_model     TEXT,
    batch_id        TEXT,
    status          TEXT NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running', 'completed', 'failed')),
    -- Concurrency, temperature, cache bypass, filters. Never the API key.
    settings        JSONB NOT NULL DEFAULT '{}'::jsonb,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    CHECK (finished_at IS NULL OR finished_at >= started_at),
    CHECK ((status = 'running') = (finished_at IS NULL))
);

CREATE INDEX eval_runs_dataset_model_idx
    ON eval_runs (dataset, model, finished_at DESC);

-- One row per case in a run. The answer is kept so a regression can be read.
CREATE TABLE eval_results (
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id             BIGINT NOT NULL REFERENCES eval_runs (id) ON DELETE CASCADE,
    case_id            TEXT NOT NULL,
    scorer             TEXT NOT NULL,
    tags               TEXT[] NOT NULL DEFAULT '{}',
    score              DOUBLE PRECISION NOT NULL CHECK (score >= 0 AND score <= 1),
    passed             BOOLEAN NOT NULL,
    -- NULL when the gateway never answered.
    latency_ms         DOUBLE PRECISION CHECK (latency_ms >= 0),
    prompt_tokens      INTEGER NOT NULL DEFAULT 0 CHECK (prompt_tokens >= 0),
    completion_tokens  INTEGER NOT NULL DEFAULT 0 CHECK (completion_tokens >= 0),
    cost_usd           NUMERIC(14, 8) NOT NULL DEFAULT 0 CHECK (cost_usd >= 0),
    judge_tokens       INTEGER NOT NULL DEFAULT 0 CHECK (judge_tokens >= 0),
    judge_cost_usd     NUMERIC(14, 8) NOT NULL DEFAULT 0 CHECK (judge_cost_usd >= 0),
    cache_status       TEXT,
    provider           TEXT,
    response_model     TEXT,
    request_id         TEXT,
    output             TEXT,
    reason             TEXT NOT NULL DEFAULT '',
    error              TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, case_id),
    -- A failed request cannot pass.
    CHECK (error IS NULL OR NOT passed)
);
