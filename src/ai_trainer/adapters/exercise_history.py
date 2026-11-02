"""The athlete's past entries of one exercise, read from the gym's typed rows (ADR-0032,
ticket #401), and the names they logged them under (B40, ticket #421). Every statement filters by
`user_id`, so another athlete's entries are never read."""

from collections.abc import Callable, Sequence
from uuid import UUID

from sqlalchemy import ColumnElement, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.orm import (
    ExerciseOrm,
    GymExerciseEntryOrm,
    GymSetOrm,
    TrainingSessionOrm,
)
from ai_trainer.domain.exercises import ExerciseEntry, ExerciseMeasure, LoggedName, LoggedSet


def _number(value: object) -> float | None:
    return None if value is None else float(str(value))


def _fit(name: ColumnElement[str], typed: ColumnElement[str]) -> ColumnElement[float]:
    """How well two normalized names fit, as one word or phrase inside the other, either way
    round: "bench" fits "bench press", and "bench press" fits "bench"."""
    return func.greatest(func.word_similarity(typed, name), func.word_similarity(name, typed))


class SqlAlchemyExerciseHistory:
    """Implements `ExerciseHistoryPort`."""

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def entries(
        self, user_id: UUID, exercise_id: UUID, *, limit: int
    ) -> Sequence[ExerciseEntry]:
        return await self._read(user_id, GymExerciseEntryOrm.exercise_id == exercise_id, limit)

    async def typed_entries(
        self, user_id: UUID, typed_name: str, *, limit: int
    ) -> Sequence[ExerciseEntry]:
        unlinked = GymExerciseEntryOrm.exercise_id.is_(None) & (
            func.exercise_name_norm(GymExerciseEntryOrm.name_as_typed)
            == func.exercise_name_norm(typed_name)
        )
        return await self._read(user_id, unlinked, limit)

    async def logged_names(
        self, user_id: UUID, name: str, *, near: float, limit: int
    ) -> Sequence[LoggedName]:
        typed = func.exercise_name_norm(name)
        as_typed = func.exercise_name_norm(GymExerciseEntryOrm.name_as_typed)
        canonical = func.exercise_name_norm(ExerciseOrm.name)
        score = func.max(
            func.greatest(_fit(as_typed, typed), func.coalesce(_fit(canonical, typed), 0.0))
        )
        # The whole name alike, not one inside the other: "bench press" against "incline bench
        # press" fits as words but isn't close.
        close = func.max(
            func.greatest(
                func.similarity(as_typed, typed),
                func.coalesce(func.similarity(canonical, typed), 0.0),
            )
        )
        entries = func.count(GymExerciseEntryOrm.id)
        shown = func.coalesce(ExerciseOrm.name, func.min(GymExerciseEntryOrm.name_as_typed))
        # Linked entries group by their exercise; typed-only ones by the name as typed.
        typed_only_name = case((GymExerciseEntryOrm.exercise_id.is_(None), as_typed))
        async with self._session_factory() as session:
            rows = await session.execute(
                select(GymExerciseEntryOrm.exercise_id, shown, entries, close)
                .outerjoin(ExerciseOrm, ExerciseOrm.id == GymExerciseEntryOrm.exercise_id)
                .where(GymExerciseEntryOrm.user_id == user_id)
                .group_by(GymExerciseEntryOrm.exercise_id, ExerciseOrm.name, typed_only_name)
                .having(score >= near)
                .order_by(close.desc(), score.desc(), entries.desc(), shown)
                .limit(limit)
            )
            found = rows.tuples().all()
        # A name close to the one asked about stands alone: the variants that only contain it
        # are other exercises, whose numbers would be reported as this one's.
        if any(similar >= near for *_, similar in found):
            found = [row for row in found if row[3] >= near]
        return [
            LoggedName(exercise_id=exercise_id, name=logged, entries=count)
            for exercise_id, logged, count, _ in found
        ]

    async def _read(
        self, user_id: UUID, which: ColumnElement[bool], limit: int
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
                        .where(GymExerciseEntryOrm.user_id == user_id, which)
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
