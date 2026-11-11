"""Matching an outside name against the shared catalogue only (#552): trigram similarity over the
catalogue's names and catalogue aliases, at a threshold, never an athlete's own exercise or alias.
Each test uses its own sport ID, so rows from other tests can't match."""

from collections.abc import Callable
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.catalogue_match import SqlAlchemyCatalogueMatch

SessionFactory = Callable[[], AsyncSession]


@pytest.fixture
def sport() -> str:
    return f"zz-match-{uuid4()}"


async def _exercise(
    factory: SessionFactory, sport: str, name: str, *, owner: UUID | None = None
) -> UUID:
    exercise_id = uuid4()
    async with factory() as session:
        await session.execute(
            text(
                "INSERT INTO exercises "
                "(id, sport_id, owner_user_id, source, source_ref, name, default_measure) "
                "VALUES (:id, :sport, :owner, :source, :ref, :name, 'reps')"
            ),
            {
                "id": exercise_id,
                "sport": sport,
                "owner": owner,
                "source": "custom" if owner else "zz-match",
                "ref": None if owner else f"zz-match-{exercise_id}",
                "name": name,
            },
        )
        await session.commit()
    return exercise_id


async def _alias(
    factory: SessionFactory, exercise_id: UUID, alias: str, *, user_id: UUID | None = None
) -> None:
    async with factory() as session:
        await session.execute(
            text(
                "INSERT INTO exercise_aliases (id, exercise_id, user_id, alias) "
                "VALUES (:id, :exercise, :user, :alias)"
            ),
            {"id": uuid4(), "exercise": exercise_id, "user": user_id, "alias": alias},
        )
        await session.commit()


async def _user(factory: SessionFactory) -> UUID:
    user_id = uuid4()
    async with factory() as session:
        await session.execute(
            text(
                "INSERT INTO users (id, sub, email, locale, status) "
                "VALUES (:id, :sub, :email, 'en', 'active')"
            ),
            {"id": user_id, "sub": f"zz-{user_id}", "email": f"zz-{user_id}@example.com"},
        )
        await session.commit()
    return user_id


async def test_catalogue_best_match_picks_the_most_similar_name(
    db_session_factory: SessionFactory, sport: str
) -> None:
    bench = await _exercise(db_session_factory, sport, "Bench Press Narrow Grip")
    await _exercise(db_session_factory, sport, "Bench Press")

    found = await SqlAlchemyCatalogueMatch(db_session_factory).best_catalogue_match(
        sport_id=sport, name="bench press narrow grip", threshold=0.75
    )

    assert found == bench


async def test_catalogue_best_match_below_floor_is_none(
    db_session_factory: SessionFactory, sport: str
) -> None:
    # Every word of the name is in "Barbell Bench Press", which a full-text hit would lift to
    # any floor; trigram similarity alone keeps it below 0.75.
    await _exercise(db_session_factory, sport, "Barbell Bench Press")

    found = await SqlAlchemyCatalogueMatch(db_session_factory).best_catalogue_match(
        sport_id=sport, name="Bench Press", threshold=0.75
    )

    assert found is None


async def test_catalogue_best_match_reads_catalogue_aliases(
    db_session_factory: SessionFactory, sport: str
) -> None:
    vacuum = await _exercise(db_session_factory, sport, "Abdominal Hollowing")
    await _alias(db_session_factory, vacuum, "Stomach Vacuum")

    found = await SqlAlchemyCatalogueMatch(db_session_factory).best_catalogue_match(
        sport_id=sport, name="Stomach Vacuum", threshold=0.75
    )

    assert found == vacuum


async def test_catalogue_best_match_ignores_athlete_exercises(
    db_session_factory: SessionFactory, sport: str
) -> None:
    athlete = await _user(db_session_factory)
    await _exercise(db_session_factory, sport, "Stomach Vacuum", owner=athlete)
    catalogue = await _exercise(db_session_factory, sport, "Abdominal Hollowing")
    await _alias(db_session_factory, catalogue, "Stomach Vacuum", user_id=athlete)

    found = await SqlAlchemyCatalogueMatch(db_session_factory).best_catalogue_match(
        sport_id=sport, name="Stomach Vacuum", threshold=0.75
    )

    assert found is None


async def test_catalogue_best_match_stays_in_its_sport(
    db_session_factory: SessionFactory, sport: str
) -> None:
    await _exercise(db_session_factory, f"{sport}-other", "Crunches")

    found = await SqlAlchemyCatalogueMatch(db_session_factory).best_catalogue_match(
        sport_id=sport, name="Crunches", threshold=0.75
    )

    assert found is None
