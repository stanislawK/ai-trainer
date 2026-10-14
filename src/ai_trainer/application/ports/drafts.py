from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from ai_trainer.domain.training import SessionDraft, StoredDraft


class SessionDraftRepositoryPort(Protocol):
    """Persists the athlete's session drafts (ADR-0022); their saved sessions are written only
    by a draft's confirm or undo. Every
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
        extends_session_id: UUID | None = None,
    ) -> StoredDraft:
        """Stores a pending draft made from the user's message `message_id`, in its
        conversation `conversation_id`, offered as an addition to the user's session
        `extends_session_id` when given (B16)."""
        ...

    async def list_shown(self, user_id: UUID, conversation_id: UUID) -> Sequence[StoredDraft]:
        """The user's drafts in the conversation, whatever their status, oldest first. A
        discarded draft stays so the chat can show it collapsed."""
        ...

    async def get(self, user_id: UUID, draft_id: UUID) -> StoredDraft:
        """The user's draft `draft_id`, whatever its status. Raises `DraftNotFoundError` when
        the user has no such draft."""
        ...

    async def update(
        self, user_id: UUID, draft_id: UUID, draft: SessionDraft, *, payload_version: int
    ) -> StoredDraft:
        """Replaces the user's pending draft `draft_id`'s document with the athlete's edit
        (F2, ticket #158); it stays `pending`. Raises `DraftNotFoundError` when the user has no
        such draft, and `DraftConfirmedError` or `DraftDiscardedError` when it is no longer
        pending; none of them changes anything."""
        ...

    async def confirm(
        self, user_id: UUID, draft_id: UUID, *, confirmed_at: datetime
    ) -> StoredDraft:
        """In one transaction, saves the user's draft `draft_id` as a training session with a
        source row holding its log's original text, and marks the draft `confirmed` (ADR-0022).
        A draft that extends a session still there, on a day it can belong to, is added to it
        instead: its activities are appended, the envelope fields the session lacks are filled,
        and the source row joins that session (B16); otherwise it no longer extends one.
        A confirmed draft is returned as it is, so confirming twice gives the same session.
        Raises `DraftNotFoundError` when the user has no such draft and `DraftDiscardedError`
        when it was discarded; neither writes anything."""
        ...

    async def undo(self, user_id: UUID, draft_id: UUID, *, undone_at: datetime) -> StoredDraft:
        """In one transaction, takes back the user's confirmed draft `draft_id`'s log from its
        training session (B4): its source row and the activities it brought go, and the session
        goes too when that was its last log (ADR-0022). The draft stays `confirmed`, its
        `session_id` cleared; it is returned as it now is. Raises `DraftNotFoundError` when the
        user has no such draft and `SessionNotFoundError` when it has no session, a pending,
        discarded or undone one; neither changes anything."""
        ...

    async def discard(
        self, user_id: UUID, draft_id: UUID, *, discarded_at: datetime
    ) -> StoredDraft:
        """Marks the user's draft `draft_id` `discarded` and sets its `resolved_at`; no session
        is written (ADR-0022 invariant 3). A discarded draft is returned as it is, so discarding
        twice keeps the first time. Raises `DraftNotFoundError` when the user has no such draft
        and `DraftConfirmedError` when it was confirmed; neither changes anything."""
        ...
