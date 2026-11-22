"""The exercise card reads only approved media, and an importer skips a removed picture
(ADR-0031 "Media links", invariant 5, F24, G10)."""

from collections.abc import Callable
from datetime import UTC, datetime
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
REMOVED_AT = datetime(2026, 11, 12, 9, 30, tzinfo=UTC)


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


async def _custom_exercise(factory: SessionFactory) -> UUID:
    exercise_id = uuid4()
    user_id = uuid4()
    async with factory() as session:
        await session.execute(
            text(
                "INSERT INTO users (id, sub, email, name, locale, status) "
                "VALUES (:id, :sub, :email, 'A', 'en', 'active')"
            ),
            {"id": user_id, "sub": f"media-{user_id}", "email": f"media-{user_id}@example.com"},
        )
        await session.execute(
            text(
                "INSERT INTO exercises (id, sport_id, owner_user_id, source, name, default_measure)"
                " VALUES (:id, 'gym', :user, 'custom', 'Mine', 'reps')"
            ),
            {"id": exercise_id, "user": user_id},
        )
        await session.commit()
    return exercise_id


def _generated() -> MediaAsset:
    return _stored().model_copy(
        update={
            "source": "ai-generated",
            "source_ref": "google/gemini-3.1-flash-lite-image-20260630",
            "attribution": "AI generated with Gemini 3.1 Flash Lite Image (Google) via OpenRouter",
        }
    )


async def _linked(
    repository: SqlAlchemyExerciseMediaRepository,
    exercise_id: UUID,
    asset: MediaAsset,
    status: MediaLinkStatus = MediaLinkStatus.SUGGESTED,
    position: int = 0,
) -> UUID:
    asset_id = await repository.add_asset(asset)
    await repository.link(exercise_id, asset_id, position=position, status=status)
    return asset_id


async def test_approve_sets_a_suggested_link_approved(
    db_session_factory: SessionFactory, repository: SqlAlchemyExerciseMediaRepository
) -> None:
    exercise_id = await _exercise(db_session_factory)
    picture = _stored()
    await _linked(repository, exercise_id, picture)
    [row] = await repository.review_links(MediaLinkStatus.SUGGESTED, exercise_id=exercise_id)

    assert await repository.approve(row.link_id)

    assert [item.asset.sha256 for item in await repository.approved_for(exercise_id)] == [
        picture.sha256
    ]
    assert await repository.review_links(MediaLinkStatus.SUGGESTED, exercise_id=exercise_id) == []


async def test_approve_returns_false_when_not_suggested(
    db_session_factory: SessionFactory, repository: SqlAlchemyExerciseMediaRepository
) -> None:
    exercise_id = await _exercise(db_session_factory)
    await _linked(repository, exercise_id, _stored(), MediaLinkStatus.APPROVED, 0)
    await _linked(repository, exercise_id, _stored(), MediaLinkStatus.REMOVED, 1)
    [approved] = await repository.review_links(MediaLinkStatus.APPROVED, exercise_id=exercise_id)
    [removed] = await repository.review_links(MediaLinkStatus.REMOVED, exercise_id=exercise_id)

    assert not await repository.approve(approved.link_id)
    assert not await repository.approve(removed.link_id)
    assert not await repository.approve(uuid4())

    still_removed = await repository.review_link(removed.link_id)
    assert still_removed is not None
    assert still_removed.status is MediaLinkStatus.REMOVED
    assert await repository.review_link(uuid4()) is None


async def test_approve_wger_suggestions_skips_other_sources_and_other_exercises(
    db_session_factory: SessionFactory, repository: SqlAlchemyExerciseMediaRepository
) -> None:
    exercise_id = await _exercise(db_session_factory)
    other_id = await _exercise(db_session_factory)
    first, second, generated, embedded, elsewhere = (
        _stored(),
        _stored(),
        _generated(),
        _embedded(),
        _stored(),
    )
    for position, asset in enumerate((first, second, generated, embedded)):
        await _linked(repository, exercise_id, asset, position=position)
    await _linked(repository, other_id, elsewhere)
    # Every ID posted, even ones that must not change: the generated and embedded links, and a
    # wger link on another exercise.
    posted = [
        row.link_id
        for exercise in (exercise_id, other_id)
        for row in await repository.review_links(MediaLinkStatus.SUGGESTED, exercise_id=exercise)
    ]

    assert await repository.approve_suggested_from(exercise_id, "wger", posted) == 2
    assert await repository.approve_suggested_from(exercise_id, "wger", posted) == 0

    assert {item.asset.sha256 for item in await repository.approved_for(exercise_id)} == {
        first.sha256,
        second.sha256,
    }
    left = await repository.review_links(MediaLinkStatus.SUGGESTED, exercise_id=exercise_id)
    assert [row.asset.source for row in left] == ["ai-generated", "youtube"]
    assert await repository.approved_for(other_id) == []


async def test_bulk_approve_leaves_a_wger_suggestion_added_after_the_page_was_drawn(
    db_session_factory: SessionFactory, repository: SqlAlchemyExerciseMediaRepository
) -> None:
    exercise_id = await _exercise(db_session_factory)
    seen = _stored()
    await _linked(repository, exercise_id, seen)
    drawn = [
        row.link_id
        for row in await repository.review_links(MediaLinkStatus.SUGGESTED, exercise_id=exercise_id)
    ]
    late = _stored()
    await _linked(repository, exercise_id, late, position=1)

    assert await repository.approve_suggested_from(exercise_id, "wger", drawn) == 1
    assert await repository.approve_suggested_from(exercise_id, "wger", []) == 0

    [left] = await repository.review_links(MediaLinkStatus.SUGGESTED, exercise_id=exercise_id)
    assert left.asset.sha256 == late.sha256
    assert [item.asset.sha256 for item in await repository.approved_for(exercise_id)] == [
        seen.sha256
    ]


async def test_review_links_lists_catalogue_links_by_exercise_then_position(
    db_session_factory: SessionFactory, repository: SqlAlchemyExerciseMediaRepository
) -> None:
    exercise_id = await _exercise(db_session_factory)
    custom_id = await _custom_exercise(db_session_factory)
    later, earlier = _stored(), _embedded()
    await _linked(repository, exercise_id, later, position=3)
    await _linked(repository, exercise_id, earlier, position=1)
    await _linked(repository, exercise_id, _stored(), MediaLinkStatus.APPROVED, 2)
    await _linked(repository, custom_id, _stored())

    rows = await repository.review_links(MediaLinkStatus.SUGGESTED)

    mine = [row for row in rows if row.exercise_id == exercise_id]
    assert [(row.position, row.asset.model_copy(update={"id": None})) for row in mine] == [
        (1, earlier),
        (3, later),
    ]
    assert all(row.exercise_name == "Row" for row in mine)
    assert all(row.status is MediaLinkStatus.SUGGESTED for row in mine)
    assert all(row.exercise_id != custom_id for row in rows)
    assert await repository.review_links(MediaLinkStatus.SUGGESTED, exercise_id=custom_id) == []


async def test_status_counts_count_catalogue_links_per_status(
    db_session_factory: SessionFactory, repository: SqlAlchemyExerciseMediaRepository
) -> None:
    before = await repository.status_counts()
    exercise_id = await _exercise(db_session_factory)
    custom_id = await _custom_exercise(db_session_factory)
    await _linked(repository, exercise_id, _stored(), position=0)
    await _linked(repository, exercise_id, _stored(), position=1)
    await _linked(repository, exercise_id, _stored(), MediaLinkStatus.APPROVED, 2)
    await _linked(repository, custom_id, _stored())

    after = await repository.status_counts()

    assert {status: after[status] - before[status] for status in MediaLinkStatus} == {
        MediaLinkStatus.SUGGESTED: 2,
        MediaLinkStatus.APPROVED: 1,
        MediaLinkStatus.REMOVED: 0,
    }


async def _link_id(
    repository: SqlAlchemyExerciseMediaRepository, exercise_id: UUID, asset_id: UUID
) -> UUID:
    rows = [
        row
        for status in MediaLinkStatus
        for row in await repository.review_links(status, exercise_id=exercise_id)
    ]
    return next(row.link_id for row in rows if row.asset.id == asset_id)


@pytest.mark.parametrize("status", [MediaLinkStatus.APPROVED, MediaLinkStatus.SUGGESTED])
async def test_remove_hides_link_from_approved_for_and_records_earlier_status(
    db_session_factory: SessionFactory,
    repository: SqlAlchemyExerciseMediaRepository,
    status: MediaLinkStatus,
) -> None:
    exercise_id = await _exercise(db_session_factory)
    asset_id = await _linked(repository, exercise_id, _stored(), status)
    link_id = await _link_id(repository, exercise_id, asset_id)

    assert await repository.remove(link_id, at=REMOVED_AT) is status

    assert await repository.approved_for(exercise_id) == []
    row = await repository.review_link(link_id)
    assert row is not None
    assert (row.status, row.removed_from, row.removed_at) == (
        MediaLinkStatus.REMOVED,
        status,
        REMOVED_AT,
    )
    [listed] = await repository.review_links(MediaLinkStatus.REMOVED, exercise_id=exercise_id)
    assert listed.removed_at == REMOVED_AT
    # Removing it again changes nothing: the first removal's time and status are kept.
    assert await repository.remove(link_id, at=datetime(2026, 11, 13, tzinfo=UTC)) is None
    again = await repository.review_link(link_id)
    assert again is not None
    assert (again.removed_from, again.removed_at) == (status, REMOVED_AT)


async def test_a_removed_pictures_sha256_stays_known_to_the_importers(
    db_session_factory: SessionFactory, repository: SqlAlchemyExerciseMediaRepository
) -> None:
    exercise_id = await _exercise(db_session_factory)
    picture = _stored()
    asset_id = await _linked(repository, exercise_id, picture, MediaLinkStatus.APPROVED)

    await repository.remove(await _link_id(repository, exercise_id, asset_id), at=REMOVED_AT)

    assert picture.sha256 is not None
    assert await repository.has_sha256(picture.sha256)
    assert await repository.is_removed(sha256=picture.sha256, embed_url=None)


@pytest.mark.parametrize("status", [MediaLinkStatus.APPROVED, MediaLinkStatus.SUGGESTED])
async def test_restore_puts_back_removed_from_and_clears_removal(
    db_session_factory: SessionFactory,
    repository: SqlAlchemyExerciseMediaRepository,
    status: MediaLinkStatus,
) -> None:
    exercise_id = await _exercise(db_session_factory)
    asset_id = await _linked(repository, exercise_id, _stored(), status)
    link_id = await _link_id(repository, exercise_id, asset_id)
    await repository.remove(link_id, at=REMOVED_AT)

    assert await repository.restore(link_id) is status

    row = await repository.review_link(link_id)
    assert row is not None
    assert (row.status, row.removed_from, row.removed_at) == (status, None, None)
    assert await repository.restore(link_id) is None


async def test_restore_of_a_removed_link_without_its_earlier_status_makes_it_suggested(
    db_session_factory: SessionFactory, repository: SqlAlchemyExerciseMediaRepository
) -> None:
    exercise_id = await _exercise(db_session_factory)
    asset_id = await _linked(repository, exercise_id, _stored(), MediaLinkStatus.REMOVED)
    link_id = await _link_id(repository, exercise_id, asset_id)

    assert await repository.restore(link_id) is MediaLinkStatus.SUGGESTED


async def test_remove_and_restore_leave_an_athletes_own_exercise_alone(
    db_session_factory: SessionFactory, repository: SqlAlchemyExerciseMediaRepository
) -> None:
    exercise_id = await _custom_exercise(db_session_factory)
    approved = await _linked(repository, exercise_id, _stored(), MediaLinkStatus.APPROVED)
    removed = await _linked(repository, exercise_id, _stored(), MediaLinkStatus.REMOVED, position=1)
    async with db_session_factory() as session:
        result = await session.execute(
            text("SELECT asset_id, id FROM exercise_media WHERE exercise_id = :id"),
            {"id": exercise_id},
        )
        ids: dict[UUID, UUID] = {asset: link for asset, link in result.all()}

    assert await repository.remove(ids[approved], at=REMOVED_AT) is None
    assert await repository.restore(ids[removed]) is None
    assert len(await repository.approved_for(exercise_id)) == 1


async def test_has_live_link_besides_counts_other_suggested_or_approved_links(
    db_session_factory: SessionFactory, repository: SqlAlchemyExerciseMediaRepository
) -> None:
    first = await _exercise(db_session_factory)
    second = await _exercise(db_session_factory)
    asset_id = await repository.add_asset(_stored())
    await repository.link(first, asset_id, position=0, status=MediaLinkStatus.APPROVED)
    await repository.link(second, asset_id, position=0, status=MediaLinkStatus.SUGGESTED)
    link_id = await _link_id(repository, first, asset_id)
    other_id = await _link_id(repository, second, asset_id)

    assert await repository.has_live_link_besides(asset_id, link_id)
    await repository.remove(other_id, at=REMOVED_AT)
    assert not await repository.has_live_link_besides(asset_id, link_id)


async def test_first_approved_pictures_takes_lowest_position_per_exercise(
    db_session_factory: SessionFactory, repository: SqlAlchemyExerciseMediaRepository
) -> None:
    exercise_id = await _exercise(db_session_factory)
    other_id = await _exercise(db_session_factory)
    first, second, others = _stored(), _stored(), _generated()
    for asset, position, on in (
        (second, 3, exercise_id),
        (first, 1, exercise_id),
        (others, 0, other_id),
    ):
        asset_id = await repository.add_asset(asset)
        await repository.link(on, asset_id, position=position, status=MediaLinkStatus.APPROVED)

    found = await repository.first_approved_pictures([exercise_id, other_id])

    assert {key: asset.sha256 for key, asset in found.items()} == {
        exercise_id: first.sha256,
        other_id: others.sha256,
    }


async def test_first_approved_pictures_skips_suggested_and_removed(
    db_session_factory: SessionFactory, repository: SqlAlchemyExerciseMediaRepository
) -> None:
    exercise_id = await _exercise(db_session_factory)
    for asset, status in (
        (_stored(), MediaLinkStatus.SUGGESTED),
        (_stored(), MediaLinkStatus.REMOVED),
    ):
        asset_id = await repository.add_asset(asset)
        await repository.link(exercise_id, asset_id, position=0, status=status)
    # An approved embedded video is no picture for the card.
    video_id = await repository.add_asset(_embedded())
    await repository.link(exercise_id, video_id, position=1, status=MediaLinkStatus.APPROVED)

    assert await repository.first_approved_pictures([exercise_id]) == {}


async def test_first_approved_pictures_reads_only_catalogue_exercises(
    db_session_factory: SessionFactory, repository: SqlAlchemyExerciseMediaRepository
) -> None:
    own_id = await _custom_exercise(db_session_factory)
    asset_id = await repository.add_asset(_stored())
    await repository.link(own_id, asset_id, position=0, status=MediaLinkStatus.APPROVED)

    assert await repository.first_approved_pictures([own_id]) == {}


async def test_first_approved_pictures_of_no_exercise_is_empty(
    repository: SqlAlchemyExerciseMediaRepository,
) -> None:
    assert await repository.first_approved_pictures([]) == {}
