"""`ExerciseProgress` (PRD 0011 M2g, B37, B40, ticket #517) with fakes: the chart's points and the
table's rows, the typed-only path, and every way the answer is `None`."""

from collections.abc import Sequence
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

from ai_trainer.application.exercise_progress import ExerciseProgress
from ai_trainer.application.session_history import GymSessionHistory
from ai_trainer.application.training_history import MAX_ENTRIES, ExerciseTrainingHistory
from ai_trainer.domain.exercises import (
    Exercise as CatalogueExercise,
)
from ai_trainer.domain.exercises import (
    ExerciseEntry,
    ExerciseLabel,
    ExerciseMeasure,
    LoggedName,
    LoggedSet,
)
from ai_trainer.domain.history import StoredGymSession
from ai_trainer.domain.sports.gym.payload import Exercise, GymSet
from ai_trainer.domain.training import SessionSpan
from tests.mcp_support import EmptyExercises

_USER = uuid4()
_SQUAT = uuid4()


class _Reader:
    def __init__(self, *sessions: StoredGymSession) -> None:
        self._sessions = sessions

    async def list_sessions(self, user_id: UUID) -> Sequence[StoredGymSession]:
        return self._sessions


class _Exercises(EmptyExercises):
    def __init__(self, *, known: bool = True) -> None:
        self._known = known

    async def get(self, user_id: UUID, exercise_id: UUID) -> CatalogueExercise | None:
        if not self._known:
            return None
        return CatalogueExercise(
            id=exercise_id,
            sport_id="gym",
            owner_user_id=None,
            name="Back Squat",
            default_measure=ExerciseMeasure.LOAD_REPS,
        )


class _History:
    def __init__(self, entries: Sequence[ExerciseEntry], *, over: int = 0) -> None:
        self._entries = entries
        self._over = over
        self.asked: list[tuple[str, int]] = []

    async def entries(
        self, user_id: UUID, exercise_id: UUID, *, limit: int
    ) -> Sequence[ExerciseEntry]:
        self.asked.append(("linked", limit))
        return [*self._entries, *list(self._entries[:1]) * self._over]

    async def typed_entries(
        self, user_id: UUID, typed_name: str, *, limit: int
    ) -> Sequence[ExerciseEntry]:
        self.asked.append((f"typed:{typed_name}", limit))
        return self._entries

    async def logged_names(
        self, user_id: UUID, name: str, *, near: float, limit: int
    ) -> Sequence[LoggedName]:
        return []


class _Bands:
    async def ranked(self, user_id: UUID) -> list[str]:
        return ["red", "black"]

    async def place(self, user_id: UUID, label: str, rank: int) -> None:
        return None

    async def next_unplaced(self, user_id: UUID) -> str | None:
        return None


def _entry(
    day: date, *sets: LoggedSet, measure: ExerciseMeasure = ExerciseMeasure.LOAD_REPS
) -> ExerciseEntry:
    return ExerciseEntry(
        session_id=uuid4(),
        occurred_on=day,
        name_as_typed="squat",
        measure=measure,
        per_side=False,
        sets=sets,
    )


def _session(exercise_id: UUID | None, *, label: str | None = None) -> StoredGymSession:
    at = datetime(2026, 11, 3, 17, 0, tzinfo=UTC)
    return StoredGymSession(
        span=SessionSpan(
            id=uuid4(), occurred_on=date(2026, 11, 3), first_logged_at=at, last_logged_at=at
        ),
        exercises=(
            Exercise(
                exercise_id=exercise_id,
                name_as_typed="squat",
                measure="load_reps",
                sets=[GymSet(reps=5)],
            ),
        ),
        labels={"squat": ExerciseLabel(name=label)} if label else {},
    )


def _progress(
    session: StoredGymSession, history: _History, *, known: bool = True
) -> ExerciseProgress:
    return ExerciseProgress(
        GymSessionHistory(_Reader(session)),
        ExerciseTrainingHistory(_Exercises(known=known), history),
        _Bands(),
    )


def _load(day: date, kg: float) -> ExerciseEntry:
    return _entry(day, LoggedSet(set_no=1, reps=5, load_kg=kg, load_original=f"{kg:g} kg"))


async def test_points_run_oldest_first_and_rows_newest_first() -> None:
    session = _session(_SQUAT, label="Back Squat")
    history = _History([_load(date(2026, 11, 3), 7.0), _load(date(2026, 10, 27), 6.0)])

    view = await _progress(session, history).view(_USER, "UTC", session.span.id, 0)

    assert view is not None
    assert view.title == "Back Squat"
    assert [(p.day.isoformat(), p.value) for p in view.points] == [
        ("2026-10-27", 6.0),
        ("2026-11-03", 7.0),
    ]
    assert [row.best for row in view.rows] == ["5 @ 7 kg", "5 @ 6 kg"]
    assert view.rows[0].exercise.sets[0].load is not None
    assert history.asked == [("linked", MAX_ENTRIES + 1)]


async def test_typed_only_entry_reads_its_typed_history() -> None:
    session = _session(None)
    history = _History([_load(date(2026, 11, 3), 7.0)])

    view = await _progress(session, history).view(_USER, "UTC", session.span.id, 0)

    assert view is not None
    assert view.title == "squat"
    assert history.asked == [("typed:squat", MAX_ENTRIES + 1)]


async def test_no_entries_is_an_empty_view() -> None:
    session = _session(None)

    view = await _progress(session, _History([])).view(_USER, "UTC", session.span.id, 0)

    assert view is not None
    assert view.points == ()
    assert view.rows == ()


async def test_entries_without_a_measured_value_are_left_out() -> None:
    session = _session(_SQUAT)
    history = _History([_entry(date(2026, 11, 3), LoggedSet(set_no=1))])

    view = await _progress(session, history).view(_USER, "UTC", session.span.id, 0)

    assert view is not None
    assert view.points == ()
    assert view.rows == ()


async def test_a_cut_history_says_so() -> None:
    session = _session(_SQUAT)
    history = _History([_load(date(2026, 11, 3), 7.0)] * MAX_ENTRIES, over=1)

    view = await _progress(session, history).view(_USER, "UTC", session.span.id, 0)

    assert view is not None
    assert view.truncated is True
    assert len(view.rows) == MAX_ENTRIES


async def test_band_entry_plots_the_place_in_the_athletes_order() -> None:
    session = _session(_SQUAT)
    band = _entry(
        date(2026, 11, 3),
        LoggedSet(set_no=1, reps=10, band_label="black"),
        measure=ExerciseMeasure.BAND_REPS,
    )

    view = await _progress(session, _History([band])).view(_USER, "UTC", session.span.id, 0)

    assert view is not None
    assert [p.value for p in view.points] == [2.0]


async def test_unknown_session_or_index_or_exercise_is_none() -> None:
    session = _session(_SQUAT)
    history = _History([_load(date(2026, 11, 3), 7.0)])
    progress = _progress(session, history)

    assert await progress.view(_USER, "UTC", uuid4(), 0) is None
    assert await progress.view(_USER, "UTC", session.span.id, 1) is None
    assert await progress.view(_USER, "UTC", session.span.id, -1) is None
    assert (
        await _progress(session, history, known=False).view(_USER, "UTC", session.span.id, 0)
        is None
    )


async def test_entries_with_reps_but_no_kg_keep_a_row_and_are_not_plotted() -> None:
    session = _session(_SQUAT)
    history = _History(
        [
            _load(date(2026, 11, 3), 7.0),
            _entry(date(2026, 10, 27), LoggedSet(set_no=1, reps=12)),
        ]
    )

    view = await _progress(session, history).view(_USER, "UTC", session.span.id, 0)

    assert view is not None
    assert [r.best for r in view.rows] == ["5 @ 7 kg", "12 reps"]
    assert [p.day for p in view.points] == [date(2026, 11, 3)]


async def test_only_unplotted_entries_give_rows_and_no_points() -> None:
    session = _session(_SQUAT)
    history = _History([_entry(date(2026, 11, 3), LoggedSet(set_no=1, reps=12))])

    view = await _progress(session, history).view(_USER, "UTC", session.span.id, 0)

    assert view is not None
    assert len(view.rows) == 1
    assert view.points == ()


async def test_spans_years_only_across_calendar_years() -> None:
    session = _session(_SQUAT)
    same = _History([_load(date(2026, 11, 3), 7.0), _load(date(2026, 1, 3), 6.0)])
    across = _History([_load(date(2026, 1, 3), 7.0), _load(date(2025, 12, 30), 6.0)])

    a = await _progress(session, same).view(_USER, "UTC", session.span.id, 0)
    b = await _progress(session, across).view(_USER, "UTC", session.span.id, 0)

    assert a is not None
    assert b is not None
    assert a.spans_years is False
    assert b.spans_years is True
