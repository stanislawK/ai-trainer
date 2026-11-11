from collections.abc import Callable, Mapping, Sequence
from uuid import UUID, uuid4

from sqlalchemy import ColumnElement, Select, exists, false, func, or_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.orm import ExerciseMediaOrm, ExerciseOrm, MediaAssetOrm
from ai_trainer.domain.media import (
    ExerciseMediaItem,
    ExerciseMediaReviewRow,
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


def _review_row(link: ExerciseMediaOrm, asset: MediaAssetOrm, name: str) -> ExerciseMediaReviewRow:
    return ExerciseMediaReviewRow(
        link_id=link.id,
        exercise_id=link.exercise_id,
        exercise_name=name,
        position=link.position,
        status=MediaLinkStatus(link.status),
        asset=_to_domain(asset),
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

    def _review_select(self) -> Select[tuple[ExerciseMediaOrm, MediaAssetOrm, str]]:
        return (
            select(ExerciseMediaOrm, MediaAssetOrm, ExerciseOrm.name)
            .join(MediaAssetOrm, ExerciseMediaOrm.asset_id == MediaAssetOrm.id)
            .join(ExerciseOrm, ExerciseMediaOrm.exercise_id == ExerciseOrm.id)
            .where(ExerciseOrm.owner_user_id.is_(None))
        )

    async def review_links(
        self, status: MediaLinkStatus, *, exercise_id: UUID | None = None
    ) -> Sequence[ExerciseMediaReviewRow]:
        statement = (
            self._review_select()
            .where(ExerciseMediaOrm.status == status.value)
            .order_by(
                ExerciseOrm.name, ExerciseOrm.id, ExerciseMediaOrm.position, ExerciseMediaOrm.id
            )
        )
        if exercise_id is not None:
            statement = statement.where(ExerciseMediaOrm.exercise_id == exercise_id)
        async with self._session_factory() as session:
            rows = (await session.execute(statement)).all()
        return [_review_row(link, asset, name) for link, asset, name in rows]

    async def review_link(self, link_id: UUID) -> ExerciseMediaReviewRow | None:
        statement = self._review_select().where(ExerciseMediaOrm.id == link_id)
        async with self._session_factory() as session:
            row = (await session.execute(statement)).one_or_none()
        return None if row is None else _review_row(*row)

    async def status_counts(self) -> Mapping[MediaLinkStatus, int]:
        statement = (
            select(ExerciseMediaOrm.status, func.count())
            .join(ExerciseOrm, ExerciseMediaOrm.exercise_id == ExerciseOrm.id)
            .where(ExerciseOrm.owner_user_id.is_(None))
            .group_by(ExerciseMediaOrm.status)
        )
        async with self._session_factory() as session:
            found = {status: count for status, count in (await session.execute(statement)).all()}
        return {status: found.get(status.value, 0) for status in MediaLinkStatus}

    async def approve(self, link_id: UUID) -> bool:
        return bool(await self._approve_where(ExerciseMediaOrm.id == link_id))

    async def approve_suggested_from(self, exercise_id: UUID, source: str) -> int:
        return await self._approve_where(
            ExerciseMediaOrm.exercise_id == exercise_id,
            ExerciseMediaOrm.asset_id.in_(
                select(MediaAssetOrm.id).where(MediaAssetOrm.source == source)
            ),
        )

    async def _approve_where(self, *conditions: ColumnElement[bool]) -> int:
        # Conditional on `suggested` in the same statement, so a link approved or removed since
        # the page was drawn is left as it is.
        statement = (
            update(ExerciseMediaOrm)
            .where(
                *conditions,
                ExerciseMediaOrm.status == MediaLinkStatus.SUGGESTED.value,
                ExerciseMediaOrm.exercise_id.in_(
                    select(ExerciseOrm.id).where(ExerciseOrm.owner_user_id.is_(None))
                ),
            )
            .values(status=MediaLinkStatus.APPROVED.value)
            .returning(ExerciseMediaOrm.id)
        )
        async with self._session_factory() as session:
            changed = len((await session.execute(statement)).all())
            await session.commit()
        return changed

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
