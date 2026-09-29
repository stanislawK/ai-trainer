from collections.abc import Callable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.orm import UserOrm, UserStatusChangeOrm
from ai_trainer.domain.user_status_changes import NewUserStatusChange
from ai_trainer.domain.users import User, UserNotFoundError, UserStatus


class SqlAlchemyUserStatusChanger:
    """Implements `UserStatusChangerPort` (ADR-0004): the user row update and the audit insert
    share one session and one commit, so they can never diverge (ADR-0005 invariant 9)."""

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def change(
        self, *, target_user_id: UUID, new_status: UserStatus, actor_user_id: UUID
    ) -> tuple[User, UserStatus]:
        async with self._session_factory() as session:
            # Locked so two overlapping changes serialize: the second re-reads the committed
            # status rather than acting on a stale one.
            row = await session.scalar(
                select(UserOrm).where(UserOrm.id == target_user_id).with_for_update()
            )
            if row is None:
                raise UserNotFoundError(target_user_id)

            old_status = UserStatus(row.status)
            if old_status is new_status:
                return _to_user(row), old_status

            change = NewUserStatusChange(
                actor_user_id=actor_user_id,
                target_user_id=target_user_id,
                old_status=old_status,
                new_status=new_status,
            )
            row.status = new_status.value
            session.add(
                UserStatusChangeOrm(
                    actor_user_id=change.actor_user_id,
                    target_user_id=change.target_user_id,
                    old_status=change.old_status.value,
                    new_status=change.new_status.value,
                )
            )
            await session.commit()
            await session.refresh(row)
            return _to_user(row), old_status


def _to_user(row: UserOrm) -> User:
    return User(
        id=row.id,
        sub=row.sub,
        email=row.email,
        name=row.name,
        locale=row.locale,
        status=UserStatus(row.status),
        created_at=row.created_at,
    )
