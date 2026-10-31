from collections.abc import Callable, Mapping, Sequence
from uuid import UUID, uuid4

from sqlalchemy import Boolean, ColumnElement, literal_column, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.orm import ExerciseAliasOrm, ExerciseOrm, ExerciseTextOrm
from ai_trainer.domain.exercises import (
    CatalogueExercise,
    CatalogueWrite,
    Exercise,
    ExerciseAlias,
    ExerciseMeasure,
)

_CUSTOM_SOURCE = "custom"
_IMPORTED_COLUMNS = (
    "sport_id",
    "name",
    "equipment",
    "body_part",
    "target",
    "secondary_muscles",
    "default_measure",
    "attribution",
)
_TEXT_COLUMNS = ("instructions", "steps")


def _visible_to(user_id: UUID) -> ColumnElement[bool]:
    """A catalogue row or the athlete's own (ADR-0031 invariant 3)."""
    return or_(ExerciseOrm.owner_user_id.is_(None), ExerciseOrm.owner_user_id == user_id)


def _exercise(row: ExerciseOrm) -> Exercise:
    return Exercise(
        id=row.id,
        sport_id=row.sport_id,
        owner_user_id=row.owner_user_id,
        name=row.name,
        default_measure=ExerciseMeasure(row.default_measure),
    )


async def _upsert_exercises(
    session: AsyncSession, exercises: Sequence[CatalogueExercise]
) -> CatalogueWrite:
    table = ExerciseOrm.__table__
    statement = insert(ExerciseOrm).values(
        [
            {
                "id": uuid4(),
                "source": exercise.source,
                "source_ref": exercise.source_ref,
                "sport_id": exercise.sport_id,
                "name": exercise.name,
                "equipment": exercise.equipment,
                "body_part": exercise.body_part,
                "target": exercise.target,
                "secondary_muscles": list(exercise.secondary_muscles),
                "default_measure": exercise.default_measure.value,
                "attribution": exercise.attribution,
            }
            for exercise in exercises
        ]
    )
    # An unchanged row isn't rewritten, so a second import leaves every row version alone. `xmax`
    # is 0 only on a row this statement inserted.
    written = await session.execute(
        statement.on_conflict_do_update(
            constraint="exercises_source_source_ref_key",
            set_={column: statement.excluded[column] for column in _IMPORTED_COLUMNS},
            where=or_(
                *(
                    table.c[column].is_distinct_from(statement.excluded[column])
                    for column in _IMPORTED_COLUMNS
                )
            ),
        ).returning(literal_column("xmax = 0", Boolean()))
    )
    inserted = [row[0] for row in written]
    return CatalogueWrite(
        inserted=sum(inserted),
        updated=len(inserted) - sum(inserted),
        unchanged=len(exercises) - len(inserted),
        texts_written=0,
    )


async def _catalogue_ids(
    session: AsyncSession, exercises: Sequence[CatalogueExercise]
) -> Mapping[tuple[str, str | None], UUID]:
    rows = await session.execute(
        select(ExerciseOrm.source, ExerciseOrm.source_ref, ExerciseOrm.id).where(
            ExerciseOrm.source.in_({exercise.source for exercise in exercises}),
            ExerciseOrm.source_ref.in_({exercise.source_ref for exercise in exercises}),
        )
    )
    return {(source, source_ref): id_ for source, source_ref, id_ in rows}


async def _upsert_texts(
    session: AsyncSession,
    exercises: Sequence[CatalogueExercise],
    ids: Mapping[tuple[str, str | None], UUID],
) -> int:
    table = ExerciseTextOrm.__table__
    statement = insert(ExerciseTextOrm).values(
        [
            {
                "exercise_id": ids[exercise.source, exercise.source_ref],
                "lang": exercise_text.lang,
                "instructions": exercise_text.instructions,
                "steps": list(exercise_text.steps),
            }
            for exercise in exercises
            for exercise_text in exercise.texts
        ]
    )
    written = await session.execute(
        statement.on_conflict_do_update(
            index_elements=["exercise_id", "lang"],
            set_={column: statement.excluded[column] for column in _TEXT_COLUMNS},
            where=or_(
                *(
                    table.c[column].is_distinct_from(statement.excluded[column])
                    for column in _TEXT_COLUMNS
                )
            ),
        ).returning(table.c.exercise_id)
    )
    return len(written.all())


class SqlAlchemyExerciseRepository:
    """Implements `ExerciseRepositoryPort` (ADR-0004: repositories live in adapters/)."""

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def upsert_catalogue(self, exercises: Sequence[CatalogueExercise]) -> CatalogueWrite:
        async with self._session_factory() as session:
            written = await _upsert_exercises(session, exercises)
            ids = await _catalogue_ids(session, exercises)
            texts_written = await _upsert_texts(session, exercises, ids)
            await session.commit()
        return written.model_copy(update={"texts_written": texts_written})

    async def get(self, user_id: UUID, exercise_id: UUID) -> Exercise | None:
        async with self._session_factory() as session:
            row = await session.scalar(
                select(ExerciseOrm).where(ExerciseOrm.id == exercise_id, _visible_to(user_id))
            )
            return None if row is None else _exercise(row)

    async def create_custom(
        self, user_id: UUID, *, sport_id: str, name: str, default_measure: ExerciseMeasure
    ) -> Exercise:
        async with self._session_factory() as session:
            row = ExerciseOrm(
                id=uuid4(),
                sport_id=sport_id,
                owner_user_id=user_id,
                source=_CUSTOM_SOURCE,
                name=name,
                default_measure=default_measure.value,
            )
            session.add(row)
            exercise = _exercise(row)
            await session.commit()
            return exercise

    async def add_user_alias(
        self, user_id: UUID, exercise_id: UUID, alias: str
    ) -> ExerciseAlias | None:
        async with self._session_factory() as session:
            visible = await session.scalar(
                select(ExerciseOrm.id).where(ExerciseOrm.id == exercise_id, _visible_to(user_id))
            )
            if visible is None:
                return None
            statement = insert(ExerciseAliasOrm).values(
                id=uuid4(), exercise_id=exercise_id, user_id=user_id, alias=alias
            )
            written = await session.execute(
                statement.on_conflict_do_update(
                    index_elements=["user_id", "alias_norm"],
                    index_where=text("user_id IS NOT NULL"),
                    set_={"exercise_id": exercise_id, "alias": alias},
                ).returning(
                    ExerciseAliasOrm.exercise_id,
                    ExerciseAliasOrm.alias,
                    ExerciseAliasOrm.alias_norm,
                )
            )
            row = written.one()
            await session.commit()
            return ExerciseAlias(exercise_id=row[0], alias=row[1], alias_norm=row[2])

    async def user_aliases(self, user_id: UUID) -> Sequence[ExerciseAlias]:
        async with self._session_factory() as session:
            rows = await session.execute(
                select(
                    ExerciseAliasOrm.exercise_id,
                    ExerciseAliasOrm.alias,
                    ExerciseAliasOrm.alias_norm,
                )
                .where(ExerciseAliasOrm.user_id == user_id)
                .order_by(ExerciseAliasOrm.alias_norm)
            )
            return [
                ExerciseAlias(exercise_id=row[0], alias=row[1], alias_norm=row[2]) for row in rows
            ]
