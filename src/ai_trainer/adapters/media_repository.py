from collections.abc import Callable, Sequence
from uuid import UUID, uuid4

from sqlalchemy import ColumnElement, exists, false, func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.orm import ExerciseMediaOrm, MediaAssetOrm
from ai_trainer.domain.media import (
    ExerciseMediaItem,
    MediaAsset,
    MediaDelivery,
    MediaKind,
    MediaLinkStatus,
    MediaRepositoryError,
    MediaRights,
)


def _to_domain(row: MediaAssetOrm) -> MediaAsset:
    return MediaAsset(
        id=row.id,
        kind=MediaKind(row.kind),
        rights=MediaRights(row.rights),
        delivery=MediaDelivery(row.delivery),
        storage_key=row.storage_key,
        embed_url=row.embed_url,
        source=row.source,
        source_ref=row.source_ref,
        license=row.license,
        attribution=row.attribution,
        sha256=row.sha256,
        width=row.width,
        height=row.height,
    )


class SqlAlchemyExerciseMediaRepository:
    """Implements `ExerciseMediaRepositoryPort`. Catalogue media holds no user data, so it takes
    no `user_id`; the exercise ID it is given comes from a user-scoped lookup (ADR-0031)."""

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add_asset(self, asset: MediaAsset) -> UUID:
        asset_id = uuid4()
        row = MediaAssetOrm(**asset.model_dump(exclude={"id"}), id=asset_id)
        async with self._session_factory() as session:
            session.add(row)
            await session.commit()
        return asset_id

    async def link(
        self, exercise_id: UUID, asset_id: UUID, *, position: int, status: MediaLinkStatus
    ) -> None:
        async with self._session_factory() as session:
            session.add(
                ExerciseMediaOrm(
                    exercise_id=exercise_id,
                    asset_id=asset_id,
                    position=position,
                    status=status.value,
                )
            )
            await session.commit()

    async def has_sha256(self, sha256: str) -> bool:
        statement = select(exists().where(MediaAssetOrm.sha256 == sha256))
        async with self._session_factory() as session:
            return bool((await session.execute(statement)).scalar_one())

    async def add_suggested(self, asset: MediaAsset, exercise_id: UUID) -> UUID:
        asset_id = uuid4()
        # After the last link, so a removed link's gap is never reused.
        positions = select(func.coalesce(func.max(ExerciseMediaOrm.position) + 1, 0)).where(
            ExerciseMediaOrm.exercise_id == exercise_id
        )
        try:
            async with self._session_factory() as session:
                position = (await session.execute(positions)).scalar_one()
                session.add(MediaAssetOrm(**asset.model_dump(exclude={"id"}), id=asset_id))
                # The asset must exist before the link that points at it.
                await session.flush()
                session.add(
                    ExerciseMediaOrm(
                        exercise_id=exercise_id,
                        asset_id=asset_id,
                        position=position,
                        status=MediaLinkStatus.SUGGESTED.value,
                    )
                )
                await session.commit()
        except SQLAlchemyError as error:
            raise MediaRepositoryError from error
        return asset_id

    async def approved_for(self, exercise_id: UUID) -> Sequence[ExerciseMediaItem]:
        statement = (
            select(MediaAssetOrm, ExerciseMediaOrm.position)
            .join(ExerciseMediaOrm, ExerciseMediaOrm.asset_id == MediaAssetOrm.id)
            .where(
                ExerciseMediaOrm.exercise_id == exercise_id,
                ExerciseMediaOrm.status == MediaLinkStatus.APPROVED.value,
            )
            .order_by(ExerciseMediaOrm.position, ExerciseMediaOrm.id)
        )
        async with self._session_factory() as session:
            rows = (await session.execute(statement)).all()
        return [ExerciseMediaItem(asset=_to_domain(asset), position=pos) for asset, pos in rows]

    async def is_removed(self, *, sha256: str | None, embed_url: str | None) -> bool:
        matches: list[ColumnElement[bool]] = []
        if sha256 is not None:
            matches.append(MediaAssetOrm.sha256 == sha256)
        if embed_url is not None:
            matches.append(MediaAssetOrm.embed_url == embed_url)
        statement = select(
            exists().where(
                ExerciseMediaOrm.asset_id == MediaAssetOrm.id,
                ExerciseMediaOrm.status == MediaLinkStatus.REMOVED.value,
                or_(false(), *matches),
            )
        )
        async with self._session_factory() as session:
            return bool((await session.execute(statement)).scalar_one())
