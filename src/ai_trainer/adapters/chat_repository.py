from collections.abc import Callable, Sequence
from datetime import datetime
from itertools import takewhile
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.orm import ChatMessageOrm
from ai_trainer.domain.chat import ChatMessage, ChatRole

# Replies to one message are a handful of parts (ADR-0008); this only bounds the read.
_MAX_REPLY_PARTS = 20


def _to_domain(row: ChatMessageOrm) -> ChatMessage:
    return ChatMessage(
        id=row.id,
        role=ChatRole(row.role),
        text=row.text,
        created_at=row.created_at,
        sports=tuple(row.sports or ()),
        template_id=row.template_id,
        template_version=row.template_version,
    )


class SqlAlchemyChatRepository:
    """Implements `ChatRepositoryPort` (ADR-0004: repositories live in adapters/)."""

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add(
        self,
        user_id: UUID,
        role: ChatRole,
        text: str,
        created_at: datetime,
        *,
        sports: Sequence[str] = (),
        template_id: str | None = None,
        template_version: int | None = None,
    ) -> ChatMessage:
        async with self._session_factory() as session:
            row = ChatMessageOrm(
                user_id=user_id,
                role=role.value,
                text=text,
                created_at=created_at,
                sports=list(sports) if role is ChatRole.ASSISTANT else None,
                template_id=template_id,
                template_version=template_version,
            )
            session.add(row)
            # Flushed, not read after the commit: a committed row's attributes expire.
            await session.flush()
            message = _to_domain(row)
            await session.commit()
            return message

    async def get(self, user_id: UUID, message_id: UUID) -> ChatMessage | None:
        async with self._session_factory() as session:
            row = await session.scalar(
                select(ChatMessageOrm).where(
                    ChatMessageOrm.user_id == user_id, ChatMessageOrm.id == message_id
                )
            )
            return None if row is None else _to_domain(row)

    async def list_recent(self, user_id: UUID, limit: int) -> Sequence[ChatMessage]:
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(ChatMessageOrm)
                .where(ChatMessageOrm.user_id == user_id)
                .order_by(ChatMessageOrm.created_at.desc(), ChatMessageOrm.id.desc())
                .limit(limit)
            )
            return [_to_domain(row) for row in reversed(rows.all())]

    async def list_before(
        self, user_id: UUID, before: ChatMessage, limit: int
    ) -> Sequence[ChatMessage]:
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(ChatMessageOrm)
                .where(
                    ChatMessageOrm.user_id == user_id,
                    ChatMessageOrm.created_at < before.created_at,
                )
                .order_by(ChatMessageOrm.created_at.desc(), ChatMessageOrm.id.desc())
                .limit(limit)
            )
            return [_to_domain(row) for row in reversed(rows.all())]

    async def list_replies(self, user_id: UUID, to: ChatMessage) -> Sequence[ChatMessage]:
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(ChatMessageOrm)
                .where(
                    ChatMessageOrm.user_id == user_id,
                    ChatMessageOrm.created_at > to.created_at,
                )
                .order_by(ChatMessageOrm.created_at, ChatMessageOrm.id)
                .limit(_MAX_REPLY_PARTS)
            )
            messages = (_to_domain(row) for row in rows.all())
            return list(takewhile(lambda message: message.role is ChatRole.ASSISTANT, messages))
