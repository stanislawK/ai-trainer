from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.onboarding_repository import SqlAlchemyOnboardingRepository
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.domain.users import NewUser, UserStatus


async def _user(factory: Callable[[], AsyncSession], sub: str) -> UUID:
    created = await SqlAlchemyUsersRepository(factory).create(
        NewUser(
            sub=sub, email=f"{sub}@example.com", name=None, locale="en", status=UserStatus.ACTIVE
        )
    )
    return created.id


async def test_replace_sports_stores_exactly_the_given_sports(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyOnboardingRepository(db_session_factory)
    user_id = await _user(db_session_factory, "sub-a")

    await repository.replace_sports(user_id, ["climbing", "gym"])

    assert sorted(await repository.list_sports(user_id)) == ["climbing", "gym"]


async def test_replace_sports_drops_sports_no_longer_picked(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyOnboardingRepository(db_session_factory)
    user_id = await _user(db_session_factory, "sub-b")
    await repository.replace_sports(user_id, ["climbing", "gym"])

    await repository.replace_sports(user_id, ["gym", "cycling"])

    assert sorted(await repository.list_sports(user_id)) == ["cycling", "gym"]


async def test_replace_sports_only_touches_the_given_user(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyOnboardingRepository(db_session_factory)
    first = await _user(db_session_factory, "sub-c")
    second = await _user(db_session_factory, "sub-d")
    await repository.replace_sports(first, ["climbing"])

    await repository.replace_sports(second, ["gym"])

    assert list(await repository.list_sports(first)) == ["climbing"]
    assert list(await repository.list_sports(second)) == ["gym"]


async def test_list_sports_of_a_user_with_none_is_empty(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyOnboardingRepository(db_session_factory)

    assert list(await repository.list_sports(uuid4())) == []


async def test_set_onboarded_at_stamps_and_clears_the_users_row(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyOnboardingRepository(db_session_factory)
    users = SqlAlchemyUsersRepository(db_session_factory)
    user_id = await _user(db_session_factory, "sub-e")
    fresh = await users.get(user_id)
    assert fresh is not None
    assert fresh.onboarded_at is None

    when = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
    await repository.set_onboarded_at(user_id, when)
    stamped = await users.get(user_id)
    assert stamped is not None
    assert stamped.onboarded_at == when

    await repository.set_onboarded_at(user_id, None)
    cleared = await users.get(user_id)
    assert cleared is not None
    assert cleared.onboarded_at is None


async def test_deleting_a_user_cascades_their_user_sports(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyOnboardingRepository(db_session_factory)
    users = SqlAlchemyUsersRepository(db_session_factory)
    user_id = await _user(db_session_factory, "sub-f")
    await repository.replace_sports(user_id, ["climbing", "gym"])

    await users.delete(user_id)

    async with db_session_factory() as session:
        count = await session.execute(
            text("SELECT count(*) FROM user_sports WHERE user_id = :user_id"),
            {"user_id": user_id},
        )
        assert count.scalar_one() == 0


async def test_replace_sports_with_nothing_clears_the_users_sports(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyOnboardingRepository(db_session_factory)
    user_id = await _user(db_session_factory, "sub-g")
    await repository.replace_sports(user_id, ["gym"])

    await repository.replace_sports(user_id, [])

    assert list(await repository.list_sports(user_id)) == []
