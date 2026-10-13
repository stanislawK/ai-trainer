from collections.abc import Callable, Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.orm import SessionDraftOrm
from ai_trainer.domain.training import DraftStatus, SessionDraft, StoredDraft

_PENDING: DraftStatus = "pending"


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

    async def list_pending(self, user_id: UUID, conversation_id: UUID) -> Sequence[StoredDraft]:
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(SessionDraftOrm)
                .where(
                    SessionDraftOrm.user_id == user_id,
                    SessionDraftOrm.conversation_id == conversation_id,
                    SessionDraftOrm.status == _PENDING,
                )
                .order_by(SessionDraftOrm.created_at, SessionDraftOrm.id)
            )
            return [_to_domain(row) for row in rows.all()]
