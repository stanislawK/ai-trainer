from collections.abc import Callable
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.onboarding_repository import SqlAlchemyOnboardingRepository
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.domain.goals import Goal
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


async def test_replace_availability_stores_exactly_the_given_days(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyOnboardingRepository(db_session_factory)
    user_id = await _user(db_session_factory, "sub-h")

    await repository.replace_availability(user_id, {0: 60, 2: 90, 5: 180})

    assert dict(await repository.list_availability(user_id)) == {0: 60, 2: 90, 5: 180}


async def test_replace_availability_drops_days_no_longer_given(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyOnboardingRepository(db_session_factory)
    user_id = await _user(db_session_factory, "sub-i")
    await repository.replace_availability(user_id, {0: 60, 2: 90})

    await repository.replace_availability(user_id, {2: 45, 6: 120})

    assert dict(await repository.list_availability(user_id)) == {2: 45, 6: 120}


async def test_replace_availability_only_touches_the_given_user(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyOnboardingRepository(db_session_factory)
    first = await _user(db_session_factory, "sub-j")
    second = await _user(db_session_factory, "sub-k")
    await repository.replace_availability(first, {0: 60})

    await repository.replace_availability(second, {1: 30})

    assert dict(await repository.list_availability(first)) == {0: 60}
    assert dict(await repository.list_availability(second)) == {1: 30}


async def test_list_availability_of_a_user_with_none_is_empty(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyOnboardingRepository(db_session_factory)

    assert dict(await repository.list_availability(uuid4())) == {}


async def test_the_database_rejects_minutes_outside_1_to_600(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user_id = await _user(db_session_factory, "sub-l")

    for minutes in (0, 601, -1):
        async with db_session_factory() as session:
            try:
                await session.execute(
                    text(
                        "INSERT INTO weekly_availability (user_id, weekday, minutes) "
                        "VALUES (:user_id, 0, :minutes)"
                    ),
                    {"user_id": user_id, "minutes": minutes},
                )
                await session.commit()
            except IntegrityError:
                await session.rollback()
            else:
                raise AssertionError(f"{minutes} minutes was accepted")


async def test_deleting_a_user_cascades_their_weekly_availability(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyOnboardingRepository(db_session_factory)
    users = SqlAlchemyUsersRepository(db_session_factory)
    user_id = await _user(db_session_factory, "sub-m")
    await repository.replace_availability(user_id, {0: 60, 2: 90, 5: 180})

    await users.delete(user_id)

    async with db_session_factory() as session:
        count = await session.execute(
            text("SELECT count(*) FROM weekly_availability WHERE user_id = :user_id"),
            {"user_id": user_id},
        )
        assert count.scalar_one() == 0


async def test_replace_goals_stores_exactly_the_given_goals_in_order(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyOnboardingRepository(db_session_factory)
    user_id = await _user(db_session_factory, "sub-n")
    goals = [
        Goal(text="Send 8a+ by spring", target_date=date(2027, 4, 30)),
        Goal(text="Ride 100 km in one go", target_date=None),
        Goal(text="Stay consistent", target_date=None),
    ]

    await repository.replace_goals(user_id, goals)

    assert list(await repository.list_goals(user_id)) == goals


async def test_replace_goals_drops_goals_no_longer_given(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyOnboardingRepository(db_session_factory)
    user_id = await _user(db_session_factory, "sub-o")
    await repository.replace_goals(
        user_id, [Goal(text="First", target_date=None), Goal(text="Removed", target_date=None)]
    )

    await repository.replace_goals(user_id, [Goal(text="First", target_date=None)])

    assert list(await repository.list_goals(user_id)) == [Goal(text="First", target_date=None)]


async def test_replace_goals_keeps_two_identical_goals(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyOnboardingRepository(db_session_factory)
    user_id = await _user(db_session_factory, "sub-p")
    same = Goal(text="Climb more", target_date=None)

    await repository.replace_goals(user_id, [same, same])

    assert list(await repository.list_goals(user_id)) == [same, same]


async def test_replace_goals_only_touches_the_given_user(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyOnboardingRepository(db_session_factory)
    first = await _user(db_session_factory, "sub-q")
    second = await _user(db_session_factory, "sub-r")
    await repository.replace_goals(first, [Goal(text="Mine", target_date=None)])

    await repository.replace_goals(second, [Goal(text="Theirs", target_date=None)])

    assert list(await repository.list_goals(first)) == [Goal(text="Mine", target_date=None)]
    assert list(await repository.list_goals(second)) == [Goal(text="Theirs", target_date=None)]


async def test_the_database_keeps_one_goal_per_user_and_position(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    # What stops a double-tapped Continue, whose two saves may both delete before either
    # inserts, from storing every goal twice: the loser's rows conflict and are dropped.
    user_id = await _user(db_session_factory, "sub-u")
    insert_first = text(
        "INSERT INTO goals (id, user_id, text, position) "
        "VALUES (gen_random_uuid(), :user_id, 'a', 0)"
    )
    async with db_session_factory() as session:
        await session.execute(insert_first, {"user_id": user_id})
        await session.commit()

    async with db_session_factory() as session:
        try:
            await session.execute(insert_first, {"user_id": user_id})
            await session.commit()
        except IntegrityError:
            await session.rollback()
        else:
            raise AssertionError("a second goal at position 0 was accepted")


async def test_list_goals_of_a_user_with_none_is_empty(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyOnboardingRepository(db_session_factory)

    assert list(await repository.list_goals(uuid4())) == []


async def test_the_database_rejects_goal_text_that_is_empty_or_over_200_characters(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user_id = await _user(db_session_factory, "sub-s")

    for bad in ("", "x" * 201):
        async with db_session_factory() as session:
            try:
                await session.execute(
                    text(
                        "INSERT INTO goals (id, user_id, text, position) "
                        "VALUES (gen_random_uuid(), :user_id, :text, 0)"
                    ),
                    {"user_id": user_id, "text": bad},
                )
                await session.commit()
            # varchar(200) rejects 201 characters as a DataError before the CHECK runs.
            except IntegrityError, DataError:
                await session.rollback()
            else:
                raise AssertionError(f"{len(bad)} characters was accepted")


async def test_deleting_a_user_cascades_their_goals(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyOnboardingRepository(db_session_factory)
    users = SqlAlchemyUsersRepository(db_session_factory)
    user_id = await _user(db_session_factory, "sub-t")
    await repository.replace_goals(
        user_id,
        [Goal(text="Climb 7a", target_date=date(2027, 1, 1)), Goal(text="Rest", target_date=None)],
    )

    await users.delete(user_id)

    async with db_session_factory() as session:
        count = await session.execute(
            text("SELECT count(*) FROM goals WHERE user_id = :user_id"), {"user_id": user_id}
        )
        assert count.scalar_one() == 0
