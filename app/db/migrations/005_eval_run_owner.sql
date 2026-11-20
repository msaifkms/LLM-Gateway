-- Who ran an eval: the owner of the API key the run sent its requests with, so
-- a user can see their own runs (GET /v1/evals/runs) the way they see their own
-- usage. NULL for runs made before this column existed, or whose user was
-- deleted; those stay visible to the admin CLI only.
ALTER TABLE eval_runs
    ADD COLUMN user_id BIGINT REFERENCES users (id) ON DELETE SET NULL;

CREATE INDEX eval_runs_user_idx ON eval_runs (user_id, id DESC);
