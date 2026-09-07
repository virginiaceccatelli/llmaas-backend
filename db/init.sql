-- Schema for the Gateway/Broker VM's Postgres.
--
-- Applied automatically by the postgres image on FIRST start only (the
-- docker-entrypoint-initdb.d convention). If you change this file after the
-- volume exists you must either `docker compose down -v` or move to real
-- migrations. See the note in broker/app/db.py.

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- Users. Deliberately thin: identity itself lives in your IdP (or the
-- frontend BFF's session store), this table only anchors keys and usage.
CREATE TABLE IF NOT EXISTS users (
    id         TEXT PRIMARY KEY,               -- IdP `sub` claim, or an internal id
    email      TEXT UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- EXTEND when you add auth: password_hash (if self-hosting login), or
-- idp_issuer/idp_subject columns; plus an `org_id` if you need teams sharing
-- keys and a single bill.

-- API keys. Only the hash is stored — a database dump yields no usable keys.
CREATE TABLE IF NOT EXISTS api_keys (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    TEXT        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    label      TEXT        NOT NULL DEFAULT 'default',  -- user-facing name
    key_prefix TEXT        NOT NULL,                    -- e.g. wiit_Ab12cd3, shown in the UI
    key_hash   TEXT        NOT NULL UNIQUE,             -- SHA-256 of the full key
    revoked    BOOLEAN     NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys (user_id);

-- EXTEND: expires_at TIMESTAMPTZ, last_used_at TIMESTAMPTZ,
--         rate_limit_per_min INT, monthly_token_quota BIGINT.

-- Usage records: one row per request, aggregated later for billing.
CREATE TABLE IF NOT EXISTS usage (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    key_id            UUID        NOT NULL REFERENCES api_keys (id) ON DELETE CASCADE,
    model             TEXT        NOT NULL,   -- our PUBLIC model name
    prompt_tokens     INT         NOT NULL DEFAULT 0,
    completion_tokens INT         NOT NULL DEFAULT 0,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_usage_key_time ON usage (key_id, created_at DESC);

-- NOTE: no prompt or completion text is stored anywhere, by design. If you
-- ever add request logging for debugging, it needs its own retention policy,
-- its own access controls, and a line in your privacy notice.

-- Seed the PoC user so `POST /keys` works out of the box with dev auth.
INSERT INTO users (id, email) VALUES ('demo-user', 'demo@example.invalid')
ON CONFLICT DO NOTHING;
