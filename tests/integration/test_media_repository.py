"""The exercise card reads only approved media, and an importer skips a removed picture
(ADR-0031 "Media links", invariant 5, F24, G10)."""

from collections.abc import Callable
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.media_repository import SqlAlchemyExerciseMediaRepository
from ai_trainer.domain.media import (
    MediaAsset,
    MediaDelivery,
    MediaKind,
    MediaLinkStatus,
    MediaRights,
)

SessionFactory = Callable[[], AsyncSession]


@pytest.fixture
def repository(db_session_factory: SessionFactory) -> SqlAlchemyExerciseMediaRepository:
    return SqlAlchemyExerciseMediaRepository(db_session_factory)


async def _exercise(factory: SessionFactory) -> UUID:
    exercise_id = uuid4()
    async with factory() as session:
        await session.execute(
            text(
                "INSERT INTO exercises (id, sport_id, source, source_ref, name, default_measure) "
                "VALUES (:id, 'gym', 'dataset', :ref, 'Row', 'reps')"
            ),
            {"id": exercise_id, "ref": f"zz-media-{exercise_id}"},
        )
        await session.commit()
    return exercise_id


def _stored() -> MediaAsset:
    return MediaAsset(
        kind=MediaKind.IMAGE,
        rights=MediaRights.LICENSED,
        delivery=MediaDelivery.STORED,
        storage_key=f"wger/{uuid4()}.png",
        source="wger",
        source_ref=f"https://wger.de/api/v2/exerciseimage/{uuid4()}/",
        license="CC BY-SA 4.0",
        attribution="Author · CC BY-SA 4.0 — via wger.de",
        sha256=uuid4().hex * 2,
        width=800,
        height=600,
    )


def _embedded() -> MediaAsset:
    return MediaAsset(
        kind=MediaKind.VIDEO,
        rights=MediaRights.UNSTATED,
        delivery=MediaDelivery.EMBEDDED,
        embed_url=f"https://www.youtube.com/embed/{uuid4()}",
        source="youtube",
        source_ref="abc",
    )


async def test_approved_for_returns_only_approved_links(
    db_session_factory: SessionFactory, repository: SqlAlchemyExerciseMediaRepository
) -> None:
    exercise_id = await _exercise(db_session_factory)
    other_id = await _exercise(db_session_factory)
    second, first, suggested, removed, elsewhere = (
        _stored(),
        _embedded(),
        _stored(),
        _stored(),
        _stored(),
    )
    for asset, position, status, on in (
        (second, 2, MediaLinkStatus.APPROVED, exercise_id),
        (first, 1, MediaLinkStatus.APPROVED, exercise_id),
        (suggested, 0, MediaLinkStatus.SUGGESTED, exercise_id),
        (removed, 0, MediaLinkStatus.REMOVED, exercise_id),
        (elsewhere, 0, MediaLinkStatus.APPROVED, other_id),
    ):
        asset_id = await repository.add_asset(asset)
        await repository.link(on, asset_id, position=position, status=status)

    items = await repository.approved_for(exercise_id)

    assert [(item.position, item.asset.model_copy(update={"id": None})) for item in items] == [
        (1, first),
        (2, second),
    ]
    assert all(item.asset.id is not None for item in items)


async def test_approved_for_is_empty_for_an_exercise_without_media(
    db_session_factory: SessionFactory, repository: SqlAlchemyExerciseMediaRepository
) -> None:
    assert await repository.approved_for(await _exercise(db_session_factory)) == []


async def test_is_removed_reports_removed_sha256_and_embed_url(
    db_session_factory: SessionFactory, repository: SqlAlchemyExerciseMediaRepository
) -> None:
    exercise_id = await _exercise(db_session_factory)
    removed_stored, removed_embedded, approved, suggested = (
        _stored(),
        _embedded(),
        _stored(),
        _embedded(),
    )
    for asset, status in (
        (removed_stored, MediaLinkStatus.REMOVED),
        (removed_embedded, MediaLinkStatus.REMOVED),
        (approved, MediaLinkStatus.APPROVED),
        (suggested, MediaLinkStatus.SUGGESTED),
    ):
        asset_id = await repository.add_asset(asset)
        await repository.link(exercise_id, asset_id, position=0, status=status)
    # Removed from one exercise is removed everywhere: takedown is about the picture.
    other_id = await _exercise(db_session_factory)
    shared = _stored()
    shared_id = await repository.add_asset(shared)
    await repository.link(exercise_id, shared_id, position=1, status=MediaLinkStatus.APPROVED)
    await repository.link(other_id, shared_id, position=0, status=MediaLinkStatus.REMOVED)

    assert await repository.is_removed(sha256=removed_stored.sha256, embed_url=None)
    assert await repository.is_removed(sha256=None, embed_url=removed_embedded.embed_url)
    assert await repository.is_removed(sha256=shared.sha256, embed_url=None)
    assert not await repository.is_removed(sha256=approved.sha256, embed_url=None)
    assert not await repository.is_removed(sha256=None, embed_url=suggested.embed_url)
    assert not await repository.is_removed(sha256="c" * 64, embed_url="https://example.com/x")
    assert not await repository.is_removed(sha256=None, embed_url=None)
