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
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Backstop for keys nobody revokes. The frontend mints one per browser
    -- session and revokes it at sign-out; if it restarts while sessions are
    -- live that never happens, and without this the key is valid forever.
    expires_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys (user_id);

CREATE INDEX IF NOT EXISTS idx_api_keys_expiring
    ON api_keys (expires_at)
    WHERE expires_at IS NOT NULL AND NOT revoked;

-- EXTEND: last_used_at TIMESTAMPTZ, rate_limit_per_min INT,
--         monthly_token_quota BIGINT.

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

-- Conversations: the chat history behind the web UI.
--
-- THIS STORES PROMPT AND COMPLETION TEXT. That is a deliberate change from the
-- original design, which stored none and said so. It was made so a user's
-- history follows their account rather than their browser — without it, two
-- people signing in on one browser share a single history, which is not a
-- multi-tenant product.
--
-- It obliges you to: a retention policy (there is none yet), a line in the
-- privacy notice, and access control on backups, which now carry user content.
-- The inference path itself still logs nothing: vLLM runs without request
-- logging and the broker never writes prompt text outside these two tables.
CREATE TABLE IF NOT EXISTS conversations (
    -- Client-generated id. The composite primary key with user_id makes a
    -- cross-tenant collision impossible: user A writing user B's conversation
    -- id touches A's own row, never B's.
    id         TEXT        NOT NULL,
    user_id    TEXT        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    title      TEXT        NOT NULL DEFAULT 'New chat',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, id)
);

CREATE INDEX IF NOT EXISTS idx_conversations_user_updated
    ON conversations (user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id              BIGSERIAL PRIMARY KEY,
    user_id         TEXT     NOT NULL,
    conversation_id TEXT     NOT NULL,
    position        INT      NOT NULL,     -- 0-based order within the chat
    role            TEXT     NOT NULL CHECK (role IN ('system', 'user', 'assistant')),
    content         TEXT     NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (user_id, conversation_id)
        REFERENCES conversations (user_id, id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation
    ON messages (user_id, conversation_id, position);

-- EXTEND: a retention policy. Something has to delete old conversations, and
-- the choice (90 days? user-triggered only?) belongs in your privacy notice
-- before real customers exist.

-- Seed the PoC user so `POST /keys` works out of the box with dev auth.
INSERT INTO users (id, email) VALUES ('demo-user', 'demo@example.invalid')
ON CONFLICT DO NOTHING;
