"""The athlete's edit of a saved session through `SqlAlchemySessionEditor` and each sport's
`ActivityStore.replace` (ticket #516, ADR-0032): a payload sport keeps the edited payload, a gym
edit keeps each entry's source and exercise, a cleared value stays null, an edit of rows that
changed meanwhile or of a stale version is refused, and another athlete's session is not found."""

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.training_session_editor import SqlAlchemySessionEditor
from ai_trainer.domain.history import SessionChangedError
from ai_trainer.domain.training import SessionNotFoundError
from tests.activity_support import STORES
from tests.chat_support import FROZEN_NOW, FixedClock
from tests.integration.test_gym_activity_store import _gym, _id, _lift, _logged, _sets
from tests.integration.test_session_continuity_repository import (
    _START,
    _athlete,
    _climb,
    _draft,
    _session,
)


def _editor(factory: Callable[[], AsyncSession]) -> SqlAlchemySessionEditor:
    return SqlAlchemySessionEditor(factory, stores=STORES, clock=FixedClock())


def _bench(*sets: dict[str, Any]) -> dict[str, Any]:
    return {
        "sport": "gym",
        "activities": [{"name_as_typed": "bench", "measure": "reps", "sets": list(sets)}],
    }


def _kg(reps: int | None, kg: int | None) -> dict[str, Any]:
    load = None if kg is None else {"original": f"{kg} kg", "value": kg, "unit": "kg", "kg": kg}
    return {"reps": reps, "load": load}


async def _ids(factory: Callable[[], AsyncSession], session_id: UUID) -> list[Any]:
    async with factory() as db:
        rows = await db.execute(
            text(
                "SELECT id, source_id, exercise_id, name_as_typed FROM gym_exercise_entries"
                " WHERE session_id = :id ORDER BY position"
            ),
            {"id": session_id},
        )
        return [tuple(row) for row in rows.all()]


async def _version(factory: Callable[[], AsyncSession], session_id: UUID) -> datetime:
    stored = await _session(factory, session_id)
    assert isinstance(stored.updated_at, datetime)
    return stored.updated_at


async def test_climbing_edit_keeps_the_edited_payload(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    athlete = await _athlete(db_session_factory, "edit-climbing")
    logged = await athlete.confirmed(
        await athlete.draft("lead 6a", _START, _draft(_climb("6a")), sport_id="climbing")
    )
    session_id = _id(logged.session_id)

    await _editor(db_session_factory).replace_activities(
        athlete.user_id,
        session_id,
        sport_id="climbing",
        payload=_climb("6b"),
        expected_updated_at=await _version(db_session_factory, session_id),
    )

    stored = await _session(db_session_factory, session_id)
    assert stored.payload == _climb("6b")
    assert stored.updated_at == FROZEN_NOW


async def test_gym_edit_keeps_source_and_exercise_and_a_cleared_value_stays_null(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    athlete = await _athlete(db_session_factory, "edit-gym-replace")
    logged = await _logged(athlete, "bench 2x8 60", _gym(_lift("bench", 2, 8, "60 kg", 60)))
    session_id = _id(logged.session_id)
    before = await _ids(db_session_factory, session_id)

    await _editor(db_session_factory).replace_activities(
        athlete.user_id,
        session_id,
        sport_id="gym",
        payload=_bench(_kg(8, 65), _kg(None, 60), _kg(6, None)),
        expected_updated_at=await _version(db_session_factory, session_id),
    )

    assert await _ids(db_session_factory, session_id) == before
    assert await _sets(db_session_factory, session_id) == [
        ("bench", 1, 8, 65, "65 kg", None, None, None, None),
        ("bench", 2, None, 60, "60 kg", None, None, None, None),
        ("bench", 3, 6, None, None, None, None, None, None),
    ]
    assert (await _session(db_session_factory, session_id)).payload == {}


async def test_gym_edit_of_changed_rows_is_refused_and_writes_nothing(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    athlete = await _athlete(db_session_factory, "edit-gym-changed")
    logged = await _logged(athlete, "bench 2x8 60", _gym(_lift("bench", 2, 8, "60 kg", 60)))
    session_id = _id(logged.session_id)
    before = await _sets(db_session_factory, session_id)
    two = _bench(_kg(8, 65))
    two["activities"] = two["activities"] * 2

    with pytest.raises(SessionChangedError):
        await _editor(db_session_factory).replace_activities(
            athlete.user_id,
            session_id,
            sport_id="gym",
            payload=two,
            expected_updated_at=await _version(db_session_factory, session_id),
        )

    assert await _sets(db_session_factory, session_id) == before


async def test_gym_edit_of_a_stale_version_is_refused_and_writes_nothing(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    athlete = await _athlete(db_session_factory, "edit-gym-stale")
    logged = await _logged(athlete, "bench 2x8 60", _gym(_lift("bench", 2, 8, "60 kg", 60)))
    session_id = _id(logged.session_id)
    rendered = await _version(db_session_factory, session_id)
    before = await _sets(db_session_factory, session_id)

    with pytest.raises(SessionChangedError):
        await _editor(db_session_factory).replace_activities(
            athlete.user_id,
            session_id,
            sport_id="gym",
            payload=_bench(_kg(8, 65), _kg(8, 65)),
            expected_updated_at=rendered - timedelta(microseconds=1),
        )

    assert await _sets(db_session_factory, session_id) == before
    assert await _version(db_session_factory, session_id) == rendered


async def test_another_athletes_session_is_not_found_and_unchanged(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    owner = await _athlete(db_session_factory, "edit-owner")
    stranger = await _athlete(db_session_factory, "edit-stranger")
    logged = await _logged(owner, "bench 2x8 60", _gym(_lift("bench", 2, 8, "60 kg", 60)))
    session_id = _id(logged.session_id)
    before = await _sets(db_session_factory, session_id)

    with pytest.raises(SessionNotFoundError):
        await _editor(db_session_factory).replace_activities(
            stranger.user_id,
            session_id,
            sport_id="gym",
            payload=_bench(_kg(8, 65)),
            expected_updated_at=await _version(db_session_factory, session_id),
        )

    assert await _sets(db_session_factory, session_id) == before
