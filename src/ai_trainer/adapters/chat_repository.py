from collections.abc import Callable, Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.orm import ChatMessageOrm
from ai_trainer.domain.chat import ChatMessage, ChatRole


def _to_domain(row: ChatMessageOrm) -> ChatMessage:
    return ChatMessage(id=row.id, role=ChatRole(row.role), text=row.text, created_at=row.created_at)


class SqlAlchemyChatRepository:
    """Implements `ChatRepositoryPort` (ADR-0004: repositories live in adapters/)."""

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add(
        self, user_id: UUID, role: ChatRole, text: str, created_at: datetime
    ) -> ChatMessage:
        async with self._session_factory() as session:
            row = ChatMessageOrm(user_id=user_id, role=role.value, text=text, created_at=created_at)
            session.add(row)
            # Flushed, not read after the commit: a committed row's attributes expire.
            await session.flush()
            message = _to_domain(row)
            await session.commit()
            return message

    async def list_recent(self, user_id: UUID, limit: int) -> Sequence[ChatMessage]:
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(ChatMessageOrm)
                .where(ChatMessageOrm.user_id == user_id)
                .order_by(ChatMessageOrm.created_at.desc(), ChatMessageOrm.id.desc())
                .limit(limit)
            )
            return [_to_domain(row) for row in reversed(rows.all())]
