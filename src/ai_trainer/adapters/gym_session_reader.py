"""Reads an athlete's gym sessions for the History page (ADR-0032, ticket #515): the span of each
session's logs, its entries and sets from the gym's own tables, and the canonical name of an
entry settled to an exercise. Every statement filters by `user_id` (ADR-0022 invariant 6)."""

from collections import defaultdict
from collections.abc import Callable, Sequence
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.orm import ExerciseOrm, GymExerciseEntryOrm, GymSetOrm, TrainingSessionOrm
from ai_trainer.adapters.training_session_repository import select_spans, to_span
from ai_trainer.domain.exercises import ExerciseLabel
from ai_trainer.domain.history import StoredGymSession
from ai_trainer.domain.sports.gym.payload import Exercise, GymSet, Measure
from ai_trainer.domain.sports.gym.rows import load_of

GYM = "gym"


def _gym_set(row: GymSetOrm) -> GymSet:
    return GymSet.model_construct(
        reps=row.reps,
        load=None if row.load_kg is None else load_of(row.load_original, row.load_kg),
        duration_s=row.duration_s,
        distance_m=None if row.distance_m is None else float(row.distance_m),
        band_label=row.band_label,
    )


class SqlAlchemyGymSessionReader:
    """Implements `GymSessionReaderPort`."""

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_sessions(self, user_id: UUID) -> Sequence[StoredGymSession]:
        spans = select_spans(user_id).where(TrainingSessionOrm.sport_id == GYM)
        async with self._session_factory() as db:
            span_rows = [to_span(row) for row in (await db.execute(spans)).all()]
            ids = [span.id for span in span_rows]
            entries = (
                (
                    await db.execute(
                        select(GymExerciseEntryOrm, ExerciseOrm.name)
                        .outerjoin(ExerciseOrm, ExerciseOrm.id == GymExerciseEntryOrm.exercise_id)
                        .where(
                            GymExerciseEntryOrm.user_id == user_id,
                            GymExerciseEntryOrm.session_id.in_(ids),
                        )
                        .order_by(GymExerciseEntryOrm.position)
                    )
                )
                .tuples()
                .all()
            )
            sets = defaultdict[UUID, list[GymSetOrm]](list)
            for row in await db.scalars(
                select(GymSetOrm)
                .where(
                    GymSetOrm.user_id == user_id,
                    GymSetOrm.entry_id.in_([entry.id for entry, _ in entries]),
                )
                .order_by(GymSetOrm.set_no)
            ):
                sets[row.entry_id].append(row)
        exercises = defaultdict[UUID, list[Exercise]](list)
        labels = defaultdict[UUID, dict[str, ExerciseLabel]](dict)
        for entry, canonical in entries:
            exercises[entry.session_id].append(
                # Not validated: the rows were valid when written, and a stricter payload rule
                # later must not take the athlete's whole history down with it.
                Exercise.model_construct(
                    exercise_id=entry.exercise_id,
                    name_as_typed=entry.name_as_typed,
                    # The check constraint keeps `measure` to the five values.
                    measure=cast(Measure, entry.measure),
                    per_side=entry.per_side,
                    implements=entry.implements,
                    rir=entry.rir,
                    sets=[_gym_set(row) for row in sets[entry.id]],
                )
            )
            if canonical is not None:
                labels[entry.session_id][entry.name_as_typed] = ExerciseLabel(name=canonical)
        return [
            StoredGymSession(span=span, exercises=tuple(exercises[span.id]), labels=labels[span.id])
            for span in span_rows
        ]
