from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ChatRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class ChatMessage(BaseModel):
    """One turn of an athlete's chat (PRD-0003 F1, ADR-0008). `created_at` is a UTC moment;
    the athlete's local time is derived from `User.timezone` at the edge (ADR-0014). A reply
    records the sports it is about (F15) and the template ID and version that produced it;
    an athlete's message has none."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    role: ChatRole
    text: str
    created_at: datetime
    sports: tuple[str, ...] = ()
    template_id: str | None = None
    template_version: int | None = None
