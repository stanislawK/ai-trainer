from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from ai_trainer.domain.clarification import Clarification, ClarificationStatus, StoredClarification


class ClarificationRepositoryPort(Protocol):
    """Persists the athlete's open questions (ADR-0015, ADR-0022). Every method is scoped to
    one `user_id` from the authenticated session; a conversation, message or clarification ID is
    always looked up with it."""

    async def add(
        self,
        user_id: UUID,
        conversation_id: UUID,
        message_id: UUID,
        purpose: str,
        clarification: Clarification,
        *,
        created_at: datetime,
    ) -> StoredClarification:
        """Stores an `open` question blocking the user's message `message_id`."""
        ...

    async def list_shown(
        self, user_id: UUID, conversation_id: UUID
    ) -> Sequence[StoredClarification]:
        """The user's questions in the conversation, whatever their status, oldest first."""
        ...

    async def get(self, user_id: UUID, clarification_id: UUID) -> StoredClarification:
        """Raises `ClarificationNotFoundError` when the user has no such question."""
        ...

    async def resolve(
        self,
        user_id: UUID,
        clarification_id: UUID,
        status: ClarificationStatus,
        *,
        option_ids: Sequence[str] | None,
        resolved_at: datetime,
    ) -> StoredClarification:
        """Moves the user's `open` question to `answered` or `abandoned`, once. Raises
        `ClarificationNotFoundError` when the user has no such question and
        `ClarificationResolvedError` when it is not open; neither changes anything."""
        ...

    async def reopen(self, user_id: UUID, clarification_id: UUID) -> None:
        """Puts the user's `answered` question back to `open` with no answer, for an answer whose
        resumed reply could not be saved. A question that is not `answered` is left as it is."""
        ...
