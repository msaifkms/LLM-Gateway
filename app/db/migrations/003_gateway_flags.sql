-- Small on/off switches that operators flip while the gateway is running.
-- No row means off. Today there is one flag, "kill_switch".
CREATE TABLE gateway_flags (
    name        TEXT PRIMARY KEY,
    enabled     BOOLEAN NOT NULL,
    reason      TEXT NOT NULL DEFAULT '',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);