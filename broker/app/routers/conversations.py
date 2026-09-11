"""
Chat history: control plane, one history per user.

This is what makes the web UI multi-tenant. History used to live in the
browser's localStorage, which is per-*browser*, not per-user: two people
signing in to the same browser shared one history and a single user on two
devices had two. Neither is a product.

Every query here is scoped by `user_id` from `require_user` — the same
mechanism that keeps keys and usage separate, and the reason a user cannot
read or delete another's conversations even by guessing an id.

NOTE: this stores prompt and completion text. See the comment in db/init.sql
above `conversations` for what that obliges you to do.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from .. import db
from ..security import require_user

log = logging.getLogger(__name__)

router = APIRouter(prefix="/conversations", tags=["conversations"])

# Bounds. The UI keeps far below these; they exist so one caller cannot fill
# the disk. Content matches the frontend's own per-message cap.
MAX_MESSAGES = 400
MAX_CONTENT = 100_000
MAX_TITLE = 200
MAX_LIST = 100


class Message(BaseModel):
    role: str = Field(pattern="^(system|user|assistant)$")
    content: str = Field(max_length=MAX_CONTENT)


class ConversationBody(BaseModel):
    title: str = Field(default="New chat", max_length=MAX_TITLE)
    messages: list[Message] = Field(default_factory=list, max_length=MAX_MESSAGES)


class ConversationSummary(BaseModel):
    id: str
    title: str
    updated_at: str
    message_count: int


class Conversation(BaseModel):
    id: str
    title: str
    updated_at: str
    messages: list[Message]


def _check_id(conversation_id: str) -> str:
    # Client-generated ids. Keep them short and boring: they end up in a
    # primary key and in URLs.
    if not conversation_id or len(conversation_id) > 64:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "bad conversation id")
    return conversation_id


@router.get("", response_model=list[ConversationSummary])
async def list_conversations(user_id: str = Depends(require_user)):
    """Newest first, for the rail. Does not carry message bodies."""
    async with db.pool().acquire() as conn:
        rows = await conn.fetch(
            """SELECT c.id, c.title, c.updated_at,
                      (SELECT COUNT(*) FROM messages m
                        WHERE m.user_id = c.user_id
                          AND m.conversation_id = c.id) AS message_count
                 FROM conversations c
                WHERE c.user_id = $1
             ORDER BY c.updated_at DESC
                LIMIT $2""",
            user_id, MAX_LIST,
        )
    return [
        ConversationSummary(
            id=r["id"], title=r["title"],
            updated_at=r["updated_at"].isoformat(),
            message_count=r["message_count"],
        )
        for r in rows
    ]


@router.get("/{conversation_id}", response_model=Conversation)
async def get_conversation(
    conversation_id: str, user_id: str = Depends(require_user)
):
    _check_id(conversation_id)
    async with db.pool().acquire() as conn:
        head = await conn.fetchrow(
            """SELECT id, title, updated_at FROM conversations
                WHERE user_id = $1 AND id = $2""",
            user_id, conversation_id,
        )
        # 404 rather than 403 for someone else's conversation: never confirm
        # that an id exists to a user who cannot see it.
        if head is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")
        rows = await conn.fetch(
            """SELECT role, content FROM messages
                WHERE user_id = $1 AND conversation_id = $2
             ORDER BY position""",
            user_id, conversation_id,
        )
    return Conversation(
        id=head["id"], title=head["title"],
        updated_at=head["updated_at"].isoformat(),
        messages=[Message(role=r["role"], content=r["content"]) for r in rows],
    )


@router.put("/{conversation_id}", response_model=ConversationSummary)
async def upsert_conversation(
    conversation_id: str,
    body: ConversationBody,
    user_id: str = Depends(require_user),
):
    """Create or replace. The client sends the whole message array each turn.

    Replacing wholesale rather than appending keeps this idempotent, so a
    retried save cannot duplicate a turn.
    """
    _check_id(conversation_id)
    async with db.pool().acquire() as conn:
        async with conn.transaction():
            # The user row may not exist yet under dev auth.
            # EXTEND: drop this once real provisioning exists — see keys.py.
            await conn.execute(
                "INSERT INTO users (id) VALUES ($1) ON CONFLICT DO NOTHING", user_id
            )
            row = await conn.fetchrow(
                """INSERT INTO conversations (id, user_id, title)
                        VALUES ($1, $2, $3)
                   ON CONFLICT (user_id, id) DO UPDATE
                        SET title = EXCLUDED.title, updated_at = now()
                     RETURNING id, title, updated_at""",
                conversation_id, user_id, body.title,
            )
            await conn.execute(
                """DELETE FROM messages
                    WHERE user_id = $1 AND conversation_id = $2""",
                user_id, conversation_id,
            )
            if body.messages:
                await conn.executemany(
                    """INSERT INTO messages
                           (user_id, conversation_id, position, role, content)
                       VALUES ($1, $2, $3, $4, $5)""",
                    [
                        (user_id, conversation_id, i, m.role, m.content)
                        for i, m in enumerate(body.messages)
                    ],
                )
    return ConversationSummary(
        id=row["id"], title=row["title"],
        updated_at=row["updated_at"].isoformat(),
        message_count=len(body.messages),
    )


@router.delete("/{conversation_id}")
async def delete_conversation(
    conversation_id: str, user_id: str = Depends(require_user)
):
    _check_id(conversation_id)
    async with db.pool().acquire() as conn:
        # The user_id predicate is what stops one user deleting another's.
        result = await conn.execute(
            "DELETE FROM conversations WHERE user_id = $1 AND id = $2",
            user_id, conversation_id,
        )
    if result.endswith(" 0"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")
    return {"status": "deleted", "id": conversation_id}

# EXTEND: retention. Nothing deletes old conversations. Add a scheduled job
# (or a trigger on updated_at) once you have decided the policy, and say what
# it is in the privacy notice.
