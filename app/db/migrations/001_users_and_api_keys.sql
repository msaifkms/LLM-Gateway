-- Users own API keys. A NULL limit means "no limit".
CREATE TABLE users (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name                TEXT NOT NULL UNIQUE,
    rate_limit_per_min  INTEGER NOT NULL DEFAULT 60 CHECK (rate_limit_per_min > 0),
    daily_token_limit   BIGINT CHECK (daily_token_limit > 0),
    daily_usd_limit     NUMERIC(12, 6) CHECK (daily_usd_limit > 0),
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Only a hash of each key is stored; the plaintext is shown once at creation.
-- key_prefix (the first few characters) lets an admin tell keys apart.
CREATE TABLE api_keys (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id        BIGINT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    label          TEXT NOT NULL DEFAULT '',
    key_prefix     TEXT NOT NULL,
    key_hash       TEXT NOT NULL UNIQUE,
    cache_enabled  BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at     TIMESTAMPTZ
);

CREATE INDEX api_keys_user_id_idx ON api_keys (user_id);