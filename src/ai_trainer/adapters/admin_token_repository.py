from collections.abc import Callable, Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.orm import AdminTokenOrm
from ai_trainer.domain.admin_tokens import AdminToken, NewAdminToken


def _to_domain(row: AdminTokenOrm) -> AdminToken:
    # The hash stays in the table: nothing read back carries it.
    return AdminToken(
        id=row.id,
        user_id=row.user_id,
        display_prefix=row.display_prefix,
        created_at=row.created_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        last_used_at=row.last_used_at,
    )


class SqlAlchemyAdminTokensRepository:
    """Implements `AdminTokensRepositoryPort` (ADR-0036): every query is scoped by `user_id`."""

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add(self, token: NewAdminToken) -> AdminToken:
        row = AdminTokenOrm(**token.model_dump(mode="python"))
        async with self._session_factory() as session:
            session.add(row)
            await session.flush()
            stored = _to_domain(row)
            await session.commit()
            return stored

    async def list_for_user(self, user_id: UUID) -> Sequence[AdminToken]:
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(AdminTokenOrm)
                .where(AdminTokenOrm.user_id == user_id)
                .order_by(AdminTokenOrm.created_at.desc(), AdminTokenOrm.id)
            )
            return [_to_domain(row) for row in rows]

    async def get_by_hash(self, token_hash: str) -> AdminToken | None:
        async with self._session_factory() as session:
            row = await session.scalar(
                select(AdminTokenOrm).where(AdminTokenOrm.token_hash == token_hash)
            )
            return None if row is None else _to_domain(row)

    async def touch(self, user_id: UUID, token_id: UUID, *, at: datetime) -> None:
        async with self._session_factory() as session:
            await session.execute(
                update(AdminTokenOrm)
                .where(
                    AdminTokenOrm.user_id == user_id,
                    AdminTokenOrm.id == token_id,
                    # A revoke that lands mid-request isn't followed by a later use.
                    AdminTokenOrm.revoked_at.is_(None),
                    AdminTokenOrm.expires_at > at,
                )
                .values(last_used_at=at)
            )
            await session.commit()

    async def revoke(self, user_id: UUID, token_id: UUID, *, at: datetime) -> AdminToken | None:
        mine = (AdminTokenOrm.user_id == user_id, AdminTokenOrm.id == token_id)
        async with self._session_factory() as session:
            await session.execute(
                update(AdminTokenOrm)
                .where(
                    *mine,
                    AdminTokenOrm.revoked_at.is_(None),
                    AdminTokenOrm.expires_at > at,
                )
                .values(revoked_at=at)
            )
            row = await session.scalar(select(AdminTokenOrm).where(*mine))
            token = None if row is None else _to_domain(row)
            await session.commit()
            return token
