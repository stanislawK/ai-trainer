from collections.abc import Callable, Mapping, Sequence
from uuid import UUID, uuid4

from sqlalchemy import (
    ARRAY,
    Boolean,
    ColumnElement,
    Row,
    SQLColumnExpression,
    Text,
    case,
    cast,
    func,
    literal_column,
    or_,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.orm import ExerciseAliasOrm, ExerciseOrm, ExerciseTextOrm
from ai_trainer.domain.exercises import (
    CatalogueExercise,
    CatalogueWrite,
    Exercise,
    ExerciseAlias,
    ExerciseCandidate,
    ExerciseDetail,
    ExerciseMeasure,
)

_CUSTOM_SOURCE = "custom"
# A description is the start of the imported instructions, short enough for a tool result.
MAX_DESCRIPTION = 200
# The full-text configuration of ADR-0031's search indexes.
_SIMPLE: ColumnElement[str] = literal_column("'simple'::regconfig")
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
_ALIAS_COLUMNS = (
    ExerciseAliasOrm.exercise_id,
    ExerciseAliasOrm.alias,
    ExerciseAliasOrm.alias_norm,
)


def visible_to(user_id: UUID) -> ColumnElement[bool]:
    """A catalogue row or the athlete's own (ADR-0031 invariant 3)."""
    return or_(ExerciseOrm.owner_user_id.is_(None), ExerciseOrm.owner_user_id == user_id)


def custom_row(user_id: UUID, *, sport_id: str, name: str, measure: str) -> ExerciseOrm:
    """An athlete's own exercise, not yet added to a session."""
    return ExerciseOrm(
        id=uuid4(),
        sport_id=sport_id,
        owner_user_id=user_id,
        source=_CUSTOM_SOURCE,
        name=name,
        default_measure=measure,
    )


async def write_user_alias(
    session: AsyncSession, user_id: UUID, exercise_id: UUID, alias: str
) -> ExerciseAlias:
    """Maps the name onto one exercise, in the caller's transaction, replacing any earlier
    mapping of it. The caller has checked that `user_id` can see the exercise."""
    statement = insert(ExerciseAliasOrm).values(
        id=uuid4(), exercise_id=exercise_id, user_id=user_id, alias=alias
    )
    written = await session.execute(
        statement.on_conflict_do_update(
            index_elements=["user_id", "alias_norm"],
            index_where=text("user_id IS NOT NULL"),
            set_={"exercise_id": exercise_id, "alias": alias},
        ).returning(*_ALIAS_COLUMNS)
    )
    return _alias(written.one())


def _name_score(
    column: SQLColumnExpression[str], name: SQLColumnExpression[str], floor: float
) -> ColumnElement[float]:
    """How well a stored name fits: its trigram similarity, and at least `floor` when it holds
    every word of the typed name (`simple` full text, as PostgreSQL has no Polish stemmer)."""
    words = func.plainto_tsquery(_SIMPLE, name)
    return func.greatest(
        func.similarity(column, name),
        case((func.to_tsvector(_SIMPLE, column).op("@@")(words), floor), else_=0.0),
    )


def _short(text_: str | None) -> str | None:
    # One line, cut at a word where there is one.
    flat = " ".join((text_ or "").split())
    if not flat:
        return None
    if len(flat) <= MAX_DESCRIPTION:
        return flat
    cut = flat[: MAX_DESCRIPTION - 1]
    return (cut.rpartition(" ")[0] or cut) + "…"


def _exercise(row: ExerciseOrm) -> Exercise:
    return Exercise(
        id=row.id,
        sport_id=row.sport_id,
        owner_user_id=row.owner_user_id,
        name=row.name,
        default_measure=ExerciseMeasure(row.default_measure),
    )


def _alias(row: Row[tuple[UUID, str, str]]) -> ExerciseAlias:
    return ExerciseAlias(exercise_id=row[0], alias=row[1], alias_norm=row[2])


async def _upsert_exercises(
    session: AsyncSession, exercises: Sequence[CatalogueExercise]
) -> tuple[int, int]:
    """How many rows were inserted and how many updated."""
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
    return sum(inserted), len(inserted) - sum(inserted)


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
            inserted, updated = await _upsert_exercises(session, exercises)
            ids = await _catalogue_ids(session, exercises)
            texts_written = await _upsert_texts(session, exercises, ids)
            await session.commit()
        return CatalogueWrite(
            inserted=inserted,
            updated=updated,
            unchanged=len(exercises) - inserted - updated,
            texts_written=texts_written,
        )

    async def get(self, user_id: UUID, exercise_id: UUID) -> Exercise | None:
        async with self._session_factory() as session:
            row = await session.scalar(
                select(ExerciseOrm).where(ExerciseOrm.id == exercise_id, visible_to(user_id))
            )
            return None if row is None else _exercise(row)

    async def match_user_aliases(
        self, user_id: UUID, *, sport_id: str, names: Sequence[str], near: float
    ) -> Mapping[str, UUID]:
        typed = func.unnest(cast(list(names), ARRAY(Text))).column_valued("typed")
        norm = func.exercise_name_norm(typed)
        similarity = func.similarity(ExerciseAliasOrm.alias_norm, norm)
        exact = ExerciseAliasOrm.alias_norm == norm
        async with self._session_factory() as session:
            rows = await session.execute(
                select(typed, ExerciseAliasOrm.exercise_id)
                .distinct(typed)
                .join(ExerciseAliasOrm, or_(exact, similarity >= near))
                .join(ExerciseOrm, ExerciseOrm.id == ExerciseAliasOrm.exercise_id)
                .where(ExerciseAliasOrm.user_id == user_id, ExerciseOrm.sport_id == sport_id)
                .order_by(typed, exact.desc(), similarity.desc(), ExerciseAliasOrm.alias_norm)
            )
            return dict(rows.tuples().all())

    async def shortlist(
        self,
        user_id: UUID,
        *,
        sport_id: str,
        name: str,
        measure: str,
        floor: float,
        limit: int,
    ) -> Sequence[ExerciseCandidate]:
        typed = func.exercise_name_norm(name)
        by_alias = (
            select(func.max(_name_score(ExerciseAliasOrm.alias_norm, typed, floor)))
            .where(
                ExerciseAliasOrm.exercise_id == ExerciseOrm.id,
                or_(ExerciseAliasOrm.user_id.is_(None), ExerciseAliasOrm.user_id == user_id),
            )
            .scalar_subquery()
        )
        score = func.greatest(
            _name_score(func.exercise_name_norm(ExerciseOrm.name), typed, floor),
            func.coalesce(by_alias, 0.0),
        )
        async with self._session_factory() as session:
            rows = await session.execute(
                select(ExerciseOrm.id, ExerciseOrm.name, ExerciseOrm.equipment, ExerciseOrm.target)
                .where(ExerciseOrm.sport_id == sport_id, visible_to(user_id), score >= floor)
                .order_by(
                    (ExerciseOrm.default_measure == measure).desc(),
                    score.desc(),
                    ExerciseOrm.name,
                )
                .limit(limit)
            )
            return [
                ExerciseCandidate(id=id_, name=found, equipment=equipment, target=target)
                for id_, found, equipment, target in rows.tuples()
            ]

    async def describe(
        self, user_id: UUID, exercise_ids: Sequence[UUID], *, lang: str
    ) -> Sequence[ExerciseDetail]:
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(ExerciseOrm).where(ExerciseOrm.id.in_(exercise_ids), visible_to(user_id))
            )
            by_id = {row.id: row for row in rows}
            texts = await session.execute(
                select(ExerciseTextOrm.exercise_id, ExerciseTextOrm.instructions)
                .where(ExerciseTextOrm.exercise_id.in_(by_id))
                .order_by(
                    ExerciseTextOrm.exercise_id,
                    (ExerciseTextOrm.lang == lang).desc(),
                    ExerciseTextOrm.lang,
                )
            )
            first: dict[UUID, str] = {}
            for exercise_id, instructions in texts.tuples():
                first.setdefault(exercise_id, instructions)
        return [
            ExerciseDetail(
                id=by_id[exercise_id].id,
                name=by_id[exercise_id].name,
                equipment=by_id[exercise_id].equipment,
                target=by_id[exercise_id].target,
                description=_short(first.get(exercise_id)),
            )
            for exercise_id in exercise_ids
            if exercise_id in by_id
        ]

    async def create_custom(
        self, user_id: UUID, *, sport_id: str, name: str, default_measure: ExerciseMeasure
    ) -> Exercise:
        async with self._session_factory() as session:
            row = custom_row(user_id, sport_id=sport_id, name=name, measure=default_measure.value)
            session.add(row)
            exercise = _exercise(row)
            await session.commit()
            return exercise

    async def add_user_alias(
        self, user_id: UUID, exercise_id: UUID, alias: str
    ) -> ExerciseAlias | None:
        async with self._session_factory() as session:
            visible = await session.scalar(
                select(ExerciseOrm.id).where(ExerciseOrm.id == exercise_id, visible_to(user_id))
            )
            if visible is None:
                return None
            alias_row = await write_user_alias(session, user_id, exercise_id, alias)
            await session.commit()
            return alias_row

    async def user_aliases(self, user_id: UUID) -> Sequence[ExerciseAlias]:
        async with self._session_factory() as session:
            rows = await session.execute(
                select(*_ALIAS_COLUMNS)
                .where(ExerciseAliasOrm.user_id == user_id)
                .order_by(ExerciseAliasOrm.alias_norm)
            )
            return [_alias(row) for row in rows]
