"""The athlete's delete of a saved session through `SqlAlchemySessionDeleter` (ticket #518,
ADR-0022, ADR-0032): one user-scoped `DELETE` takes the session, and its sources, gym entries and
sets go by their cascades; a draft that pointed at it keeps its row with no session; the
athlete's bands, aliases and custom exercises stay (ADR-0031 invariant 3); an unknown session or
another athlete's is not found and nothing is deleted."""

from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.exercise_repository import SqlAlchemyExerciseRepository
from ai_trainer.adapters.training_session_editor import SqlAlchemySessionDeleter
from ai_trainer.domain.training import SessionNotFoundError
from tests.integration.test_gym_activity_store import _custom, _gym, _id, _logged, _sets, _settled
from tests.integration.test_session_continuity_repository import _athlete, _session


async def _count(factory: Callable[[], AsyncSession], sql: str, **params: Any) -> int:
    async with factory() as db:
        return int(await db.scalar(text(sql), params) or 0)


async def _remaining(factory: Callable[[], AsyncSession], session_id: UUID) -> tuple[int, ...]:
    """The session's own rows: itself, its sources, its gym entries and its sets."""
    return (
        await _count(
            factory, "SELECT count(*) FROM training_sessions WHERE id = :id", id=session_id
        ),
        await _count(
            factory,
            "SELECT count(*) FROM training_session_sources WHERE session_id = :id",
            id=session_id,
        ),
        await _count(
            factory,
            "SELECT count(*) FROM gym_exercise_entries WHERE session_id = :id",
            id=session_id,
        ),
        len(await _sets(factory, session_id)),
    )


async def test_delete_removes_the_session_and_its_rows_and_keeps_the_athletes_own(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    athlete = await _athlete(db_session_factory, "delete-own")
    exercise_id = await _custom(athlete, db_session_factory, "Bench")
    confirmed = await _logged(athlete, "bench 3x8 60", _settled(exercise_id, "bench press"))
    session_id = _id(confirmed.session_id)
    await _logged(
        athlete,
        "red band rows",
        _gym({"name": "rows", "sets": 3, "reps": 10, "band": "red"}),
        extends=session_id,
    )
    assert await _remaining(db_session_factory, session_id) == (1, 2, 2, 6)

    await SqlAlchemySessionDeleter(db_session_factory).delete(athlete.user_id, session_id)

    assert await _remaining(db_session_factory, session_id) == (0, 0, 0, 0)
    assert await _session(db_session_factory, session_id) is None
    # The drafts that made it keep their rows, pointing at no session.
    async with db_session_factory() as db:
        drafts = (
            await db.execute(
                text("SELECT session_id FROM session_drafts WHERE id = :id"),
                {"id": confirmed.id},
            )
        ).all()
    assert [row[0] for row in drafts] == [None]
    # The athlete's own data stays (ADR-0031 invariant 3, ADR-0032).
    exercises = SqlAlchemyExerciseRepository(db_session_factory)
    [alias] = await exercises.user_aliases(athlete.user_id)
    assert (alias.alias, alias.exercise_id) == ("bench press", exercise_id)
    assert await exercises.get(athlete.user_id, exercise_id) is not None
    assert (
        await _count(
            db_session_factory,
            "SELECT count(*) FROM athlete_bands WHERE user_id = :id",
            id=athlete.user_id,
        )
        == 1
    )


async def test_unknown_session_is_not_found(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    athlete = await _athlete(db_session_factory, "delete-unknown")

    with pytest.raises(SessionNotFoundError):
        await SqlAlchemySessionDeleter(db_session_factory).delete(athlete.user_id, uuid4())


async def test_another_athletes_session_is_not_found_and_kept(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    owner = await _athlete(db_session_factory, "delete-owner")
    stranger = await _athlete(db_session_factory, "delete-stranger")
    exercise_id = await _custom(owner, db_session_factory, "Bench")
    confirmed = await _logged(owner, "bench 3x8 60", _settled(exercise_id, "bench press"))
    session_id = _id(confirmed.session_id)
    before = await _remaining(db_session_factory, session_id)

    with pytest.raises(SessionNotFoundError):
        await SqlAlchemySessionDeleter(db_session_factory).delete(stranger.user_id, session_id)

    assert await _remaining(db_session_factory, session_id) == before == (1, 1, 1, 3)
