"""`SqlAlchemyUserStatusChanger`: the user-row update and the audit insert commit atomically in
one transaction (ADR-0005 invariant 9, ticket #16 review-gate skeptic finding)."""

from collections.abc import Callable
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.orm import UserStatusChangeOrm
from ai_trainer.adapters.user_status_changer import SqlAlchemyUserStatusChanger
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.domain.users import NewUser, UserNotFoundError, UserStatus


async def _create_user(
    users: SqlAlchemyUsersRepository, *, sub: str, status: UserStatus = UserStatus.PENDING
) -> UUID:
    user = await users.create(
        NewUser(sub=sub, email=f"{sub}@example.com", name=sub, locale="en", status=status)
    )
    return user.id


async def test_change_updates_the_status_and_returns_the_previous_one(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    users = SqlAlchemyUsersRepository(db_session_factory)
    changer = SqlAlchemyUserStatusChanger(db_session_factory)
    target_id = await _create_user(users, sub="changer-target-1", status=UserStatus.PENDING)
    actor_id = await _create_user(users, sub="changer-actor-1")

    updated, previous_status = await changer.change(
        target_user_id=target_id, new_status=UserStatus.ACTIVE, actor_user_id=actor_id
    )

    assert updated.status is UserStatus.ACTIVE
    assert previous_status is UserStatus.PENDING
    found = await users.get(target_id)
    assert found is not None
    assert found.status is UserStatus.ACTIVE


async def test_change_records_an_audit_row_in_the_same_transaction(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    users = SqlAlchemyUsersRepository(db_session_factory)
    changer = SqlAlchemyUserStatusChanger(db_session_factory)
    target_id = await _create_user(users, sub="changer-target-2", status=UserStatus.PENDING)
    actor_id = await _create_user(users, sub="changer-actor-2")

    await changer.change(
        target_user_id=target_id, new_status=UserStatus.ACTIVE, actor_user_id=actor_id
    )

    async with db_session_factory() as session:
        rows = (
            await session.scalars(
                select(UserStatusChangeOrm).where(UserStatusChangeOrm.target_user_id == target_id)
            )
        ).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.actor_user_id == actor_id
    assert row.old_status == "pending"
    assert row.new_status == "active"
    assert row.created_at is not None


async def test_change_with_no_match_raises_user_not_found(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    changer = SqlAlchemyUserStatusChanger(db_session_factory)
    missing_id = uuid4()

    with pytest.raises(UserNotFoundError) as excinfo:
        await changer.change(
            target_user_id=missing_id, new_status=UserStatus.ACTIVE, actor_user_id=uuid4()
        )

    assert excinfo.value.user_id == missing_id
