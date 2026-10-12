from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from ai_trainer.adapters.orm import ConversationOrm
from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.domain.chat import Conversation


def _to_domain(row: ConversationOrm) -> Conversation:
    return Conversation(
        id=row.id,
        title=row.title,
        created_at=row.created_at,
        last_message_at=row.last_message_at,
    )


class SqlAlchemyConversationRepository:
    """Implements `ConversationRepositoryPort` (ADR-0004: repositories live in adapters/). A
    conversation whose last message is more than `retention_days` old is invisible to `get` and
    `list_recent` whether or not the purge has run; the age is read from the `Clock` port
    (ADR-0014, ADR-0021)."""

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        *,
        clock: ClockPort,
        retention_days: int,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._retention = timedelta(days=retention_days)

    def _not_expired(self) -> ColumnElement[bool]:
        return ConversationOrm.last_message_at >= self._clock.now() - self._retention

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
                    ConversationOrm.user_id == user_id,
                    ConversationOrm.id == conversation_id,
                    self._not_expired(),
                )
            )
            return None if row is None else _to_domain(row)

    async def list_recent(self, user_id: UUID, limit: int) -> Sequence[Conversation]:
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(ConversationOrm)
                .where(ConversationOrm.user_id == user_id, self._not_expired())
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

    async def purge_before(self, cutoff: datetime) -> int:
        async with self._session_factory() as session:
            deleted = await session.scalars(
                delete(ConversationOrm)
                .where(ConversationOrm.last_message_at < cutoff)
                .returning(ConversationOrm.id)
            )
            count = len(deleted.all())
            await session.commit()
            return count
