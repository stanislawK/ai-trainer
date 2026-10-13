from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from ai_trainer.domain.training import SessionDraft, StoredDraft


class SessionDraftRepositoryPort(Protocol):
    """Persists the athlete's session drafts (ADR-0022), never their saved sessions. Every
    method is scoped to one `user_id` from the authenticated session; a conversation or message
    ID is always looked up with it."""

    async def add(
        self,
        user_id: UUID,
        conversation_id: UUID,
        message_id: UUID,
        sport_id: str,
        draft: SessionDraft,
        *,
        payload_version: int,
        created_at: datetime,
    ) -> StoredDraft:
        """Stores a pending draft made from the user's message `message_id`, in its
        conversation `conversation_id`."""
        ...

    async def list_pending(self, user_id: UUID, conversation_id: UUID) -> Sequence[StoredDraft]:
        """The user's drafts in the conversation that are still waiting for an answer, oldest
        first."""
        ...
