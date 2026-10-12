from collections.abc import Callable, Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.orm import ConversationOrm
from ai_trainer.domain.chat import Conversation


def _to_domain(row: ConversationOrm) -> Conversation:
    return Conversation(
        id=row.id,
        title=row.title,
        created_at=row.created_at,
        last_message_at=row.last_message_at,
    )


class SqlAlchemyConversationRepository:
    """Implements `ConversationRepositoryPort` (ADR-0004: repositories live in adapters/)."""

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(self, user_id: UUID, title: str | None, now: datetime) -> Conversation:
        async with self._session_factory() as session:
            row = ConversationOrm(user_id=user_id, title=title, created_at=now, last_message_at=now)
            session.add(row)
            # Flushed, not read after the commit: a committed row's attributes expire.
            await session.flush()
            conversation = _to_domain(row)
            await session.commit()
            return conversation

    async def get(self, user_id: UUID, conversation_id: UUID) -> Conversation | None:
        async with self._session_factory() as session:
            row = await session.scalar(
                select(ConversationOrm).where(
                    ConversationOrm.user_id == user_id, ConversationOrm.id == conversation_id
                )
            )
            return None if row is None else _to_domain(row)

    async def list_recent(self, user_id: UUID, limit: int) -> Sequence[Conversation]:
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(ConversationOrm)
                .where(ConversationOrm.user_id == user_id)
                .order_by(ConversationOrm.last_message_at.desc(), ConversationOrm.id.desc())
                .limit(limit)
            )
            return [_to_domain(row) for row in rows.all()]

    async def touch(self, user_id: UUID, conversation_id: UUID, when: datetime) -> None:
        async with self._session_factory() as session:
            await session.execute(
                update(ConversationOrm)
                .where(ConversationOrm.user_id == user_id, ConversationOrm.id == conversation_id)
                .values(last_message_at=func.greatest(ConversationOrm.last_message_at, when))
            )
            await session.commit()
