"""Shortlisting an outside name against the shared catalogue only (#552, #555): trigram similarity
over the catalogue's names and catalogue aliases, at a floor, best first and at most a limit, never
an athlete's own exercise or alias. Each test uses its own sport ID, so rows from other tests can't
match."""

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
    factory: SessionFactory,
    sport: str,
    name: str,
    *,
    owner: UUID | None = None,
    equipment: str | None = None,
    target: str | None = None,
) -> UUID:
    exercise_id = uuid4()
    async with factory() as session:
        await session.execute(
            text(
                "INSERT INTO exercises "
                "(id, sport_id, owner_user_id, source, source_ref, name, equipment, target, "
                "default_measure) "
                "VALUES (:id, :sport, :owner, :source, :ref, :name, :equipment, :target, 'reps')"
            ),
            {
                "id": exercise_id,
                "sport": sport,
                "owner": owner,
                "source": "custom" if owner else "zz-match",
                "ref": None if owner else f"zz-match-{exercise_id}",
                "name": name,
                "equipment": equipment,
                "target": target,
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


async def _shortlist(
    factory: SessionFactory, sport: str, name: str, *, floor: float = 0.25, limit: int = 4
) -> list[UUID]:
    found = await SqlAlchemyCatalogueMatch(factory).catalogue_shortlist(
        sport_id=sport, name=name, floor=floor, limit=limit
    )
    return [candidate.id for candidate in found]


async def test_catalogue_shortlist_is_best_first_with_equipment_and_target(
    db_session_factory: SessionFactory, sport: str
) -> None:
    narrow = await _exercise(
        db_session_factory,
        sport,
        "Bench Press Narrow Grip",
        equipment="barbell",
        target="triceps",
    )
    bench = await _exercise(db_session_factory, sport, "Bench Press")

    found = await SqlAlchemyCatalogueMatch(db_session_factory).catalogue_shortlist(
        sport_id=sport, name="bench press narrow grip", floor=0.25, limit=4
    )

    assert [candidate.id for candidate in found] == [narrow, bench]
    assert (found[0].name, found[0].equipment, found[0].target) == (
        "Bench Press Narrow Grip",
        "barbell",
        "triceps",
    )
    assert found[0].name_score is not None
    assert found[1].name_score is not None
    assert found[0].name_score > found[1].name_score


async def test_catalogue_shortlist_respects_the_floor(
    db_session_factory: SessionFactory, sport: str
) -> None:
    await _exercise(db_session_factory, sport, "Barbell Bench Press")
    await _exercise(db_session_factory, sport, "Stomach Vacuum")

    assert await _shortlist(db_session_factory, sport, "Bench Press", floor=0.75) == []
    assert len(await _shortlist(db_session_factory, sport, "Bench Press", floor=0.25)) == 1


async def test_catalogue_shortlist_respects_the_limit(
    db_session_factory: SessionFactory, sport: str
) -> None:
    for name in ("Push-up", "Push Up Plus", "Push-up Wide", "Push-up Close"):
        await _exercise(db_session_factory, sport, name)

    assert len(await _shortlist(db_session_factory, sport, "Push Up", limit=2)) == 2


async def test_catalogue_shortlist_reads_catalogue_aliases(
    db_session_factory: SessionFactory, sport: str
) -> None:
    vacuum = await _exercise(db_session_factory, sport, "Abdominal Hollowing")
    await _alias(db_session_factory, vacuum, "Stomach Vacuum")

    assert await _shortlist(db_session_factory, sport, "Stomach Vacuum", floor=0.75) == [vacuum]


async def test_catalogue_shortlist_ignores_athlete_exercises_and_aliases(
    db_session_factory: SessionFactory, sport: str
) -> None:
    athlete = await _user(db_session_factory)
    await _exercise(db_session_factory, sport, "Stomach Vacuum", owner=athlete)
    catalogue = await _exercise(db_session_factory, sport, "Abdominal Hollowing")
    await _alias(db_session_factory, catalogue, "Stomach Vacuum", user_id=athlete)

    assert await _shortlist(db_session_factory, sport, "Stomach Vacuum") == []


async def test_catalogue_shortlist_stays_in_its_sport(
    db_session_factory: SessionFactory, sport: str
) -> None:
    await _exercise(db_session_factory, f"{sport}-other", "Crunches")

    assert await _shortlist(db_session_factory, sport, "Crunches") == []
