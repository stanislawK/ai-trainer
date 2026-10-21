from collections.abc import Callable, Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.orm import ClarificationOrm
from ai_trainer.domain.clarification import (
    Clarification,
    ClarificationNotFoundError,
    ClarificationResolvedError,
    ClarificationStatus,
    StoredClarification,
)

_OPEN: ClarificationStatus = "open"


def _to_domain(row: ClarificationOrm) -> StoredClarification:
    return StoredClarification.model_validate(
        {
            "id": row.id,
            "conversation_id": row.conversation_id,
            "message_id": row.message_id,
            "purpose": row.purpose,
            "clarification": Clarification.model_validate(row.clarification),
            "status": row.status,
            "answer_option_ids": row.answer_option_ids,
            "created_at": row.created_at,
        }
    )


class SqlAlchemyClarificationRepository:
    """Implements `ClarificationRepositoryPort` (ADR-0004, ADR-0015, ADR-0022). The conversation
    and message keys are composite with `user_id`, so a question can't block another user's
    message."""

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

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
        async with self._session_factory() as session:
            row = ClarificationOrm(
                user_id=user_id,
                conversation_id=conversation_id,
                message_id=message_id,
                purpose=purpose,
                clarification=clarification.model_dump(mode="json"),
                status=_OPEN,
                created_at=created_at,
            )
            session.add(row)
            # Flushed, not read after the commit: a committed row's attributes expire.
            await session.flush()
            stored = _to_domain(row)
            await session.commit()
            return stored

    async def list_shown(
        self, user_id: UUID, conversation_id: UUID
    ) -> Sequence[StoredClarification]:
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(ClarificationOrm)
                .where(
                    ClarificationOrm.user_id == user_id,
                    ClarificationOrm.conversation_id == conversation_id,
                )
                .order_by(ClarificationOrm.created_at, ClarificationOrm.id)
            )
            return [_to_domain(row) for row in rows]

    async def get(self, user_id: UUID, clarification_id: UUID) -> StoredClarification:
        async with self._session_factory() as session:
            row = await session.scalar(
                select(ClarificationOrm).where(
                    ClarificationOrm.user_id == user_id, ClarificationOrm.id == clarification_id
                )
            )
            if row is None:
                raise ClarificationNotFoundError(clarification_id)
            return _to_domain(row)

    async def resolve(
        self,
        user_id: UUID,
        clarification_id: UUID,
        status: ClarificationStatus,
        *,
        option_ids: Sequence[str] | None,
        resolved_at: datetime,
    ) -> StoredClarification:
        async with self._session_factory() as session:
            # One conditional UPDATE, so two answers at once resolve it once: the loser matches
            # no `open` row.
            row = await session.scalar(
                update(ClarificationOrm)
                .where(
                    ClarificationOrm.user_id == user_id,
                    ClarificationOrm.id == clarification_id,
                    ClarificationOrm.status == _OPEN,
                )
                .values(
                    status=status,
                    answer_option_ids=None if option_ids is None else list(option_ids),
                    answered_at=resolved_at,
                )
                .returning(ClarificationOrm)
            )
            if row is None:
                exists = await session.scalar(
                    select(ClarificationOrm.id).where(
                        ClarificationOrm.user_id == user_id,
                        ClarificationOrm.id == clarification_id,
                    )
                )
                if exists is None:
                    raise ClarificationNotFoundError(clarification_id)
                raise ClarificationResolvedError(clarification_id)
            stored = _to_domain(row)
            await session.commit()
            return stored

    async def reopen(self, user_id: UUID, clarification_id: UUID) -> None:
        async with self._session_factory() as session:
            await session.execute(
                update(ClarificationOrm)
                .where(
                    ClarificationOrm.user_id == user_id,
                    ClarificationOrm.id == clarification_id,
                    ClarificationOrm.status == "answered",
                )
                .values(status=_OPEN, answer_option_ids=None, answered_at=None)
            )
            await session.commit()
