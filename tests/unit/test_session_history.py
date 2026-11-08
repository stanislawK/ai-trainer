"""The History read model (ticket #515): newest first, the time of day in the athlete's
timezone, and a UTC fallback for an unknown zone. The athlete's edit of a saved session (ticket
#516): a rejected value names its field, an unreadable session is not editable, and neither
writes anything."""

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import JsonValue

from ai_trainer.application.session_history import GymSessionEdit, GymSessionHistory
from ai_trainer.domain.history import (
    SessionChangedError,
    SessionEditError,
    SessionNotEditableError,
    StoredGymSession,
)
from ai_trainer.domain.sports.gym.payload import Exercise
from ai_trainer.domain.training import SessionNotFoundError, SessionSpan


def _stored(day: date, first: datetime, exercises: tuple[Exercise, ...] = ()) -> StoredGymSession:
    return StoredGymSession(
        span=SessionSpan(id=uuid4(), occurred_on=day, first_logged_at=first, last_logged_at=first),
        updated_at=first,
        exercises=exercises,
        labels={},
    )


class FakeReader:
    def __init__(self, *sessions: StoredGymSession) -> None:
        self._sessions = sessions

    async def list_sessions(self, user_id: UUID) -> Sequence[StoredGymSession]:
        return self._sessions

    async def get_session(self, user_id: UUID, session_id: UUID) -> StoredGymSession | None:
        return next((s for s in self._sessions if s.span.id == session_id), None)


class FakeEditor:
    def __init__(self) -> None:
        self.saved: list[tuple[UUID, str, Mapping[str, JsonValue]]] = []
        self.versions: list[datetime] = []

    async def replace_activities(
        self,
        user_id: UUID,
        session_id: UUID,
        *,
        sport_id: str,
        payload: Mapping[str, JsonValue],
        expected_updated_at: datetime,
    ) -> None:
        self.saved.append((session_id, sport_id, payload))
        self.versions.append(expected_updated_at)


async def test_orders_newest_first_and_converts_to_user_timezone() -> None:
    old = _stored(date(2026, 10, 1), datetime(2026, 10, 1, 16, 30, tzinfo=UTC))
    morning = _stored(date(2026, 11, 3), datetime(2026, 11, 3, 6, 15, tzinfo=UTC))
    evening = _stored(date(2026, 11, 3), datetime(2026, 11, 3, 17, 40, tzinfo=UTC))
    history = GymSessionHistory(FakeReader(old, evening, morning))

    sessions = await history.list(uuid4(), "Europe/Warsaw")

    assert [s.id for s in sessions] == [evening.span.id, morning.span.id, old.span.id]
    assert [s.time.isoformat() for s in sessions] == ["18:40:00", "07:15:00", "18:30:00"]
    assert sessions[0].day == date(2026, 11, 3)


async def test_get_reads_one_session_in_user_timezone_and_none_for_unknown() -> None:
    stored = _stored(date(2026, 11, 3), datetime(2026, 11, 3, 17, 40, tzinfo=UTC))
    history = GymSessionHistory(FakeReader(stored))

    found = await history.get(uuid4(), stored.span.id, "Europe/Warsaw")

    assert found is not None
    assert (found.id, found.time.isoformat()) == (stored.span.id, "18:40:00")
    assert await history.get(uuid4(), uuid4(), "Europe/Warsaw") is None


async def test_unknown_timezone_falls_back_to_utc() -> None:
    stored = _stored(date(2026, 11, 3), datetime(2026, 11, 3, 17, 40, tzinfo=UTC))
    history = GymSessionHistory(FakeReader(stored))

    sessions = await history.list(uuid4(), "Not/AZone")

    assert sessions[0].time.isoformat() == "17:40:00"


_BENCH = Exercise.model_validate(
    {
        "name_as_typed": "bench",
        "measure": "reps",
        "sets": [{"reps": 8, "load": {"original": "60 kg", "value": 60, "unit": "kg", "kg": 60}}],
    }
)
_FIRST = datetime(2026, 11, 5, 17, 40, tzinfo=UTC)
_TOKEN = _FIRST.isoformat()


async def test_edit_maps_validation_error_to_fields() -> None:
    stored = _stored(date(2026, 11, 5), _FIRST, (_BENCH,))
    editor = FakeEditor()

    with pytest.raises(SessionEditError) as raised:
        await GymSessionEdit(FakeReader(stored), editor).save(
            uuid4(), stored.span.id, {"activities": [{"sets": [{"reps": "0"}]}]}, version=_TOKEN
        )

    assert raised.value.fields == frozenset({"activities.0.sets.0.reps"})
    assert editor.saved == []


async def test_stored_row_invalid_is_not_editable() -> None:
    no_sets = Exercise.model_construct(name_as_typed="bench", measure="reps", sets=[])
    stored = _stored(date(2026, 11, 5), _FIRST, (no_sets,))
    editor = FakeEditor()

    with pytest.raises(SessionNotEditableError):
        await GymSessionEdit(FakeReader(stored), editor).save(
            uuid4(), stored.span.id, {}, version=_TOKEN
        )

    assert editor.saved == []


async def test_session_without_entries_is_not_editable() -> None:
    stored = _stored(date(2026, 11, 5), _FIRST)
    editor = FakeEditor()

    with pytest.raises(SessionNotEditableError):
        await GymSessionEdit(FakeReader(stored), editor).save(
            uuid4(), stored.span.id, {"activities": [{"sets": [{"reps": "8"}]}]}, version=_TOKEN
        )

    assert editor.saved == []


async def test_unknown_session_is_not_found() -> None:
    editor = FakeEditor()

    with pytest.raises(SessionNotFoundError):
        await GymSessionEdit(FakeReader(), editor).save(uuid4(), uuid4(), {}, version=_TOKEN)

    assert editor.saved == []


async def test_edit_ignores_a_posted_name_and_saves_the_gym_payload() -> None:
    stored = _stored(date(2026, 11, 5), _FIRST, (_BENCH,))
    editor = FakeEditor()

    await GymSessionEdit(FakeReader(stored), editor).save(
        uuid4(),
        stored.span.id,
        {"activities": [{"name": "squat", "sets": [{"src": "0", "reps": "8", "load": "65"}]}]},
        version=_TOKEN,
    )

    [(session_id, sport_id, payload)] = editor.saved
    assert (session_id, sport_id) == (stored.span.id, "gym")
    activities = payload["activities"]
    assert isinstance(activities, list)
    exercise = activities[0]
    assert isinstance(exercise, dict)
    assert exercise["name_as_typed"] == "bench"
    sets = exercise["sets"]
    assert isinstance(sets, list)
    first = sets[0]
    assert isinstance(first, dict)
    load = first["load"]
    assert isinstance(load, dict)
    assert load["kg"] == 65


async def test_edit_without_activities_saves_the_session_as_it_is() -> None:
    stored = _stored(date(2026, 11, 5), _FIRST, (_BENCH,))
    editor = FakeEditor()

    await GymSessionEdit(FakeReader(stored), editor).save(
        uuid4(), stored.span.id, {}, version=_TOKEN
    )

    [(_, _, payload)] = editor.saved
    assert payload == {"sport": "gym", "activities": [_BENCH.model_dump(mode="json")]}


async def test_edit_passes_the_rendered_version_to_the_editor() -> None:
    stored = _stored(date(2026, 11, 5), _FIRST, (_BENCH,))
    editor = FakeEditor()

    await GymSessionEdit(FakeReader(stored), editor).save(
        uuid4(), stored.span.id, {}, version=_TOKEN
    )

    assert editor.versions == [_FIRST]


@pytest.mark.parametrize(
    "version",
    [None, "", "not a time", "2026-11-05T17:40:00"],
    ids=["missing", "blank", "unparseable", "naive"],
)
async def test_missing_or_unreadable_version_is_changed(version: str | None) -> None:
    stored = _stored(date(2026, 11, 5), _FIRST, (_BENCH,))
    editor = FakeEditor()

    with pytest.raises(SessionChangedError):
        await GymSessionEdit(FakeReader(stored), editor).save(
            uuid4(), stored.span.id, {}, version=version
        )

    assert editor.saved == []
