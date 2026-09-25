from collections.abc import Callable, Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.orm import UserOrm
from ai_trainer.domain.users import NewUser, User, UserAlreadyExistsError, UserStatus


def _to_domain(row: UserOrm) -> User:
    return User(
        id=row.id,
        sub=row.sub,
        email=row.email,
        name=row.name,
        locale=row.locale,
        status=UserStatus(row.status),
        created_at=row.created_at,
    )


class SqlAlchemyUsersRepository:
    """Implements `UsersRepositoryPort` (ADR-0004: repositories live in adapters/)."""

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_by_sub(self, sub: str) -> User | None:
        async with self._session_factory() as session:
            row = await session.scalar(select(UserOrm).where(UserOrm.sub == sub))
            return _to_domain(row) if row is not None else None

    async def get(self, user_id: UUID) -> User | None:
        async with self._session_factory() as session:
            row = await session.get(UserOrm, user_id)
            return _to_domain(row) if row is not None else None

    async def create(self, new_user: NewUser) -> User:
        async with self._session_factory() as session:
            row = UserOrm(
                sub=new_user.sub,
                email=new_user.email,
                name=new_user.name,
                locale=new_user.locale,
                status=new_user.status.value,
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError as exc:
                # ix_users_sub is UNIQUE: a concurrent sign-in for the same `sub` can win the
                # race between our `get_by_sub` and this insert (ADR-0005).
                await session.rollback()
                raise UserAlreadyExistsError(new_user.sub) from exc
            await session.refresh(row)
            return _to_domain(row)

    async def list_all(self) -> Sequence[User]:
        async with self._session_factory() as session:
            rows = await session.scalars(select(UserOrm).order_by(UserOrm.created_at))
            return [_to_domain(row) for row in rows]
