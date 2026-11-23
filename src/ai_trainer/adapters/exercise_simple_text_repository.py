from collections.abc import Callable, Collection, Mapping, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, and_, case, delete, literal, null, or_, select, update
from sqlalchemy.dialects.postgresql import Insert, insert
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.orm import ExerciseOrm, ExerciseSimpleTextOrm, ExerciseTextOrm
from ai_trainer.domain.exercise_simple_texts import (
    CardHowtoSource,
    FailedCheckOutcome,
    FlaggedText,
    HowtoReviewRow,
    HowtoSource,
    NewSimpleText,
    SimpleTextStatus,
    StoredSimpleText,
)

# The statuses the admin's list shows, and the only ones its Edit and Clear change (#594).
_REVIEWED = (SimpleTextStatus.FLAGGED.value, SimpleTextStatus.REJECTED.value)


class SqlAlchemyExerciseSimpleTextRepository:
    """Implements `ExerciseSimpleTextsPort` (ADR-0031 *Beginner how-to*, #589, #591) and
    `HowtoReviewPort` (#594): the shared
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

    async def flag(
        self, exercise_id: UUID, *, lang: str, flagged_at: datetime
    ) -> FlaggedText | None:
        """One guarded UPDATE (#593): only a `shown` row with a text changes, so of two
        concurrent flags one returns the row and the other `None`. An admin's edit is flagged
        like any shown text; the card hides any flagged row."""
        row = ExerciseSimpleTextOrm
        statement = (
            update(row)
            .where(
                row.exercise_id == exercise_id,
                row.lang == lang,
                row.status == SimpleTextStatus.SHOWN.value,
                row.text.is_not(None),
            )
            .values(status=SimpleTextStatus.FLAGGED.value, flagged_at=flagged_at)
            .returning(row.template_id, row.template_version)
        )
        async with self._session_factory() as session:
            flagged = (await session.execute(statement)).one_or_none()
            await session.commit()
        if flagged is None:
            return None
        return FlaggedText(
            template_id=flagged.template_id, template_version=flagged.template_version
        )

    async def review_rows(self, *, lang: str) -> Sequence[HowtoReviewRow]:
        """Flagged and `rejected` rows with a text, joined to the exercise's name and its text
        in `lang`, the latest flag first (a `rejected` row has none), then by name. The text is
        an outer join: a flagged row stays hidden from athletes, so it is listed even when its
        exercise has lost its text, with no original."""
        row = ExerciseSimpleTextOrm
        statement = (
            select(
                row.exercise_id,
                ExerciseOrm.name,
                row.text,
                row.status,
                row.flagged_at,
                ExerciseTextOrm.instructions,
            )
            .join(ExerciseOrm, ExerciseOrm.id == row.exercise_id)
            .outerjoin(
                ExerciseTextOrm,
                and_(ExerciseTextOrm.exercise_id == row.exercise_id, ExerciseTextOrm.lang == lang),
            )
            .where(row.lang == lang, row.status.in_(_REVIEWED), row.text.is_not(None))
            .order_by(row.flagged_at.desc().nulls_last(), ExerciseOrm.name)
        )
        async with self._session_factory() as session:
            rows = (await session.execute(statement)).all()
        return [
            HowtoReviewRow(
                exercise_id=found.exercise_id,
                name=found.name,
                text=found.text,
                status=SimpleTextStatus(found.status),
                flagged_at=found.flagged_at,
                original=found.instructions,
            )
            for found in rows
        ]

    async def admin_edit(
        self, exercise_id: UUID, *, lang: str, text: str, edited_at: datetime
    ) -> bool:
        """One guarded UPDATE: only a listed row takes the admin's text, `shown` again with
        `edited_at`, which the upsert's guard never rewrites."""
        row = ExerciseSimpleTextOrm
        statement = (
            update(row)
            .where(row.exercise_id == exercise_id, row.lang == lang, row.status.in_(_REVIEWED))
            .values(text=text, status=SimpleTextStatus.SHOWN.value, edited_at=edited_at)
            .returning(row.exercise_id)
        )
        async with self._session_factory() as session:
            edited = (await session.execute(statement)).scalar_one_or_none()
            await session.commit()
        return edited is not None

    async def clear(self, exercise_id: UUID, *, lang: str) -> str | None:
        """One guarded DELETE, returning the exercise's name: only a listed row goes."""
        row = ExerciseSimpleTextOrm
        name = select(ExerciseOrm.name).where(ExerciseOrm.id == row.exercise_id).scalar_subquery()
        statement = (
            delete(row)
            .where(row.exercise_id == exercise_id, row.lang == lang, row.status.in_(_REVIEWED))
            .returning(name)
        )
        async with self._session_factory() as session:
            cleared = (await session.execute(statement)).scalar_one_or_none()
            await session.commit()
        return cleared


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
