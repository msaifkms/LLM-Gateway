-- One row per answered chat request. Rows are never updated: they are the
-- billing record, so cost is stored as computed at the time (prices change).
CREATE TABLE usage_events (
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id            BIGINT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    api_key_id         BIGINT NOT NULL REFERENCES api_keys (id) ON DELETE CASCADE,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    route              TEXT NOT NULL,
    provider           TEXT NOT NULL,
    model              TEXT NOT NULL,
    cache_status       TEXT NOT NULL
                       CHECK (cache_status IN ('MISS', 'BYPASS', 'HIT', 'COALESCED')),
    prompt_tokens      INTEGER NOT NULL CHECK (prompt_tokens >= 0),
    completion_tokens  INTEGER NOT NULL CHECK (completion_tokens >= 0),
    -- What this request actually cost us: zero for HIT and COALESCED.
    cost_usd           NUMERIC(14, 8) NOT NULL CHECK (cost_usd >= 0),
    -- What it would have cost uncached, so savings can be reported.
    list_cost_usd      NUMERIC(14, 8) NOT NULL CHECK (list_cost_usd >= 0),
    -- False when the model had no price on file (cost is then recorded as 0).
    priced             BOOLEAN NOT NULL
);

CREATE INDEX usage_events_user_created_idx ON usage_events (user_id, created_at DESC);