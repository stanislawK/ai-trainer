"""The gym backfill against the real Postgres (ADR-0032, ticket #377): a session from before the
typed tables gets its entries and sets with `exercise_id` and `source_id` NULL, once, and its
stored JSON is never touched."""

import json
from collections.abc import Callable
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.gym_backfill import SqlAlchemyGymBackfillStore
from ai_trainer.application.gym_backfill import backfill_gym_sessions
from ai_trainer.domain.sports.gym.payload import GymPayload
from ai_trainer.domain.sports.gym.rows import entries_of
from tests.integration.test_gym_activity_store import (
    _LATER,
    _from_before_the_tables,
    _gym,
    _gym_row_count,
    _lift,
    _logged,
    _sets,
)
from tests.integration.test_session_continuity_repository import _athlete


async def _entries_of_rows(
    factory: Callable[[], AsyncSession], session_id: UUID | None
) -> list[Any]:
    """Position, name, measure, exercise_id and source_id of each entry, backfilled or not."""
    async with factory() as db:
        rows = await db.execute(
            text(
                "SELECT position, name_as_typed, measure, exercise_id, source_id"
                " FROM gym_exercise_entries WHERE session_id = :id ORDER BY position"
            ),
            {"id": session_id},
        )
        return [tuple(row) for row in rows.all()]


async def _stored(factory: Callable[[], AsyncSession], session_id: UUID | None) -> Any:
    async with factory() as db:
        return (
            await db.execute(
                text(
                    "SELECT payload::text, payload_version, updated_at"
                    " FROM training_sessions WHERE id = :id"
                ),
                {"id": session_id},
            )
        ).one()


async def _old_session(factory: Callable[[], AsyncSession], sub: str) -> tuple[Any, UUID | None]:
    athlete = await _athlete(factory, sub)
    first = await _logged(
        athlete,
        "Squat 100kg 5x5, pull-ups 8",
        _gym(_lift("Squat", 5, 5, "100kg", 100), {"name": "Pull-ups", "reps": 8}),
    )
    await _from_before_the_tables(factory, first)
    return athlete, first.session_id


async def test_v1_session_gets_entries_and_sets_for_its_own_user(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    athlete, session_id = await _old_session(db_session_factory, "backfill-own")
    other, other_session_id = await _old_session(db_session_factory, "backfill-other")
    store = SqlAlchemyGymBackfillStore(db_session_factory)

    await backfill_gym_sessions(store=store)

    assert await _entries_of_rows(db_session_factory, session_id) == [
        (1, "Squat", "load_reps", None, None),
        (2, "Pull-ups", "reps", None, None),
    ]
    assert await _sets(db_session_factory, session_id) == [
        ("Squat", n, 5, 100, "100kg", None, None, None, None) for n in (1, 2, 3, 4, 5)
    ] + [("Pull-ups", 1, 8, None, None, None, None, None, None)]
    assert await _gym_row_count(db_session_factory, athlete.user_id) == (2, 6)
    assert await _gym_row_count(db_session_factory, other.user_id) == (2, 6)
    assert await _entries_of_rows(db_session_factory, other_session_id)


async def test_second_run_writes_nothing(db_session_factory: Callable[[], AsyncSession]) -> None:
    athlete, _ = await _old_session(db_session_factory, "backfill-twice")
    store = SqlAlchemyGymBackfillStore(db_session_factory)
    first = await backfill_gym_sessions(store=store)

    second = await backfill_gym_sessions(store=store)

    assert (first.sessions, first.entries) == (1, 2)
    assert (second.sessions, second.entries, second.sets, second.skipped) == (0, 0, 0, ())
    assert await _gym_row_count(db_session_factory, athlete.user_id) == (2, 6)


async def test_payload_and_version_are_unchanged(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    _, session_id = await _old_session(db_session_factory, "backfill-bytes")
    before = await _stored(db_session_factory, session_id)

    await backfill_gym_sessions(store=SqlAlchemyGymBackfillStore(db_session_factory))

    assert await _stored(db_session_factory, session_id) == before


async def test_unreadable_payload_is_reported_and_others_run(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    _, broken_id = await _old_session(db_session_factory, "backfill-broken")
    athlete, good_id = await _old_session(db_session_factory, "backfill-good")
    async with db_session_factory() as db:
        await db.execute(
            text("UPDATE training_sessions SET payload = CAST(:p AS jsonb) WHERE id = :id"),
            {"p": json.dumps({"sport": "gym", "activities": []}), "id": broken_id},
        )
        await db.commit()

    report = await backfill_gym_sessions(store=SqlAlchemyGymBackfillStore(db_session_factory))

    assert [(skip.session_id, skip.reason) for skip in report.skipped] == [
        (broken_id, "payload_invalid")
    ]
    assert await _entries_of_rows(db_session_factory, broken_id) == []
    assert len(await _entries_of_rows(db_session_factory, good_id)) == 2
    assert await _gym_row_count(db_session_factory, athlete.user_id) == (2, 6)


async def test_session_with_rows_from_a_later_log_gets_its_earlier_activities_after_them(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    athlete, session_id = await _old_session(db_session_factory, "backfill-later-log")
    await _logged(
        athlete,
        "then dips 3x10",
        _gym({"name": "Dips", "sets": 3, "reps": 10}),
        at=_LATER,
        extends=session_id,
    )

    await backfill_gym_sessions(store=SqlAlchemyGymBackfillStore(db_session_factory))

    assert [
        (position, name)
        for position, name, *_ in await _entries_of_rows(db_session_factory, session_id)
    ] == [(1, "Dips"), (2, "Squat"), (3, "Pull-ups")]


async def test_a_payload_changed_since_it_was_read_is_not_written_from_the_stale_copy(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    """An undo between `candidates()` and `write()` rewrote the payload: the next run reads it."""
    athlete, session_id = await _old_session(db_session_factory, "backfill-stale")
    store = SqlAlchemyGymBackfillStore(db_session_factory)
    [candidate] = [c for c in await store.candidates() if c.session_id == session_id]
    async with db_session_factory() as db:
        await db.execute(
            text("UPDATE training_sessions SET payload = CAST(:p AS jsonb) WHERE id = :id"),
            {
                "p": json.dumps({"sport": "gym", "activities": [{"name": "Squat", "sets": 1}]}),
                "id": session_id,
            },
        )
        await db.commit()

    await store.write(candidate, entries_of(GymPayload.model_validate(candidate.payload)))

    assert await _gym_row_count(db_session_factory, athlete.user_id) == (0, 0)
    report = await backfill_gym_sessions(store=store)
    assert (report.sessions, report.entries) == (1, 1)


async def test_a_candidate_backfilled_in_the_meantime_is_written_once(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    """Two runs that both read the candidate: the second finds the rows under the row lock."""
    athlete, session_id = await _old_session(db_session_factory, "backfill-race")
    store = SqlAlchemyGymBackfillStore(db_session_factory)
    [candidate] = [c for c in await store.candidates() if c.session_id == session_id]
    await backfill_gym_sessions(store=store)

    await store.write(candidate, entries_of(GymPayload.model_validate(candidate.payload)))

    assert await _gym_row_count(db_session_factory, athlete.user_id) == (2, 6)
