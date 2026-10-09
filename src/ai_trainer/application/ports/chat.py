from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from ai_trainer.domain.chat import ChatMessage, ChatRole


class ChatRepositoryPort(Protocol):
    """Persists an athlete's chat (ADR-0008). Every method is scoped to one `user_id`, which
    comes from the authenticated session (ADR-0005)."""

    async def add(
        self, user_id: UUID, role: ChatRole, text: str, created_at: datetime
    ) -> ChatMessage: ...

    async def list_recent(self, user_id: UUID, limit: int) -> Sequence[ChatMessage]:
        """The user's latest `limit` messages, oldest first."""
        ...
