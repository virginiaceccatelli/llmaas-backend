CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- API keys: we store only the hash, never the plaintext.
CREATE TABLE IF NOT EXISTS api_keys (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    TEXT        NOT NULL,
    key_prefix TEXT        NOT NULL,               -- shown in UI for identification
    key_hash   TEXT        NOT NULL UNIQUE,        -- SHA-256 of the full key
    revoked    BOOLEAN     NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys (key_hash);
CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys (user_id);

-- Usage records: one row per request, aggregated later for billing.
CREATE TABLE IF NOT EXISTS usage (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    key_id            UUID NOT NULL REFERENCES api_keys (id),
    model             TEXT NOT NULL,
    prompt_tokens     INT  NOT NULL DEFAULT 0,
    completion_tokens INT  NOT NULL DEFAULT 0,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_usage_key ON usage (key_id);