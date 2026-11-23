"""The exercise card's drawn picture against real PostgreSQL (ADR-0031 *Picture on the card*,
#643): which exercises may be drawn, the one to draw, and two writers at once keeping one link."""

import asyncio
from collections.abc import AsyncIterator, Callable
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ai_trainer.adapters.card_picture_repository import SqlAlchemyCardPictureRepository
from ai_trainer.adapters.db import build_engine
from ai_trainer.adapters.media_repository import SqlAlchemyExerciseMediaRepository
from ai_trainer.domain.exercises import ExerciseText
from ai_trainer.domain.media import (
    MediaAsset,
    MediaDelivery,
    MediaKind,
    MediaLinkStatus,
    MediaRepositoryError,
    MediaRights,
)
from ai_trainer.domain.media_generation import (
    CARD_PICTURE_FRAGMENT,
    GENERATED_PICTURE_SOURCE,
    PictureSubject,
)
from ai_trainer.settings import Settings

SessionFactory = Callable[[], AsyncSession]
_RACE_SOURCE = "zz-card-race"
_HOW = "Hang from the bar, then pull your chest up to it."


async def _exercise(
    factory: SessionFactory,
    *,
    source: str = "dataset",
    owner: UUID | None = None,
    texts: tuple[tuple[str, str, list[str]], ...] = (("en", _HOW, []),),
) -> UUID:
    exercise_id = uuid4()
    async with factory() as session:
        await session.execute(
            text(
                "INSERT INTO exercises (id, sport_id, owner_user_id, source, source_ref, name, "
                "default_measure) VALUES (:id, 'gym', :owner, :source, :ref, 'Pull-Up', 'reps')"
            ),
            {"id": exercise_id, "owner": owner, "source": source, "ref": f"zz-card-{exercise_id}"},
        )
        for lang, instructions, steps in texts:
            await session.execute(
                text(
                    "INSERT INTO exercise_texts (exercise_id, lang, instructions, steps) "
                    "VALUES (:id, :lang, :instructions, :steps)"
                ),
                {"id": exercise_id, "lang": lang, "instructions": instructions, "steps": steps},
            )
        await session.commit()
    return exercise_id


async def _user(factory: SessionFactory) -> UUID:
    user_id = uuid4()
    async with factory() as session:
        await session.execute(
            text("INSERT INTO users (id, sub, email) VALUES (:id, :sub, :email)"),
            {"id": user_id, "sub": str(user_id), "email": f"{user_id}@example.com"},
        )
        await session.commit()
    return user_id


def _asset(source: str = GENERATED_PICTURE_SOURCE) -> MediaAsset:
    sha256 = uuid4().hex * 2
    return MediaAsset(
        kind=MediaKind.IMAGE,
        rights=MediaRights.LICENSED,
        delivery=MediaDelivery.STORED,
        storage_key=f"exercises/{uuid4()}/{sha256}.png",
        source=source,
        source_ref="model/slug" + CARD_PICTURE_FRAGMENT,
        license="licence",
        attribution="AI generated",
        sha256=sha256,
    )


async def _link(
    media: SqlAlchemyExerciseMediaRepository,
    exercise_id: UUID,
    status: MediaLinkStatus,
    source: str,
) -> None:
    asset_id = await media.add_asset(_asset(source))
    await media.link(exercise_id, asset_id, position=0, status=status)


# AC 5: a custom exercise, a candidate with a picture or one whose drawn picture was removed is
# never drawn; a removed wger picture doesn't block, and no English text leaves nothing to draw.
async def test_drawable_excludes_custom_suggested_approved_and_removed_ai_generated(
    db_session_factory: SessionFactory,
) -> None:
    repository = SqlAlchemyCardPictureRepository(db_session_factory)
    media = SqlAlchemyExerciseMediaRepository(db_session_factory)
    plain = await _exercise(db_session_factory)
    steps_only = await _exercise(db_session_factory, texts=(("en", " ", ["Hang.", "Pull."]),))
    removed_wger = await _exercise(db_session_factory)
    await _link(media, removed_wger, MediaLinkStatus.REMOVED, "wger")
    custom = await _exercise(
        db_session_factory, source="custom", owner=await _user(db_session_factory)
    )
    suggested = await _exercise(db_session_factory)
    await _link(media, suggested, MediaLinkStatus.SUGGESTED, "wger")
    approved = await _exercise(db_session_factory)
    await _link(media, approved, MediaLinkStatus.APPROVED, "wger")
    removed_drawn = await _exercise(db_session_factory)
    await _link(media, removed_drawn, MediaLinkStatus.REMOVED, GENERATED_PICTURE_SOURCE)
    german_only = await _exercise(db_session_factory, texts=(("de", "Hängen.", []),))
    blank = await _exercise(db_session_factory, texts=(("en", " ", [" "]),))

    found = await repository.drawable(
        [
            plain,
            steps_only,
            removed_wger,
            custom,
            suggested,
            approved,
            removed_drawn,
            german_only,
            blank,
            uuid4(),
        ],
        lang="en",
    )

    assert found == {plain, steps_only, removed_wger}
    assert await repository.drawable([], lang="en") == set()


async def test_card_subject_is_the_exercise_with_its_english_text_only_when_drawable(
    db_session_factory: SessionFactory,
) -> None:
    repository = SqlAlchemyCardPictureRepository(db_session_factory)
    media = SqlAlchemyExerciseMediaRepository(db_session_factory)
    exercise_id = await _exercise(
        db_session_factory, texts=(("de", "Hängen.", []), ("en", _HOW, ["Hang.", "Pull."]))
    )
    approved = await _exercise(db_session_factory)
    await _link(media, approved, MediaLinkStatus.APPROVED, "wger")

    subject = await repository.card_subject(exercise_id, lang="en")

    assert subject == PictureSubject(
        exercise_id=exercise_id,
        name="Pull-Up",
        sport_id="gym",
        text=ExerciseText(lang="en", instructions=_HOW, steps=("Hang.", "Pull.")),
    )
    assert await repository.card_subject(approved, lang="en") is None
    assert await repository.card_subject(uuid4(), lang="en") is None


async def test_add_card_picture_writes_an_approved_link_once(
    db_session_factory: SessionFactory,
) -> None:
    repository = SqlAlchemyCardPictureRepository(db_session_factory)
    media = SqlAlchemyExerciseMediaRepository(db_session_factory)
    exercise_id = await _exercise(db_session_factory)
    # A removed wger link keeps its position, so the drawn one goes after it.
    await _link(media, exercise_id, MediaLinkStatus.REMOVED, "wger")
    asset = _asset()

    asset_id = await repository.add_card_picture(asset, exercise_id, lang="en")
    second = await repository.add_card_picture(_asset(), exercise_id, lang="en")

    assert asset_id is not None
    assert second is None
    [item] = await media.approved_for(exercise_id)
    assert item.position == 1
    assert item.asset == asset.model_copy(update={"id": asset_id})
    assert await repository.has_sha256(asset.sha256 or "")
    assert await repository.drawable([exercise_id], lang="en") == set()


async def test_a_failed_card_write_raises_and_writes_nothing(
    db_session_factory: SessionFactory,
) -> None:
    repository = SqlAlchemyCardPictureRepository(db_session_factory)
    exercise_id = await _exercise(db_session_factory)
    taken = _asset()
    await SqlAlchemyExerciseMediaRepository(db_session_factory).add_asset(taken)
    # The same bytes break the unique `sha256` index.
    duplicate = _asset().model_copy(update={"sha256": taken.sha256})

    with pytest.raises(MediaRepositoryError):
        await repository.add_card_picture(duplicate, exercise_id, lang="en")

    assert await repository.drawable([exercise_id], lang="en") == {exercise_id}


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    """Its own engine: the race needs two connections that commit for real, which the shared
    test connection can't give. It deletes what it wrote by its exercise source."""
    engine = build_engine(str(Settings().database_url))
    try:
        yield engine
    finally:
        async with engine.begin() as connection:
            ids = await connection.execute(
                text(
                    "SELECT em.asset_id FROM exercise_media em JOIN exercises e "
                    "ON e.id = em.exercise_id WHERE e.source = :s"
                ),
                {"s": _RACE_SOURCE},
            )
            asset_ids = [row[0] for row in ids]
            await connection.execute(
                text("DELETE FROM exercises WHERE source = :s"), {"s": _RACE_SOURCE}
            )
            await connection.execute(
                text("DELETE FROM media_assets WHERE id = ANY(:ids)"), {"ids": asset_ids}
            )
        await engine.dispose()


# AC 4: two cards drawing at once keep one link; the loser gets None and writes nothing.
async def test_concurrent_card_writes_keep_one_link(engine: AsyncEngine) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    exercise_id = await _exercise(factory, source=_RACE_SOURCE)
    repository = SqlAlchemyCardPictureRepository(factory)
    first, second = _asset(), _asset()

    results = await asyncio.gather(
        repository.add_card_picture(first, exercise_id, lang="en"),
        repository.add_card_picture(second, exercise_id, lang="en"),
    )

    [winner] = [result for result in results if result is not None]
    assert results.count(None) == 1
    [item] = await SqlAlchemyExerciseMediaRepository(factory).approved_for(exercise_id)
    assert item.asset.id == winner
    loser = second if results[0] == winner else first
    assert not await repository.has_sha256(loser.sha256 or "")
