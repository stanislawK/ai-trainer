from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict

# A conversation's title is its first athlete message, cut to this many characters (ADR-0021).
TITLE_LENGTH = 60


class ChatRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class ChatMessage(BaseModel):
    """One turn of an athlete's chat, in one of their conversations (PRD-0004 F1, B24, ADR-0008,
    ADR-0021). `created_at` is a UTC moment; the athlete's local time is derived from
    `User.timezone` at the edge (ADR-0014). A reply records the sports it is about (F15) and the
    template ID and version that produced it; an athlete's message has none."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    conversation_id: UUID
    role: ChatRole
    text: str
    created_at: datetime
    sports: tuple[str, ...] = ()
    template_id: str | None = None
    template_version: int | None = None


class Conversation(BaseModel):
    """One thread of an athlete's chat (PRD-0004 B24, ADR-0021). Prompt history never crosses
    its boundary. `last_message_at` is a UTC moment that moves forward with every message and
    reply part; `title` is display text, never instructions."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    title: str | None
    created_at: datetime
    last_message_at: datetime


def conversation_title(text: str) -> str:
    """The title for a conversation that starts with `text`: whitespace collapsed to single
    spaces and cut to `TITLE_LENGTH` characters, with no model call (ADR-0021). The backfill in
    migration `b6f1c2d9e4a7` computes the same in SQL."""
    return " ".join(text.split())[:TITLE_LENGTH].rstrip()
