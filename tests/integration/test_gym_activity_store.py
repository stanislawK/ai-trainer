"""A confirmed gym log in the gym's own tables (ADR-0032, ticket #376): its entries and sets
are written in the confirm's transaction with the source row they came from, the session's
payload is `{}`, an added log appends after the session's entries and its undo takes only its
own, and nothing reaches another user's rows. Climbing and cycling keep their payload."""

import json
from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.gym_backfill import SqlAlchemyGymBackfillStore
from ai_trainer.adapters.training_session_repository import SqlAlchemyTrainingSessionRepository
from ai_trainer.application.gym_backfill import backfill_gym_sessions
from ai_trainer.domain.sports.base import extend_payload, withdraw_payload
from ai_trainer.domain.sports.climbing.payload import ClimbingPayload
from ai_trainer.domain.sports.cycling.payload import CyclingPayload
from ai_trainer.domain.sports.gym.extraction import GymExtraction, recent_exercises
from ai_trainer.domain.training import DraftNotFoundError, SessionDraft, StoredDraft
from tests.activity_support import STORES
from tests.integration.test_session_continuity_repository import (
    _START,
    _Athlete,
    _athlete,
    _climb,
    _draft,
    _session,
    _session_count,
)

_LATER = _START + timedelta(minutes=5)


def _id(session_id: UUID | None) -> UUID:
    """A confirmed draft's session."""
    assert session_id is not None
    return session_id


def _gym(*activities: dict[str, Any]) -> SessionDraft:
    """A gym draft as its extraction writes it: code converts the load to kg."""
    payload = GymExtraction.model_validate(
        {"occurred_on": "2026-10-02", "date_source": "assumed_today", "activities": activities}
    ).to_payload()
    return _draft(payload.model_dump(mode="json"))


def _lift(name: str, sets: int, reps: int, original: str, value: float) -> dict[str, Any]:
    return {
        "name": name,
        "sets": sets,
        "reps": reps,
        "load": {"original": original, "value": value, "unit": "kg"},
    }


async def _logged(
    athlete: _Athlete,
    log: str,
    draft: SessionDraft,
    *,
    at: Any = _START,
    extends: UUID | None = None,
) -> StoredDraft:
    return await athlete.confirmed(
        await athlete.draft(log, at, draft, sport_id="gym", extends=extends)
    )


async def _entries(factory: Callable[[], AsyncSession], session_id: UUID | None) -> list[Any]:
    async with factory() as db:
        rows = await db.execute(
            text(
                "SELECT e.position, e.name_as_typed, e.measure, e.rir, e.per_side, e.implements,"
                " e.exercise_id, e.note, src.raw_text"
                " FROM gym_exercise_entries e"
                " JOIN training_session_sources src ON src.id = e.source_id"
                " WHERE e.session_id = :id ORDER BY e.position"
            ),
            {"id": session_id},
        )
        return [tuple(row) for row in rows.all()]


async def _sets(factory: Callable[[], AsyncSession], session_id: UUID | None) -> list[Any]:
    async with factory() as db:
        rows = await db.execute(
            text(
                "SELECT e.name_as_typed, s.set_no, s.reps, s.load_kg, s.load_original,"
                " s.duration_s, s.distance_m, s.band_id, s.band_label"
                " FROM gym_sets s JOIN gym_exercise_entries e ON e.id = s.entry_id"
                " WHERE e.session_id = :id ORDER BY e.position, s.set_no"
            ),
            {"id": session_id},
        )
        return [tuple(row) for row in rows.all()]


async def _gym_row_count(factory: Callable[[], AsyncSession], user_id: UUID) -> tuple[int, int]:
    async with factory() as db:
        entries = await db.scalar(
            text("SELECT count(*) FROM gym_exercise_entries WHERE user_id = :id"), {"id": user_id}
        )
        sets = await db.scalar(
            text("SELECT count(*) FROM gym_sets WHERE user_id = :id"), {"id": user_id}
        )
        return entries or 0, sets or 0


# --- AC: three sets of 8 at 45 kg, payload `{}` --------------------------------------------


async def test_confirming_bench_press_stores_three_sets_of_eight_at_45_kg_and_an_empty_payload(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    athlete = await _athlete(db_session_factory, "gym-bench")

    confirmed = await _logged(
        athlete, "Bench press 45kg 8 x3", _gym(_lift("Bench press", 3, 8, "45kg", 45))
    )

    assert (await _session(db_session_factory, confirmed.session_id)).payload == {}
    assert await _sets(db_session_factory, confirmed.session_id) == [
        ("Bench press", n, 8, Decimal("45.00"), "45kg", None, None, None, None) for n in (1, 2, 3)
    ]
    assert await _entries(db_session_factory, confirmed.session_id) == [
        (1, "Bench press", "load_reps", None, False, None, None, None, "Bench press 45kg 8 x3")
    ]


# --- AC: a value the message didn't give is NULL -------------------------------------------


async def test_a_gym_log_without_load_reps_or_rir_stores_null(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    athlete = await _athlete(db_session_factory, "gym-null")

    confirmed = await _logged(athlete, "4 sets of pull-ups", _gym({"name": "pull-ups", "sets": 4}))

    assert await _sets(db_session_factory, confirmed.session_id) == [
        ("pull-ups", n, None, None, None, None, None, None, None) for n in (1, 2, 3, 4)
    ]
    assert await _entries(db_session_factory, confirmed.session_id) == [
        (1, "pull-ups", "reps", None, False, None, None, None, "4 sets of pull-ups")
    ]


# --- AC: an added log appends after the session's entries ----------------------------------


async def test_a_log_added_to_a_gym_session_appends_its_entries_after_the_existing_ones(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    athlete = await _athlete(db_session_factory, "gym-append")
    first = await _logged(
        athlete,
        "Squat 100kg 5x5, RDL 80kg 3x8",
        _gym(_lift("Squat", 5, 5, "100kg", 100), _lift("RDL", 3, 8, "80kg", 80)),
    )

    added = await _logged(
        athlete,
        "then dips 3x10",
        _gym({"name": "Dips", "sets": 3, "reps": 10}),
        at=_LATER,
        extends=first.session_id,
    )

    assert added.session_id == first.session_id
    assert await _session_count(db_session_factory, athlete.user_id) == 1
    assert (await _session(db_session_factory, first.session_id)).payload == {}
    assert [
        (position, name, log)
        for position, name, *_, log in await _entries(db_session_factory, first.session_id)
    ] == [
        (1, "Squat", "Squat 100kg 5x5, RDL 80kg 3x8"),
        (2, "RDL", "Squat 100kg 5x5, RDL 80kg 3x8"),
        (3, "Dips", "then dips 3x10"),
    ]


async def test_a_log_added_to_a_session_from_before_the_tables_keeps_its_payload(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    """Owner-chosen at #376's plan gate (D4): the old v1 payload stays as it is (ADR-0022
    invariant 4) and the added log's rows start at 1; #377 backfills the earlier activities."""
    athlete = await _athlete(db_session_factory, "gym-pre-table")
    first = await _logged(athlete, "Squat 100kg 5x5", _gym(_lift("Squat", 5, 5, "100kg", 100)))
    v1 = await _from_before_the_tables(db_session_factory, first)

    await _logged(
        athlete,
        "then dips 3x10",
        _gym({"name": "Dips", "sets": 3, "reps": 10}),
        at=_LATER,
        extends=first.session_id,
    )

    assert (await _session(db_session_factory, first.session_id)).payload == v1
    assert [
        (position, name)
        for position, name, *_ in await _entries(db_session_factory, first.session_id)
    ] == [(1, "Dips")]


async def test_undoing_a_log_from_before_the_tables_takes_it_out_of_the_old_payload(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    """Found by #376's code review: with no rows to delete, the log's exercises leave the v1
    payload as before #376, so #377's backfill doesn't bring them back."""
    athlete = await _athlete(db_session_factory, "gym-pre-table-undo")
    first = await _logged(athlete, "Squat 100kg 5x5", _gym(_lift("Squat", 5, 5, "100kg", 100)))
    added = await _logged(
        athlete,
        "then dips 3x10",
        _gym({"name": "Dips", "sets": 3, "reps": 10}),
        at=_LATER,
        extends=first.session_id,
    )
    await _from_before_the_tables(db_session_factory, first, added)

    await athlete.drafts.undo(athlete.user_id, added.id, undone_at=_LATER)

    assert (await _session(db_session_factory, first.session_id)).payload == {
        "sport": "gym",
        "activities": first.draft.payload["activities"],
    }


async def test_undoing_the_last_old_log_of_a_session_with_rows_leaves_an_empty_payload(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    athlete = await _athlete(db_session_factory, "gym-pre-table-last")
    first = await _logged(athlete, "Squat 100kg 5x5", _gym(_lift("Squat", 5, 5, "100kg", 100)))
    await _from_before_the_tables(db_session_factory, first)
    await _logged(
        athlete,
        "then dips 3x10",
        _gym({"name": "Dips", "sets": 3, "reps": 10}),
        at=_LATER,
        extends=first.session_id,
    )

    await athlete.drafts.undo(athlete.user_id, first.id, undone_at=_LATER)

    assert (await _session(db_session_factory, first.session_id)).payload == {}
    assert [name for _, name, *_ in await _entries(db_session_factory, first.session_id)] == [
        "Dips"
    ]


def _activities_of(log: StoredDraft) -> list[Any]:
    activities = log.draft.payload["activities"]
    assert isinstance(activities, list)
    return activities


async def _from_before_the_tables(
    factory: Callable[[], AsyncSession], *logs: StoredDraft
) -> dict[str, Any]:
    """The logs' session as confirms before #376 left it: a v1 payload holding their
    activities, and no rows."""
    session_id = logs[0].session_id
    v1 = {
        "sport": "gym",
        "activities": [activity for log in logs for activity in _activities_of(log)],
    }
    async with factory() as db:
        await db.execute(
            text("DELETE FROM gym_exercise_entries WHERE session_id = :id"), {"id": session_id}
        )
        await db.execute(
            text("UPDATE training_sessions SET payload = CAST(:payload AS jsonb) WHERE id = :id"),
            {"payload": json.dumps(v1), "id": session_id},
        )
        await db.commit()
    return v1


# --- AC: undo takes only its own entries; the last undo removes the session ----------------


async def test_undoing_an_added_gym_log_removes_only_its_entries(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    athlete = await _athlete(db_session_factory, "gym-undo-added")
    first = await _logged(athlete, "Squat 100kg 5x5", _gym(_lift("Squat", 5, 5, "100kg", 100)))
    # The same exercise again: only this log's entry goes, by its source, not by its name.
    added = await _logged(
        athlete,
        "and squat 100kg 5x5 again",
        _gym(_lift("Squat", 5, 5, "100kg", 100)),
        at=_LATER,
        extends=first.session_id,
    )

    undone = await athlete.drafts.undo(athlete.user_id, added.id, undone_at=_LATER)

    assert undone.session_id is None
    assert [
        (position, name, log)
        for position, name, *_, log in await _entries(db_session_factory, first.session_id)
    ] == [(1, "Squat", "Squat 100kg 5x5")]
    assert await _gym_row_count(db_session_factory, athlete.user_id) == (1, 5)
    assert (await _session(db_session_factory, first.session_id)).payload == {}


async def test_undoing_the_last_gym_log_removes_the_session_and_its_rows(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    athlete = await _athlete(db_session_factory, "gym-undo-last")
    first = await _logged(athlete, "Squat 100kg 5x5", _gym(_lift("Squat", 5, 5, "100kg", 100)))
    added = await _logged(
        athlete,
        "then dips 3x10",
        _gym({"name": "Dips", "sets": 3, "reps": 10}),
        at=_LATER,
        extends=first.session_id,
    )
    await athlete.drafts.undo(athlete.user_id, added.id, undone_at=_LATER)

    await athlete.drafts.undo(athlete.user_id, first.id, undone_at=_LATER)

    assert await _session(db_session_factory, first.session_id) is None
    assert await _gym_row_count(db_session_factory, athlete.user_id) == (0, 0)


async def test_undo_of_a_log_added_before_the_backfill_removes_only_its_entries_latest_first(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    """ADR-0032: a backfilled row has no source, so the undo takes the log's activities out by
    name as typed, the latest match first, as `withdraw_payload` does; the other logs' stay."""
    athlete = await _athlete(db_session_factory, "gym-backfilled-undo")
    first = await _logged(
        athlete,
        "Squat 100kg 5x5, dips 3x10",
        _gym(_lift("Squat", 5, 5, "100kg", 100), {"name": "Dips", "sets": 3, "reps": 10}),
    )
    added = await _logged(
        athlete,
        "and dips 3x10 again",
        _gym({"name": "Dips", "sets": 3, "reps": 10}),
        at=_LATER,
        extends=first.session_id,
    )
    await _from_before_the_tables(db_session_factory, first, added)
    await backfill_gym_sessions(store=SqlAlchemyGymBackfillStore(db_session_factory))

    await athlete.drafts.undo(athlete.user_id, added.id, undone_at=_LATER)

    async with db_session_factory() as db:
        rows = await db.execute(
            text(
                "SELECT position, name_as_typed, source_id FROM gym_exercise_entries"
                " WHERE session_id = :id ORDER BY position"
            ),
            {"id": first.session_id},
        )
        assert [tuple(row) for row in rows.all()] == [(1, "Squat", None), (2, "Dips", None)]
    assert await _gym_row_count(db_session_factory, athlete.user_id) == (2, 8)
    assert [
        activity["name"]
        for activity in (await _session(db_session_factory, first.session_id)).payload["activities"]
    ] == ["Squat", "Dips"]


# --- AC: climbing and cycling as before ----------------------------------------------------


_PAYLOAD_TYPES: dict[str, type[BaseModel]] = {
    "climbing": ClimbingPayload,
    "cycling": CyclingPayload,
}


def _ride(km: int) -> dict[str, Any]:
    distance = {"original": f"{km} km", "value": km, "unit": "km", "km": km}
    return {"sport": "cycling", "activities": [{"distance": distance}]}


@pytest.mark.parametrize(
    ("sport_id", "first", "second"),
    [("climbing", _climb("6b"), _climb("6c")), ("cycling", _ride(40), _ride(12))],
    ids=["climbing", "cycling"],
)
async def test_climbing_and_cycling_still_keep_their_activities_in_the_payload(
    db_session_factory: Callable[[], AsyncSession],
    sport_id: str,
    first: dict[str, Any],
    second: dict[str, Any],
) -> None:
    athlete = await _athlete(db_session_factory, f"payload-{sport_id}")
    logged = await athlete.confirmed(
        await athlete.draft("first", _START, _draft(first), sport_id=sport_id)
    )
    alone = (await _session(db_session_factory, logged.session_id)).payload
    added = await athlete.confirmed(
        await athlete.draft(
            "second", _LATER, _draft(second), sport_id=sport_id, extends=logged.session_id
        )
    )
    both = (await _session(db_session_factory, logged.session_id)).payload

    await athlete.drafts.undo(athlete.user_id, added.id, undone_at=_LATER)
    after_undo = (await _session(db_session_factory, logged.session_id)).payload
    await athlete.drafts.undo(athlete.user_id, logged.id, undone_at=_LATER)

    # As before #376: a new session keeps the draft's payload, an addition and its undo go
    # through the sport's payload model (ticket #165).
    payload_type = _PAYLOAD_TYPES[sport_id]
    assert alone == first
    assert both == extend_payload(payload_type, first, second).model_dump(mode="json")
    assert after_undo == withdraw_payload(payload_type, both, second).model_dump(mode="json")
    assert await _session(db_session_factory, logged.session_id) is None
    assert await _gym_row_count(db_session_factory, athlete.user_id) == (0, 0)


# --- the extraction's recent exercises read the rows ---------------------------------------


async def test_the_extractions_recent_exercises_read_the_rows(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    athlete = await _athlete(db_session_factory, "gym-recent")
    await _logged(
        athlete,
        "Deadlift 315 lb 3x5, pull-ups",
        _gym(
            {
                "name": "Deadlift",
                "sets": 3,
                "reps": 5,
                "load": {"original": "315 lb", "value": 315, "unit": "lb"},
            },
            {"name": "pull-ups", "sets": 3},
        ),
    )
    sessions = SqlAlchemyTrainingSessionRepository(db_session_factory, stores=STORES)

    payloads = await sessions.recent_payloads(athlete.user_id, "gym", 5)

    assert recent_exercises(payloads) == ("Deadlift (lb)", "pull-ups")


# --- AC: another user's rows are unreachable -----------------------------------------------


async def test_recent_gym_exercises_read_only_the_users_rows(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    owner = await _athlete(db_session_factory, "gym-recent-owner")
    stranger = await _athlete(db_session_factory, "gym-recent-stranger")
    theirs = await _logged(owner, "Squat 100kg 5x5", _gym(_lift("Squat", 5, 5, "100kg", 100)))
    sessions = SqlAlchemyTrainingSessionRepository(db_session_factory, stores=STORES)

    assert await sessions.recent_payloads(stranger.user_id, "gym", 5) == []
    async with db_session_factory() as db:
        assert await STORES("gym").recent(db, stranger.user_id, [_id(theirs.session_id)]) == [
            {"activities": []}
        ]


async def test_undoing_another_users_gym_log_changes_none_of_their_rows(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    owner = await _athlete(db_session_factory, "gym-undo-owner")
    stranger = await _athlete(db_session_factory, "gym-undo-stranger")
    theirs = await _logged(owner, "Squat 100kg 5x5", _gym(_lift("Squat", 5, 5, "100kg", 100)))

    with pytest.raises(DraftNotFoundError):
        await stranger.drafts.undo(stranger.user_id, theirs.id, undone_at=_LATER)

    assert await _gym_row_count(db_session_factory, owner.user_id) == (1, 5)


_STRANGERS_ENTRY = text(
    "INSERT INTO gym_exercise_entries"
    " (id, user_id, session_id, source_id, position, name_as_typed, measure)"
    " VALUES (:id, :user_id, :session_id, :source_id, 99, 'Squat', 'reps')"
)
_SET = text(
    "INSERT INTO gym_sets (id, user_id, entry_id, set_no, band_id)"
    " VALUES (:id, :user_id, :entry_id, 9, :band_id)"
)


async def _source_of(factory: Callable[[], AsyncSession], session_id: UUID | None) -> UUID:
    async with factory() as db:
        source_id = await db.scalar(
            text("SELECT id FROM training_session_sources WHERE session_id = :id"),
            {"id": session_id},
        )
        assert isinstance(source_id, UUID)
        return source_id


@pytest.mark.parametrize("reference", ["session", "source", "entry", "band"])
async def test_gym_rows_cannot_reference_another_users_session_source_entry_or_band(
    db_session_factory: Callable[[], AsyncSession], reference: str
) -> None:
    owner = await _athlete(db_session_factory, f"gym-fk-owner-{reference}")
    stranger = await _athlete(db_session_factory, f"gym-fk-stranger-{reference}")
    theirs = await _logged(owner, "Squat 100kg 5x5", _gym(_lift("Squat", 5, 5, "100kg", 100)))
    mine = await _logged(stranger, "Dips 3x10", _gym({"name": "Dips", "sets": 3, "reps": 10}))
    their_source = await _source_of(db_session_factory, theirs.session_id)
    my_source = await _source_of(db_session_factory, mine.session_id)
    async with db_session_factory() as db:
        their_entry = await db.scalar(
            text("SELECT id FROM gym_exercise_entries WHERE session_id = :id"),
            {"id": theirs.session_id},
        )
        their_band = uuid4()
        await db.execute(
            text("INSERT INTO athlete_bands (id, user_id, label) VALUES (:id, :user_id, 'red')"),
            {"id": their_band, "user_id": owner.user_id},
        )
        await db.commit()

        my_entry = await db.scalar(
            text("SELECT id FROM gym_exercise_entries WHERE session_id = :id"),
            {"id": mine.session_id},
        )
    # The stranger's own row, with one reference pointing at the owner's.
    entry = {
        "id": uuid4(),
        "user_id": stranger.user_id,
        "session_id": theirs.session_id if reference == "session" else mine.session_id,
        "source_id": their_source if reference == "source" else my_source,
    }
    gym_set = {
        "id": uuid4(),
        "user_id": stranger.user_id,
        "entry_id": their_entry if reference == "entry" else my_entry,
        "band_id": their_band if reference == "band" else None,
    }
    statement, params = (
        (_STRANGERS_ENTRY, entry) if reference in ("session", "source") else (_SET, gym_set)
    )

    async with db_session_factory() as db:
        with pytest.raises(IntegrityError):
            await db.execute(statement, params)
