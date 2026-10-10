from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from ai_trainer.domain.chat import ChatMessage, ChatRole


class ChatRepositoryPort(Protocol):
    """Persists an athlete's chat (ADR-0008). Every method is scoped to one `user_id`, which
    comes from the authenticated session (ADR-0005)."""

    async def add(
        self,
        user_id: UUID,
        role: ChatRole,
        text: str,
        created_at: datetime,
        *,
        sports: Sequence[str] = (),
        template_id: str | None = None,
        template_version: int | None = None,
    ) -> ChatMessage: ...

    async def get(self, user_id: UUID, message_id: UUID) -> ChatMessage | None:
        """The user's message with this ID; `None` if it doesn't exist or isn't theirs."""
        ...

    async def list_recent(self, user_id: UUID, limit: int) -> Sequence[ChatMessage]:
        """The user's latest `limit` messages, oldest first."""
        ...

    async def list_before(
        self, user_id: UUID, before: ChatMessage, limit: int
    ) -> Sequence[ChatMessage]:
        """The user's latest `limit` messages older than `before`, oldest first."""
        ...

    async def list_replies(self, user_id: UUID, to: ChatMessage) -> Sequence[ChatMessage]:
        """The assistant messages after `to`, up to the user's next message, oldest first."""
        ...
