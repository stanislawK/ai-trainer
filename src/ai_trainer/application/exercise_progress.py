"""One exercise's progress over time (PRD 0011 M2g, B37, B40, ticket #517): the entries the athlete
has of the exercise a saved session's entry names, through the query behind the `exercise_history`
tool, as a chart's points and a table's rows. Another athlete's session, or an index past the
session's entries, is `None`, which the route turns into a 404 (ADR-0021)."""

from datetime import date
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from ai_trainer.application.ports.band_order import BandOrderPort
from ai_trainer.application.session_history import GymSessionHistory
from ai_trainer.application.training_history import (
    MAX_ENTRIES,
    ExerciseHistoryFailed,
    ExerciseTrainingHistory,
)
from ai_trainer.domain.exercise_progress import best_of
from ai_trainer.domain.exercises import ExerciseEntry, ExerciseMeasure
from ai_trainer.domain.sports.gym.pace import Split, split_of
from ai_trainer.domain.sports.gym.payload import Exercise, GymSet
from ai_trainer.domain.sports.gym.rows import load_of


class ProgressPoint(BaseModel):
    """One day on the chart; `value` is `None` for a band with no place in the athlete's order,
    or a timed distance with no pace, which the chart marks with a dashed line."""

    model_config = ConfigDict(frozen=True)

    day: date
    value: float | None


class ProgressRow(BaseModel):
    """One table row: the day, the entry as the logged card shows it, and its best set, with the
    value the chart plots for it (`None` when it plots none)."""

    model_config = ConfigDict(frozen=True)

    day: date
    exercise: Exercise
    best: str
    value: float | None = None


class ProgressView(BaseModel):
    """What the progress page shows. `points` run oldest first, `rows` newest first. With no
    measured entry both are empty and the page says so, with no numbers (B40)."""

    model_config = ConfigDict(frozen=True)

    session_id: UUID
    session_day: date
    title: str
    measure: ExerciseMeasure
    points: tuple[ProgressPoint, ...]
    rows: tuple[ProgressRow, ...]
    truncated: bool
    # The athlete's ranked bands, lightest first, for a band chart's axis; empty otherwise.
    order: tuple[str, ...] = ()
    # A pace's unit: per 500 m for a rowing machine or ski erg, else per km (#626).
    split: Split = "km"

    @property
    def spans_years(self) -> bool:
        """Whether the rows cover more than one calendar year, so dates need their year."""
        return len({row.day.year for row in self.rows}) > 1


def _read_as(chart: ExerciseMeasure, entry: ExerciseEntry) -> ExerciseMeasure | None:
    """How an entry is read on a chart: a timed distance on either side is read by the chart's
    measure, so km/h never lands on a seconds or metres axis and a time never on a speed one
    (#626). Any other entry keeps its own measure, as #517 reads it."""
    timed = ExerciseMeasure.DISTANCE_TIME
    return chart if timed in (chart, entry.measure) else None


def _payload(entry: ExerciseEntry) -> Exercise:
    """The entry as a payload exercise, so the logged card's line reads it. Not validated: the
    rows were valid when written."""
    return Exercise.model_construct(
        name_as_typed=entry.name_as_typed,
        measure=entry.measure.value,
        per_side=entry.per_side,
        implements=entry.implements,
        rir=entry.rir,
        sets=[
            GymSet.model_construct(
                reps=s.reps,
                load=None
                if s.load_kg is None
                else load_of(s.load_original, Decimal(str(s.load_kg))),
                duration_s=s.duration_s,
                distance_m=s.distance_m,
                band_label=s.band_label,
            )
            for s in entry.sets
        ],
    )


class ExerciseProgress:
    def __init__(
        self,
        sessions: GymSessionHistory,
        exercises: ExerciseTrainingHistory,
        bands: BandOrderPort,
    ) -> None:
        self._sessions = sessions
        self._exercises = exercises
        self._bands = bands

    async def view(
        self, user_id: UUID, timezone: str, session_id: UUID, index: int
    ) -> ProgressView | None:
        """The progress of the exercise at `index` of the user's session, or `None` when the
        session isn't theirs, the index is out of range, or the exercise isn't visible to them."""
        opened = await self._sessions.get(user_id, session_id, timezone)
        if opened is None or not 0 <= index < len(opened.exercises):
            return None
        chosen = opened.exercises[index]
        if chosen.exercise_id is None:
            found = await self._exercises.typed_entries(user_id, chosen.name_as_typed, MAX_ENTRIES)
        else:
            found = await self._exercises.entries(user_id, chosen.exercise_id, MAX_ENTRIES)
        label = opened.labels.get(chosen.name_as_typed)
        title = label.name if label else chosen.name_as_typed
        measure = ExerciseMeasure(chosen.measure)
        if isinstance(found, ExerciseHistoryFailed):
            if found.reason == "not_found":
                return None
            return ProgressView(
                session_id=session_id,
                session_day=opened.day,
                title=title,
                measure=measure,
                points=(),
                rows=(),
                truncated=False,
            )
        order = await self._bands.ranked(user_id)
        title = found.exercise.name if found.exercise else title
        split = split_of(title, chosen.name_as_typed)
        # A timed distance reads every entry by its pace, so a session logged with only a time
        # is a dashed marker, as an unranked band is (#626).
        marked = measure in (ExerciseMeasure.BAND_REPS, ExerciseMeasure.DISTANCE_TIME)
        measured = [
            (entry, best)
            for entry in found.entries
            if (best := best_of(entry, order, measure=_read_as(measure, entry), split=split))
            is not None
        ]  # newest first, as read
        return ProgressView(
            session_id=session_id,
            session_day=opened.day,
            measure=measure,
            title=title,
            points=tuple(
                ProgressPoint(day=entry.occurred_on, value=best.value)
                for entry, best in reversed(measured)
                if best.value is not None or marked
            ),
            rows=tuple(
                ProgressRow(
                    day=entry.occurred_on,
                    exercise=_payload(entry),
                    best=best.text,
                    value=best.value,
                )
                for entry, best in measured
            ),
            truncated=found.truncated,
            order=tuple(order) if measure is ExerciseMeasure.BAND_REPS else (),
            split=split,
        )
