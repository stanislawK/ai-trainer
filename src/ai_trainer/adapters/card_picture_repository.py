"""The picture drawn on the exercise card (ADR-0031 *Picture on the card*, #643): which catalogue
exercises may be drawn, the one to draw, and its write under a transaction-scoped advisory lock on
the exercise ID, so two cards drawing at once keep one link (first write wins)."""

from collections.abc import Callable, Collection
from uuid import UUID, uuid4

from sqlalchemy import ColumnElement, and_, exists, func, not_, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ai_trainer.adapters.orm import ExerciseMediaOrm, ExerciseOrm, ExerciseTextOrm, MediaAssetOrm
from ai_trainer.domain.exercises import ExerciseText
from ai_trainer.domain.media import MediaAsset, MediaLinkStatus, MediaRepositoryError
from ai_trainer.domain.media_generation import GENERATED_PICTURE_SOURCE, PictureSubject

# Keeps this lock's keys apart from other advisory locks taken on a hashed UUID.
_LOCK_SEED = 643

_LIVE = (MediaLinkStatus.SUGGESTED.value, MediaLinkStatus.APPROVED.value)


def _drawable(lang: str) -> ColumnElement[bool]:
    """ADR-0031's *When* rule on `ExerciseOrm`: a catalogue exercise with a text in `lang` that
    says how it is done, no live link, and no removed link of a generated picture. A removed link
    from another source doesn't block it."""
    # Aliases, so a query that also selects these tables doesn't correlate them away.
    texts, links, assets = (
        aliased(ExerciseTextOrm),
        aliased(ExerciseMediaOrm),
        aliased(MediaAssetOrm),
    )
    has_text = exists().where(
        texts.exercise_id == ExerciseOrm.id,
        texts.lang == lang,
        or_(
            func.btrim(texts.instructions) != "",
            func.btrim(func.array_to_string(texts.steps, "")) != "",
        ),
    )
    has_live = exists().where(links.exercise_id == ExerciseOrm.id, links.status.in_(_LIVE))
    removed_generated = exists().where(
        links.exercise_id == ExerciseOrm.id,
        links.status == MediaLinkStatus.REMOVED.value,
        links.asset_id == assets.id,
        assets.source == GENERATED_PICTURE_SOURCE,
    )
    return and_(
        ExerciseOrm.owner_user_id.is_(None), has_text, not_(has_live), not_(removed_generated)
    )


class SqlAlchemyCardPictureRepository:
    """Implements `CardPictureRepositoryPort`. Catalogue media holds no user data, so it takes no
    `user_id`; the exercise IDs come from the athlete's own, user-scoped question."""

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def drawable(self, exercise_ids: Collection[UUID], *, lang: str) -> set[UUID]:
        if not exercise_ids:
            return set()
        statement = select(ExerciseOrm.id).where(ExerciseOrm.id.in_(exercise_ids), _drawable(lang))
        async with self._session_factory() as session:
            return set((await session.scalars(statement)).all())

    async def card_subject(self, exercise_id: UUID, *, lang: str) -> PictureSubject | None:
        statement = (
            select(ExerciseOrm, ExerciseTextOrm)
            .join(ExerciseTextOrm, ExerciseTextOrm.exercise_id == ExerciseOrm.id)
            .where(ExerciseOrm.id == exercise_id, ExerciseTextOrm.lang == lang, _drawable(lang))
        )
        async with self._session_factory() as session:
            row = (await session.execute(statement)).one_or_none()
        if row is None:
            return None
        exercise, text = row
        return PictureSubject(
            exercise_id=exercise.id,
            name=exercise.name,
            sport_id=exercise.sport_id,
            text=ExerciseText(
                lang=text.lang, instructions=text.instructions, steps=tuple(text.steps)
            ),
        )

    async def has_sha256(self, sha256: str) -> bool:
        statement = select(exists().where(MediaAssetOrm.sha256 == sha256))
        async with self._session_factory() as session:
            return bool((await session.execute(statement)).scalar_one())

    async def add_card_picture(
        self, asset: MediaAsset, exercise_id: UUID, *, lang: str
    ) -> UUID | None:
        asset_id = uuid4()
        lock = func.pg_advisory_xact_lock(func.hashtextextended(str(exercise_id), _LOCK_SEED))
        still_drawable = select(exists().where(ExerciseOrm.id == exercise_id, _drawable(lang)))
        # After the last link, so a removed link's gap is never reused.
        positions = select(func.coalesce(func.max(ExerciseMediaOrm.position) + 1, 0)).where(
            ExerciseMediaOrm.exercise_id == exercise_id
        )
        try:
            async with self._session_factory() as session:
                # Held until commit: a second writer waits here, then finds the first one's link.
                await session.execute(select(lock))
                if not (await session.execute(still_drawable)).scalar_one():
                    return None
                position = (await session.execute(positions)).scalar_one()
                session.add(MediaAssetOrm(**asset.model_dump(exclude={"id"}), id=asset_id))
                # The asset must exist before the link that points at it.
                await session.flush()
                session.add(
                    ExerciseMediaOrm(
                        exercise_id=exercise_id,
                        asset_id=asset_id,
                        position=position,
                        status=MediaLinkStatus.APPROVED.value,
                    )
                )
                await session.commit()
        except SQLAlchemyError as error:
            raise MediaRepositoryError from error
        return asset_id
