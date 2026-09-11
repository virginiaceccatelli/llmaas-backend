-- 001: server-side conversations, and API-key expiry.
--
-- Apply to an EXISTING database (db/init.sql only runs on a fresh one):
--
--   .\scripts\local_postgres.ps1 migrate
--
-- or by hand:
--   psql -h 127.0.0.1 -U llmaas -d llmaas -v ON_ERROR_STOP=1 \
--        -f db/migrations/001_conversations_and_key_expiry.sql
--
-- Idempotent: safe to run twice.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. API-key expiry
-- ---------------------------------------------------------------------------
-- The frontend mints one key per browser session and revokes it at sign-out.
-- If the BFF restarts, crashes, or is redeployed while sessions are live, that
-- revocation never happens and the key stays valid forever with nothing
-- tracking it. An expiry is the backstop: a key nobody revokes still dies.
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;

-- Partial index: the reaper only ever looks at unrevoked keys that have an
-- expiry, which is a small slice of the table.
CREATE INDEX IF NOT EXISTS idx_api_keys_expiring
    ON api_keys (expires_at)
    WHERE expires_at IS NOT NULL AND NOT revoked;

-- ---------------------------------------------------------------------------
-- 2. Conversations
-- ---------------------------------------------------------------------------
-- NOTE, deliberately: this stores PROMPT AND COMPLETION TEXT. Until now the
-- platform stored none, anywhere, and said so. That changed so a user's chat
-- history follows their account instead of their browser — without it, two
-- people signing in to the same browser share one history, which is not a
-- multi-tenant product.
--
-- What that obliges you to do, and what is NOT done by this file:
--   * a retention policy (nothing deletes old conversations yet);
--   * a line in the privacy notice saying conversations are stored;
--   * access control on database backups, which now contain user content.
-- See architecture.md, "Data we store".

CREATE TABLE IF NOT EXISTS conversations (
    -- Client-generated id. The composite primary key with user_id is what
    -- makes a collision across tenants impossible: user A choosing user B's
    -- conversation id writes to A's own row, never B's.
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
    -- Position in the conversation, 0-based. The client sends the whole array
    -- on every save, so this is rewritten wholesale rather than appended.
    position        INT      NOT NULL,
    role            TEXT     NOT NULL CHECK (role IN ('system', 'user', 'assistant')),
    content         TEXT     NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (user_id, conversation_id)
        REFERENCES conversations (user_id, id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation
    ON messages (user_id, conversation_id, position);

COMMIT;
