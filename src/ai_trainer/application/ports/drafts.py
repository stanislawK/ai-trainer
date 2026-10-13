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

    async def list_shown(self, user_id: UUID, conversation_id: UUID) -> Sequence[StoredDraft]:
        """The user's drafts in the conversation that the chat shows, pending or confirmed,
        oldest first. A discarded draft is left out."""
        ...

    async def confirm(
        self, user_id: UUID, draft_id: UUID, *, confirmed_at: datetime
    ) -> StoredDraft:
        """In one transaction, saves the user's draft `draft_id` as a training session with a
        source row holding its log's original text, and marks the draft `confirmed` (ADR-0022).
        A confirmed draft is returned as it is, so confirming twice gives the same session.
        Raises `DraftNotFoundError` when the user has no such draft and `DraftDiscardedError`
        when it was discarded; neither writes anything."""
        ...
