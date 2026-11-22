from collections.abc import Callable
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.orm import ExerciseOrm, ExerciseSimpleTextOrm, ExerciseTextOrm
from ai_trainer.domain.exercise_simple_texts import (
    HowtoSource,
    NewSimpleText,
    SimpleTextStatus,
    StoredSimpleText,
)


class SqlAlchemyExerciseSimpleTextRepository:
    """Implements `ExerciseSimpleTextsPort` (ADR-0031 *Beginner how-to*, #589): the shared
    catalogue only, so no athlete's exercise is ever sent to a model, and `exercise_simple_texts`,
    which holds no user data."""

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def howto_source(self, exercise_id: UUID, *, lang: str) -> HowtoSource | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(ExerciseOrm, ExerciseTextOrm.instructions)
                    .join(ExerciseTextOrm, ExerciseTextOrm.exercise_id == ExerciseOrm.id)
                    .where(
                        ExerciseOrm.id == exercise_id,
                        ExerciseOrm.owner_user_id.is_(None),
                        ExerciseTextOrm.lang == lang,
                    )
                )
            ).one_or_none()
        if row is None:
            return None
        exercise, instructions = row._tuple()
        return HowtoSource(
            exercise_id=exercise.id,
            name=exercise.name,
            instructions=instructions,
            target=exercise.target,
            equipment=exercise.equipment,
        )

    async def stored(self, exercise_id: UUID, *, lang: str) -> StoredSimpleText | None:
        async with self._session_factory() as session:
            row = await session.get(ExerciseSimpleTextOrm, (exercise_id, lang))
        if row is None:
            return None
        return StoredSimpleText(
            source_sha256=row.source_sha256,
            status=SimpleTextStatus(row.status),
            edited_at=row.edited_at,
        )

    async def write(self, text: NewSimpleText) -> bool:
        """`ON CONFLICT DO UPDATE … WHERE`: against a row from the same source, or an edited row,
        it changes nothing, so the first write wins; a stale unedited row is rewritten whole, shown
        again with its checks and flag cleared (ADR-0031 *Stale text*)."""
        row = ExerciseSimpleTextOrm
        values = {
            "exercise_id": text.exercise_id,
            "lang": text.lang,
            "text": text.text,
            "template_id": text.template_id,
            "template_version": text.template_version,
            "model": text.model,
            "source_sha256": text.source_sha256,
            "status": SimpleTextStatus.SHOWN.value,
            "check_version": None,
            "check_failures": 0,
            "created_at": text.created_at,
            "flagged_at": None,
        }
        inserted = insert(row).values(values)
        rewritten = {
            column: inserted.excluded[column]
            for column in values
            if column not in ("exercise_id", "lang")
        }
        statement = inserted.on_conflict_do_update(
            index_elements=[row.exercise_id, row.lang],
            set_=rewritten,
            where=and_(
                row.source_sha256 != inserted.excluded.source_sha256,
                row.edited_at.is_(None),
            ),
        ).returning(row.exercise_id)
        async with self._session_factory() as session:
            written = (await session.execute(statement)).scalar_one_or_none()
            await session.commit()
        return written is not None
