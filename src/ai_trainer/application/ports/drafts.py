from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from pydantic import JsonValue

from ai_trainer.domain.clarification import Clarification
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
        asks: Clarification | None = None,
        template_id: str | None = None,
        template_version: int | None = None,
    ) -> StoredDraft:
        """Stores a pending draft made from the user's message `message_id`, in its
        conversation `conversation_id`, offered as an addition to the user's session
        `extends_session_id` when given (B16). `asks` is the same-session question on its card,
        stored `open` in the same transaction, so the draft never exists without it (ticket
        #166). `template_id` and `template_version` name the extraction template it came from
        (ADR-0034)."""
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

    async def change_sport(
        self,
        user_id: UUID,
        draft_id: UUID,
        sport_id: str,
        draft: SessionDraft,
        *,
        payload_version: int,
        extends_session_id: UUID | None,
        asks: Clarification | None = None,
        asked_at: datetime | None = None,
        template_id: str | None = None,
        template_version: int | None = None,
    ) -> StoredDraft:
        """Replaces the user's pending draft `draft_id` with one in `sport_id`, extracted again
        from the same log (B23, ticket #163): its sport, document, payload version and the
        session it extends (B16) all change, and it keeps its ID and stays `pending`. A
        same-session question still open on it no longer applies, so it is `abandoned` in the
        same transaction, and `asks`, the new sport's own when given, is stored `open` in it too,
        asked at `asked_at` (ticket #166). Raises
        `DraftNotFoundError` when the user has no such draft, and `DraftConfirmedError` or
        `DraftDiscardedError` when it is no longer pending; none of them changes anything."""
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
        Raises `DraftNotFoundError` when the user has no such draft, `DraftDiscardedError`
        when it was discarded, and `DraftAwaitingAnswerError` while a question on its card is
        still open (ticket #166); none of them writes anything."""
        ...

    async def settle_session(
        self,
        user_id: UUID,
        draft_id: UUID,
        clarification_id: UUID,
        option_id: str,
        *,
        extends_session_id: UUID | None,
        answered_at: datetime,
        asks: Clarification | None = None,
    ) -> StoredDraft:
        """In one transaction, answers the user's open same-session question
        `clarification_id` on their pending draft `draft_id` with `option_id`, and sets the
        session the draft extends to `extends_session_id`, or clears it with `None` (B16,
        ticket #166). A session that is gone, or no longer in the draft's sport, is not
        extended, and a confirm then saves a session of its own. `asks` is the card's next
        question, stored `open` in the same transaction (ticket #397). The draft row is locked
        first, as in a confirm, so a confirm racing the answer waits for it. Raises
        `DraftNotFoundError` when the user has no such draft, `DraftConfirmedError` or
        `DraftDiscardedError` when it is no longer pending, and `ClarificationResolvedError`
        when the question is not open on it; none of them changes anything."""
        ...

    async def settle_name(
        self,
        user_id: UUID,
        draft_id: UUID,
        clarification_id: UUID,
        *,
        option_id: str | None,
        settle: Callable[[Mapping[str, JsonValue]], dict[str, JsonValue]],
        asks: Clarification | None,
        answered_at: datetime,
        typed_name: str | None = None,
    ) -> StoredDraft:
        """In one transaction, closes the user's open exercise question `clarification_id` on
        their pending draft `draft_id`, as answered with `option_id` or abandoned with `None`,
        replaces the draft's payload with `settle` of it, read under the draft row's lock so an
        edit racing the tap isn't lost, and stores `asks`, the card's next question, `open`
        (ticket #397). `typed_name` is kept on the closed question for its row (ticket #402). No
        alias or exercise is written before the confirm. Raises as `settle_session` does; none of
        them changes anything."""
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
