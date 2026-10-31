"""The athlete's past entries of one exercise, read from the gym's typed rows (ADR-0032,
ticket #401). Every statement filters by `user_id`, so another athlete's entries are never read."""

from collections.abc import Callable, Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.orm import GymExerciseEntryOrm, GymSetOrm, TrainingSessionOrm
from ai_trainer.domain.exercises import ExerciseEntry, ExerciseMeasure, LoggedSet


def _number(value: object) -> float | None:
    return None if value is None else float(str(value))


class SqlAlchemyExerciseHistory:
    """Implements `ExerciseHistoryPort`."""

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def entries(
        self, user_id: UUID, exercise_id: UUID, *, limit: int
    ) -> Sequence[ExerciseEntry]:
        async with self._session_factory() as session:
            found = (
                (
                    await session.execute(
                        select(GymExerciseEntryOrm, TrainingSessionOrm.occurred_on)
                        .join(
                            TrainingSessionOrm,
                            (TrainingSessionOrm.id == GymExerciseEntryOrm.session_id)
                            & (TrainingSessionOrm.user_id == GymExerciseEntryOrm.user_id),
                        )
                        .where(
                            GymExerciseEntryOrm.user_id == user_id,
                            GymExerciseEntryOrm.exercise_id == exercise_id,
                        )
                        .order_by(
                            TrainingSessionOrm.occurred_on.desc(),
                            TrainingSessionOrm.created_at.desc(),
                            GymExerciseEntryOrm.position,
                        )
                        .limit(limit)
                    )
                )
                .tuples()
                .all()
            )
            sets = (
                await session.scalars(
                    select(GymSetOrm)
                    .where(
                        GymSetOrm.user_id == user_id,
                        GymSetOrm.entry_id.in_([entry.id for entry, _ in found]),
                    )
                    .order_by(GymSetOrm.entry_id, GymSetOrm.set_no)
                )
            ).all()
        by_entry: dict[UUID, list[LoggedSet]] = {}
        for row in sets:
            by_entry.setdefault(row.entry_id, []).append(
                LoggedSet(
                    set_no=row.set_no,
                    reps=row.reps,
                    load_kg=_number(row.load_kg),
                    load_original=row.load_original,
                    duration_s=row.duration_s,
                    distance_m=_number(row.distance_m),
                    band_label=row.band_label,
                )
            )
        return [
            ExerciseEntry(
                session_id=entry.session_id,
                occurred_on=occurred_on,
                name_as_typed=entry.name_as_typed,
                measure=ExerciseMeasure(entry.measure),
                per_side=entry.per_side,
                implements=entry.implements,
                rir=entry.rir,
                sets=tuple(by_entry.get(entry.id, ())),
            )
            for entry, occurred_on in found
        ]
