from collections.abc import Callable, Collection, Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, and_, case, literal, null, or_, select
from sqlalchemy.dialects.postgresql import Insert, insert
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.orm import ExerciseOrm, ExerciseSimpleTextOrm, ExerciseTextOrm
from ai_trainer.domain.exercise_simple_texts import (
    CardHowtoSource,
    FailedCheckOutcome,
    HowtoSource,
    NewSimpleText,
    SimpleTextStatus,
    StoredSimpleText,
)


class SqlAlchemyExerciseSimpleTextRepository:
    """Implements `ExerciseSimpleTextsPort` (ADR-0031 *Beginner how-to*, #589, #591): the shared
    catalogue only, so no athlete's exercise is ever sent to a model, and `exercise_simple_texts`,
    which holds no user data."""

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def howto_source(self, exercise_id: UUID, *, lang: str) -> HowtoSource | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(
                        ExerciseOrm.id,
                        ExerciseOrm.name,
                        ExerciseTextOrm.instructions,
                        ExerciseOrm.target,
                        ExerciseOrm.equipment,
                    )
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
        return HowtoSource(
            exercise_id=row.id,
            name=row.name,
            instructions=row.instructions,
            target=row.target,
            equipment=row.equipment,
        )

    async def stored(self, exercise_id: UUID, *, lang: str) -> StoredSimpleText | None:
        async with self._session_factory() as session:
            row = await session.get(ExerciseSimpleTextOrm, (exercise_id, lang))
        if row is None:
            return None
        return _stored(row)

    async def card_rows(
        self, exercise_ids: Collection[UUID], *, lang: str
    ) -> Mapping[UUID, CardHowtoSource]:
        """The catalogue's text in `lang` joined to its stored short version, if any, in one
        query for every candidate of the open cards (#592)."""
        if not exercise_ids:
            return {}
        simple = ExerciseSimpleTextOrm
        statement = (
            select(ExerciseTextOrm.exercise_id, ExerciseTextOrm.instructions, simple)
            .join(ExerciseOrm, ExerciseOrm.id == ExerciseTextOrm.exercise_id)
            .outerjoin(
                simple,
                and_(simple.exercise_id == ExerciseTextOrm.exercise_id, simple.lang == lang),
            )
            .where(
                ExerciseTextOrm.exercise_id.in_(list(exercise_ids)),
                ExerciseTextOrm.lang == lang,
                ExerciseOrm.owner_user_id.is_(None),
            )
        )
        async with self._session_factory() as session:
            rows = (await session.execute(statement)).all()
        return {
            exercise_id: CardHowtoSource(
                instructions=instructions,
                stored=None if stored is None else _stored(stored),
                text=None if stored is None else stored.text,
            )
            for exercise_id, instructions, stored in rows
        }

    async def write(self, text: NewSimpleText) -> bool:
        """`ON CONFLICT DO UPDATE … WHERE`: against a row with a text from the same source, or an
        edited row, it changes nothing, so the first write wins; a stale or text-less unedited
        row is rewritten whole, shown again with its flag cleared. A text-less row of the same
        source keeps its failure count; a stale row's restarts (ADR-0031 *Stale text*, #591)."""
        row = ExerciseSimpleTextOrm
        inserted = insert(row).values(_values(text, body=text.text, check_failures=0))
        same_source = row.source_sha256 == inserted.excluded.source_sha256
        statement = _upsert(
            inserted, check_failures=case((same_source, row.check_failures), else_=0)
        ).returning(row.exercise_id)
        async with self._session_factory() as session:
            written = (await session.execute(statement)).scalar_one_or_none()
            await session.commit()
        return written is not None

    async def record_failed_check(self, text: NewSimpleText) -> FailedCheckOutcome:
        """One upsert under the same guard as `write`: a new row, or a stale one, gets no text
        and a count of 1; a text-less row of the same source (its first failure) becomes
        `rejected`, keeping `text` for the admin list (#594), with a count of 2. A row with a
        text from the same source (another athlete's write passed) or an edited row is left as
        it is (#591)."""
        row = ExerciseSimpleTextOrm
        inserted = insert(row).values(_values(text, body=None, check_failures=1))
        same_source = row.source_sha256 == inserted.excluded.source_sha256
        statement = _upsert(
            inserted,
            text=case((same_source, literal(text.text)), else_=null()),
            status=case(
                (same_source, literal(SimpleTextStatus.REJECTED.value)),
                else_=literal(SimpleTextStatus.SHOWN.value),
            ),
            check_failures=case((same_source, row.check_failures + 1), else_=1),
        ).returning(row.status)
        async with self._session_factory() as session:
            status = (await session.execute(statement)).scalar_one_or_none()
            await session.commit()
        if status is None:
            return "present"
        return "rejected" if status == SimpleTextStatus.REJECTED.value else "counted"


def _stored(row: ExerciseSimpleTextOrm) -> StoredSimpleText:
    return StoredSimpleText(
        source_sha256=row.source_sha256,
        status=SimpleTextStatus(row.status),
        edited_at=row.edited_at,
        has_text=row.text is not None,
        check_failures=row.check_failures,
    )


# Every column an upsert rewrites from its own values: all but the key and an admin's edit.
_REWRITTEN = (
    "text",
    "template_id",
    "template_version",
    "model",
    "source_sha256",
    "status",
    "check_version",
    "check_failures",
    "created_at",
    "flagged_at",
)


def _values(text: NewSimpleText, *, body: str | None, check_failures: int) -> dict[str, Any]:
    return {
        "exercise_id": text.exercise_id,
        "lang": text.lang,
        "text": body,
        "template_id": text.template_id,
        "template_version": text.template_version,
        "model": text.model,
        "source_sha256": text.source_sha256,
        "status": SimpleTextStatus.SHOWN.value,
        "check_version": text.check_version,
        "check_failures": check_failures,
        "created_at": text.created_at,
        "flagged_at": None,
    }


def _upsert(inserted: Insert, **overrides: ColumnElement[Any]) -> Insert:
    """The insert, or a rewrite of an unedited row that is stale or has no text, from the
    insert's own values with `overrides` on top: never over an admin's edit, and never over a
    text already written from the same source."""
    row = ExerciseSimpleTextOrm
    rewritten: dict[str, Any] = {column: inserted.excluded[column] for column in _REWRITTEN}
    return inserted.on_conflict_do_update(
        index_elements=[row.exercise_id, row.lang],
        set_=rewritten | overrides,
        where=and_(
            row.edited_at.is_(None),
            or_(row.source_sha256 != inserted.excluded.source_sha256, row.text.is_(None)),
        ),
    )
