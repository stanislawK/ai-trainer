from collections.abc import Callable, Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.orm import (
    ChatMessageOrm,
    SessionDraftOrm,
    TrainingSessionOrm,
    TrainingSessionSourceOrm,
)
from ai_trainer.domain.training import (
    DraftConfirmedError,
    DraftDiscardedError,
    DraftNotFoundError,
    DraftStatus,
    SessionDraft,
    SessionNotFoundError,
    StoredDraft,
)

_PENDING: DraftStatus = "pending"
_CONFIRMED: DraftStatus = "confirmed"
_DISCARDED: DraftStatus = "discarded"
_CHAT_SOURCE = "chat"


def _to_domain(row: SessionDraftOrm) -> StoredDraft:
    return StoredDraft.model_validate(
        {
            "id": row.id,
            "conversation_id": row.conversation_id,
            "message_id": row.message_id,
            "sport_id": row.sport_id,
            "draft": SessionDraft.model_validate(row.draft),
            "payload_version": row.payload_version,
            "status": row.status,
            "session_id": row.session_id,
            "created_at": row.created_at,
        }
    )


class SqlAlchemySessionDraftRepository:
    """Implements `SessionDraftRepositoryPort` (ADR-0004, ADR-0022). The conversation key is
    composite with `user_id`, so a draft can't join another user's conversation."""

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

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
        async with self._session_factory() as session:
            row = SessionDraftOrm(
                user_id=user_id,
                conversation_id=conversation_id,
                message_id=message_id,
                sport_id=sport_id,
                draft=draft.model_dump(mode="json"),
                payload_version=payload_version,
                status=_PENDING,
                created_at=created_at,
            )
            session.add(row)
            # Flushed, not read after the commit: a committed row's attributes expire.
            await session.flush()
            stored = _to_domain(row)
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
            return [_to_domain(row) for row in rows.all()]

    async def get(self, user_id: UUID, draft_id: UUID) -> StoredDraft:
        async with self._session_factory() as session:
            row = await session.scalar(
                select(SessionDraftOrm).where(
                    SessionDraftOrm.id == draft_id, SessionDraftOrm.user_id == user_id
                )
            )
            if row is None:
                raise DraftNotFoundError(draft_id)
            return _to_domain(row)

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
            if row.status == _CONFIRMED:
                raise DraftConfirmedError(draft_id)
            if row.status == _DISCARDED:
                raise DraftDiscardedError(draft_id)
            row.draft = draft.model_dump(mode="json")
            row.payload_version = payload_version
            await session.flush()
            edited = _to_domain(row)
            await session.commit()
            return edited

    async def confirm(
        self, user_id: UUID, draft_id: UUID, *, confirmed_at: datetime
    ) -> StoredDraft:
        """One transaction (ADR-0022): the session, its source row with the log's original text
        (B2), and the draft's move to `confirmed`. The draft row is locked first, so a second
        confirm waits for the first and then finds it done: one session either way."""
        async with self._session_factory() as session:
            row = await session.scalar(
                select(SessionDraftOrm)
                .where(SessionDraftOrm.id == draft_id, SessionDraftOrm.user_id == user_id)
                .with_for_update()
            )
            if row is None:
                raise DraftNotFoundError(draft_id)
            if row.status == _DISCARDED:
                raise DraftDiscardedError(draft_id)
            if row.status == _CONFIRMED:
                return _to_domain(row)
            log = (
                await session.execute(
                    select(ChatMessageOrm.text, ChatMessageOrm.created_at).where(
                        ChatMessageOrm.id == row.message_id, ChatMessageOrm.user_id == user_id
                    )
                )
            ).one()
            draft = SessionDraft.model_validate(row.draft)
            training = TrainingSessionOrm(
                user_id=user_id,
                sport_id=row.sport_id,
                occurred_on=draft.occurred_on,
                timezone=draft.timezone,
                started_at=draft.started_at,
                duration_min=draft.duration_min,
                rpe=draft.rpe,
                feeling=draft.feeling,
                notes=draft.notes,
                payload=draft.payload,
                payload_version=row.payload_version,
                created_at=confirmed_at,
                updated_at=confirmed_at,
            )
            session.add(training)
            await session.flush()
            session.add(
                TrainingSessionSourceOrm(
                    user_id=user_id,
                    session_id=training.id,
                    raw_text=log.text,
                    source=_CHAT_SOURCE,
                    logged_at=log.created_at,
                )
            )
            row.status = _CONFIRMED
            row.session_id = training.id
            row.resolved_at = confirmed_at
            await session.flush()
            confirmed = _to_domain(row)
            await session.commit()
            return confirmed

    async def discard(
        self, user_id: UUID, draft_id: UUID, *, discarded_at: datetime
    ) -> StoredDraft:
        """Marks the draft `discarded`; it writes no session (ADR-0022 invariant 3). The row is
        locked first, so a discard racing a confirm either finds it confirmed and is refused,
        or goes first and makes the confirm refuse."""
        async with self._session_factory() as session:
            row = await session.scalar(
                select(SessionDraftOrm)
                .where(SessionDraftOrm.id == draft_id, SessionDraftOrm.user_id == user_id)
                .with_for_update()
            )
            if row is None:
                raise DraftNotFoundError(draft_id)
            if row.status == _CONFIRMED:
                raise DraftConfirmedError(draft_id)
            if row.status == _PENDING:
                row.status = _DISCARDED
                row.resolved_at = discarded_at
            await session.flush()
            discarded = _to_domain(row)
            await session.commit()
            return discarded

    async def undo(self, user_id: UUID, session_id: UUID) -> StoredDraft:
        """Deletes the session; its source rows go with it (`ON DELETE CASCADE`) and the
        draft's `session_id` is cleared by its own key (`SET NULL (session_id)`), so the draft
        stays `confirmed` (ADR-0022). The draft row is locked first, as in a confirm, so a
        second undo waits for the first and then finds no session."""
        async with self._session_factory() as session:
            row = await session.scalar(
                select(SessionDraftOrm)
                .where(SessionDraftOrm.session_id == session_id, SessionDraftOrm.user_id == user_id)
                .with_for_update()
            )
            if row is None:
                raise SessionNotFoundError(session_id)
            await session.execute(
                delete(TrainingSessionOrm).where(
                    TrainingSessionOrm.id == session_id, TrainingSessionOrm.user_id == user_id
                )
            )
            await session.refresh(row)
            undone = _to_domain(row)
            await session.commit()
            return undone
