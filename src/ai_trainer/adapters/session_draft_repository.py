from collections.abc import Callable, Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters import session_draft_lifecycle as lifecycle
from ai_trainer.adapters.activity_stores import ActivityStore
from ai_trainer.adapters.orm import ClarificationOrm, SessionDraftOrm
from ai_trainer.adapters.session_draft_lifecycle import (
    CONFIRMED,
    DISCARDED,
    OPEN,
    PENDING,
    to_domain,
    with_span,
)
from ai_trainer.adapters.training_session_repository import spans_of
from ai_trainer.domain.clarification import SAME_SESSION_PURPOSE, Clarification
from ai_trainer.domain.sports.base import ExtractionSpec
from ai_trainer.domain.sports.registry import SportRegistry, default_sport_registry
from ai_trainer.domain.training import (
    DraftConfirmedError,
    DraftDiscardedError,
    DraftNotFoundError,
    SessionDraft,
    StoredDraft,
)


def _ask(
    session: AsyncSession, row: SessionDraftOrm, asks: Clarification | None, at: datetime
) -> None:
    """The same-session question on the draft's card, added in the draft's own transaction
    (ticket #166)."""
    if asks is None:
        return
    session.add(
        ClarificationOrm(
            user_id=row.user_id,
            conversation_id=row.conversation_id,
            message_id=row.message_id,
            draft_id=row.id,
            purpose=SAME_SESSION_PURPOSE,
            clarification=asks.model_dump(mode="json"),
            status=OPEN,
            created_at=at,
        )
    )


class SqlAlchemySessionDraftRepository:
    """Implements `SessionDraftRepositoryPort` (ADR-0004, ADR-0022). The conversation key is
    composite with `user_id`, so a draft can't join another user's conversation, nor extend
    another user's session. A confirm or an undo reaches the session's activities through its
    sport's `ActivityStore` (ADR-0032), so the repository knows the sports."""

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        sports: SportRegistry | None = None,
        *,
        stores: Callable[[str], ActivityStore],
    ) -> None:
        self._session_factory = session_factory
        self._sports = sports or default_sport_registry()
        self._stores = stores

    def _spec(self, sport_id: str) -> ExtractionSpec:
        spec = self._sports.get(sport_id).extraction
        assert spec is not None  # a draft is only ever made by its sport's extraction
        return spec

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
    ) -> StoredDraft:
        async with self._session_factory() as session:
            row = SessionDraftOrm(
                user_id=user_id,
                conversation_id=conversation_id,
                message_id=message_id,
                sport_id=sport_id,
                draft=draft.model_dump(mode="json"),
                payload_version=payload_version,
                status=PENDING,
                extends_session_id=extends_session_id,
                created_at=created_at,
            )
            session.add(row)
            # Flushed, not read after the commit: a committed row's attributes expire.
            await session.flush()
            _ask(session, row, asks, created_at)
            stored = await with_span(session, user_id, row)
            await session.commit()
            return stored

    async def list_shown(self, user_id: UUID, conversation_id: UUID) -> Sequence[StoredDraft]:
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(SessionDraftOrm)
                .where(
                    SessionDraftOrm.user_id == user_id,
                    SessionDraftOrm.conversation_id == conversation_id,
                )
                .order_by(SessionDraftOrm.created_at, SessionDraftOrm.id)
            )
            shown = rows.all()
            extended = {row.extends_session_id for row in shown} - {None}
            spans = await spans_of(session, user_id, [id_ for id_ in extended if id_ is not None])
            return [to_domain(row, spans) for row in shown]

    async def get(self, user_id: UUID, draft_id: UUID) -> StoredDraft:
        async with self._session_factory() as session:
            row = await session.scalar(
                select(SessionDraftOrm).where(
                    SessionDraftOrm.id == draft_id, SessionDraftOrm.user_id == user_id
                )
            )
            if row is None:
                raise DraftNotFoundError(draft_id)
            return await with_span(session, user_id, row)

    async def update(
        self, user_id: UUID, draft_id: UUID, draft: SessionDraft, *, payload_version: int
    ) -> StoredDraft:
        """The row is locked first, as in a confirm, so an edit racing a confirm or a discard
        either goes first, and the confirm saves the edit, or finds the draft resolved and is
        refused."""
        async with self._session_factory() as session:
            row = await session.scalar(
                select(SessionDraftOrm)
                .where(SessionDraftOrm.id == draft_id, SessionDraftOrm.user_id == user_id)
                .with_for_update()
            )
            if row is None:
                raise DraftNotFoundError(draft_id)
            if row.status == CONFIRMED:
                raise DraftConfirmedError(draft_id)
            if row.status == DISCARDED:
                raise DraftDiscardedError(draft_id)
            row.draft = draft.model_dump(mode="json")
            row.payload_version = payload_version
            await session.flush()
            edited = await with_span(session, user_id, row)
            await session.commit()
            return edited

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
    ) -> StoredDraft:
        """Locked first, as an edit is, so a switch racing a confirm or a discard either goes
        first, and the confirm saves the new sport, or finds the draft resolved and is
        refused. The draft's open same-session question is abandoned with it (ticket #166)."""
        async with self._session_factory() as session:
            row = await session.scalar(
                select(SessionDraftOrm)
                .where(SessionDraftOrm.id == draft_id, SessionDraftOrm.user_id == user_id)
                .with_for_update()
            )
            if row is None:
                raise DraftNotFoundError(draft_id)
            if row.status == CONFIRMED:
                raise DraftConfirmedError(draft_id)
            if row.status == DISCARDED:
                raise DraftDiscardedError(draft_id)
            row.sport_id = sport_id
            row.draft = draft.model_dump(mode="json")
            row.payload_version = payload_version
            row.extends_session_id = extends_session_id
            # A same-session question asked about the old sport's session no longer applies.
            await session.execute(
                update(ClarificationOrm)
                .where(
                    ClarificationOrm.user_id == user_id,
                    ClarificationOrm.draft_id == draft_id,
                    ClarificationOrm.status == OPEN,
                )
                .values(status="abandoned", answered_at=None)
            )
            await session.flush()
            _ask(session, row, asks, asked_at or row.created_at)
            changed = await with_span(session, user_id, row)
            await session.commit()
            return changed

    async def confirm(
        self, user_id: UUID, draft_id: UUID, *, confirmed_at: datetime
    ) -> StoredDraft:
        async with self._session_factory() as session:
            return await lifecycle.confirm(
                session,
                user_id,
                draft_id,
                confirmed_at=confirmed_at,
                spec_of=self._spec,
                store_of=self._stores,
            )

    async def settle_session(
        self,
        user_id: UUID,
        draft_id: UUID,
        clarification_id: UUID,
        option_id: str,
        *,
        extends_session_id: UUID | None,
        answered_at: datetime,
    ) -> StoredDraft:
        async with self._session_factory() as session:
            return await lifecycle.settle_session(
                session,
                user_id,
                draft_id,
                clarification_id,
                option_id,
                extends_session_id=extends_session_id,
                answered_at=answered_at,
            )

    async def discard(
        self, user_id: UUID, draft_id: UUID, *, discarded_at: datetime
    ) -> StoredDraft:
        async with self._session_factory() as session:
            return await lifecycle.discard(session, user_id, draft_id, discarded_at=discarded_at)

    async def undo(self, user_id: UUID, draft_id: UUID, *, undone_at: datetime) -> StoredDraft:
        async with self._session_factory() as session:
            return await lifecycle.undo(
                session,
                user_id,
                draft_id,
                undone_at=undone_at,
                spec_of=self._spec,
                store_of=self._stores,
            )
