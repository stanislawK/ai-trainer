"""An athlete's custom exercises and aliases are their own (ADR-0031 invariant 3, G1, G7)."""

from collections.abc import Callable
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.exercise_repository import SqlAlchemyExerciseRepository
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.application.exercise_catalogue import import_catalogue
from ai_trainer.domain.exercises import ExerciseMeasure
from ai_trainer.domain.users import NewUser, UserStatus
from tests.exercise_support import sample_source

SessionFactory = Callable[[], AsyncSession]


async def _user(factory: SessionFactory, sub: str) -> UUID:
    user = await SqlAlchemyUsersRepository(factory).create(
        NewUser(
            sub=sub, email=f"{sub}@example.com", name=None, locale="en", status=UserStatus.ACTIVE
        )
    )
    return user.id


@pytest.fixture
def repository(db_session_factory: SessionFactory) -> SqlAlchemyExerciseRepository:
    return SqlAlchemyExerciseRepository(db_session_factory)


async def _count(factory: SessionFactory, statement: str, **params: object) -> object:
    async with factory() as session:
        return (await session.execute(text(statement), params)).scalar_one()


async def test_deleting_an_account_removes_custom_exercises_and_aliases(
    db_session_factory: SessionFactory, repository: SqlAlchemyExerciseRepository
) -> None:
    await import_catalogue(source=sample_source(), exercises=repository)
    leaving = await _user(db_session_factory, "exercise-leaving")
    staying = await _user(db_session_factory, "exercise-staying")
    custom = await repository.create_custom(
        leaving, sport_id="gym", name="My Row", default_measure=ExerciseMeasure.REPS
    )
    await repository.add_user_alias(leaving, custom.id, "my row")
    kept = await repository.create_custom(
        staying, sport_id="gym", name="Your Row", default_measure=ExerciseMeasure.REPS
    )

    await SqlAlchemyUsersRepository(db_session_factory).delete(leaving)

    assert (
        await _count(
            db_session_factory, "SELECT count(*) FROM exercises WHERE id = :id", id=custom.id
        )
        == 0
    )
    assert (
        await _count(
            db_session_factory,
            "SELECT count(*) FROM exercise_aliases WHERE user_id = :id",
            id=leaving,
        )
        == 0
    )
    assert await repository.get(staying, kept.id) == kept
    assert (
        await _count(
            db_session_factory,
            "SELECT count(*) FROM exercises WHERE source_ref LIKE 'zz-test-%'",
        )
        == 9
    )


async def test_one_user_cannot_read_anothers_custom_exercise_or_alias(
    db_session_factory: SessionFactory, repository: SqlAlchemyExerciseRepository
) -> None:
    owner = await _user(db_session_factory, "exercise-owner")
    other = await _user(db_session_factory, "exercise-other")
    custom = await repository.create_custom(
        owner, sport_id="gym", name="My Row", default_measure=ExerciseMeasure.REPS
    )
    await repository.add_user_alias(owner, custom.id, "my row")

    assert await repository.get(other, custom.id) is None
    assert await repository.user_aliases(other) == []
    assert await repository.add_user_alias(other, custom.id, "stolen") is None
    assert await repository.user_aliases(other) == []


async def test_a_catalogue_exercise_is_visible_to_everyone_and_takes_user_aliases(
    db_session_factory: SessionFactory, repository: SqlAlchemyExerciseRepository
) -> None:
    await import_catalogue(source=sample_source(), exercises=repository)
    user = await _user(db_session_factory, "exercise-reader")
    async with db_session_factory() as session:
        catalogue_id = (
            await session.execute(text("SELECT id FROM exercises WHERE source_ref = 'zz-test-01'"))
        ).scalar_one()

    found = await repository.get(user, catalogue_id)
    alias = await repository.add_user_alias(user, catalogue_id, "  ŁAWKA   Płaska ")

    assert found is not None
    assert found.name == "Barbell Bench Press"
    assert found.owner_user_id is None
    assert found.default_measure == ExerciseMeasure.LOAD_REPS
    assert alias is not None
    assert alias.alias_norm == "lawka plaska"


async def test_a_user_alias_points_at_one_exercise_the_latest_confirmed(
    db_session_factory: SessionFactory, repository: SqlAlchemyExerciseRepository
) -> None:
    user = await _user(db_session_factory, "exercise-remapper")
    first = await repository.create_custom(
        user, sport_id="gym", name="First", default_measure=ExerciseMeasure.REPS
    )
    second = await repository.create_custom(
        user, sport_id="gym", name="Second", default_measure=ExerciseMeasure.REPS
    )

    await repository.add_user_alias(user, first.id, "my lift")
    remapped = await repository.add_user_alias(user, second.id, "MY  lift")

    assert remapped is not None
    assert await repository.user_aliases(user) == [remapped]
    assert remapped.exercise_id == second.id


async def test_get_of_an_unknown_id_is_none(
    db_session_factory: SessionFactory, repository: SqlAlchemyExerciseRepository
) -> None:
    user = await _user(db_session_factory, "exercise-unknown")

    assert await repository.get(user, uuid4()) is None
