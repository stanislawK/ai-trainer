from collections.abc import Callable
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.orm import SessionOrm
from ai_trainer.domain.sessions import NewSession, Session


def _to_domain(row: SessionOrm) -> Session:
    return Session(
        id=row.id,
        user_id=row.user_id,
        expires_at=row.expires_at,
        created_at=row.created_at,
    )


class SqlAlchemySessionsRepository:
    """Implements `SessionsRepositoryPort` (ADR-0004: repositories live in adapters/)."""

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(self, new_session: NewSession) -> Session:
        async with self._session_factory() as session:
            row = SessionOrm(user_id=new_session.user_id, expires_at=new_session.expires_at)
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _to_domain(row)

    async def get(self, session_id: UUID) -> Session | None:
        async with self._session_factory() as session:
            row = await session.get(SessionOrm, session_id)
            return _to_domain(row) if row is not None else None

    async def delete(self, session_id: UUID) -> None:
        async with self._session_factory() as session:
            row = await session.get(SessionOrm, session_id)
            if row is not None:
                await session.delete(row)
                await session.commit()
