"""Storing a generated picture against real PostgreSQL (ADR-0031, #535), with an in-memory
bucket: the asset's checked fields, the `suggested` link the card query ignores, a second run's
extra link, and what a failure on either side leaves behind."""

import hashlib
from collections.abc import AsyncIterator
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ai_trainer.adapters.db import build_engine, build_session_factory
from ai_trainer.adapters.exercise_repository import SqlAlchemyExerciseRepository
from ai_trainer.adapters.media_repository import SqlAlchemyExerciseMediaRepository
from ai_trainer.application.exercise_picture_store import StoreExercisePicture
from ai_trainer.domain.exercises import CatalogueExercise, ExerciseMeasure, ExerciseText
from ai_trainer.domain.media_generation import (
    GeneratedImage,
    PictureBrief,
    PictureMade,
    PictureStoreFailed,
    StoredPicture,
)
from ai_trainer.settings import Settings
from tests.picture_support import FrozenClock, InMemoryBucket

_SOURCE = "zz-pic-store"
_BRIEF = PictureBrief(exercise_name="Zz", start_position="a", end_position="b", image_prompt="c")


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
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
                {"s": _SOURCE},
            )
            asset_ids = [row[0] for row in ids]
            await connection.execute(
                text("DELETE FROM exercises WHERE source = :s"), {"s": _SOURCE}
            )
            await connection.execute(
                text("DELETE FROM media_assets WHERE id = ANY(:ids)"), {"ids": asset_ids}
            )
        await engine.dispose()


@pytest.fixture
async def exercise_id(engine: AsyncEngine) -> UUID:
    name = f"Zz Pic Store {uuid4()}"
    await SqlAlchemyExerciseRepository(build_session_factory(engine)).upsert_catalogue(
        [
            CatalogueExercise(
                sport_id="gym",
                source=_SOURCE,
                source_ref=name,
                name=name,
                equipment="body weight",
                body_part="back",
                target="lats",
                secondary_muscles=(),
                default_measure=ExerciseMeasure.REPS,
                attribution="test",
                texts=(ExerciseText(lang="en", instructions="Hang, then pull.", steps=()),),
            )
        ]
    )
    async with engine.connect() as connection:
        found = await connection.execute(
            text("SELECT id FROM exercises WHERE source = :s AND name = :n"),
            {"s": _SOURCE, "n": name},
        )
        return UUID(str(found.scalar_one()))


def _made(exercise_id: UUID, data: bytes) -> PictureMade:
    return PictureMade(
        exercise_id=exercise_id,
        brief=_BRIEF,
        image=GeneratedImage(data=data, media_type="image/jpeg"),
        spent_usd=Decimal("0.0336"),
    )


def _use_case(engine: AsyncEngine, bucket: InMemoryBucket) -> StoreExercisePicture:
    return StoreExercisePicture(
        storage=bucket,
        media=SqlAlchemyExerciseMediaRepository(build_session_factory(engine)),
        clock=FrozenClock(),
        model="google/gemini-3.1-flash-lite-image-20260630",
    )


async def _rows(engine: AsyncEngine, exercise_id: UUID) -> list[dict[str, object]]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT em.status, em.position, a.* FROM exercise_media em "
                "JOIN media_assets a ON a.id = em.asset_id WHERE em.exercise_id = :id "
                "ORDER BY em.position"
            ),
            {"id": exercise_id},
        )
        return [dict(row) for row in rows.mappings()]


async def test_stored_asset_has_sha256_and_key_and_no_embed_url(
    engine: AsyncEngine, exercise_id: UUID
) -> None:
    data = f"jpeg-{uuid4()}".encode()
    bucket = InMemoryBucket()

    result = await _use_case(engine, bucket).store(_made(exercise_id, data))

    assert isinstance(result, StoredPicture)
    [row] = await _rows(engine, exercise_id)
    assert row["sha256"] == hashlib.sha256(data).hexdigest()
    assert row["storage_key"] == f"exercises/{exercise_id}/{row['sha256']}.jpg"
    assert row["embed_url"] is None
    assert (row["kind"], row["rights"], row["delivery"]) == ("image", "licensed", "stored")
    assert row["source"] == "ai-generated"
    assert bucket.objects == {str(row["storage_key"]): data}


async def test_link_is_suggested_and_the_card_query_does_not_return_it(
    engine: AsyncEngine, exercise_id: UUID
) -> None:
    await _use_case(engine, InMemoryBucket()).store(_made(exercise_id, f"a-{uuid4()}".encode()))

    [row] = await _rows(engine, exercise_id)
    assert row["status"] == "suggested"
    media = SqlAlchemyExerciseMediaRepository(build_session_factory(engine))
    assert await media.approved_for(exercise_id) == []


async def test_second_run_adds_another_suggested_link(
    engine: AsyncEngine, exercise_id: UUID
) -> None:
    store = _use_case(engine, InMemoryBucket())
    await store.store(_made(exercise_id, f"one-{uuid4()}".encode()))
    await store.store(_made(exercise_id, f"two-{uuid4()}".encode()))

    rows = await _rows(engine, exercise_id)
    assert [(row["status"], row["position"]) for row in rows] == [
        ("suggested", 0),
        ("suggested", 1),
    ]


async def test_a_new_link_goes_after_the_last_position_even_past_a_gap(
    engine: AsyncEngine, exercise_id: UUID
) -> None:
    store = _use_case(engine, InMemoryBucket())
    await store.store(_made(exercise_id, f"one-{uuid4()}".encode()))
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE exercise_media SET position = 2 WHERE exercise_id = :id"),
            {"id": exercise_id},
        )

    await store.store(_made(exercise_id, f"two-{uuid4()}".encode()))

    rows = await _rows(engine, exercise_id)
    assert [row["position"] for row in rows] == [2, 3]


async def test_storage_failure_leaves_no_asset_and_no_link(
    engine: AsyncEngine, exercise_id: UUID
) -> None:
    result = await _use_case(engine, InMemoryBucket(fail_put=True)).store(
        _made(exercise_id, f"x-{uuid4()}".encode())
    )

    assert result == PictureStoreFailed(reason="storage_error", spent_usd=Decimal("0.0336"))
    assert await _rows(engine, exercise_id) == []


async def test_database_failure_deletes_the_uploaded_object(engine: AsyncEngine) -> None:
    """A link to an exercise that doesn't exist fails the transaction: no asset row remains."""
    bucket = InMemoryBucket()
    missing = uuid4()

    result = await _use_case(engine, bucket).store(_made(missing, f"y-{uuid4()}".encode()))

    assert result == PictureStoreFailed(reason="database_error", spent_usd=Decimal("0.0336"))
    assert bucket.objects == {}
    async with engine.connect() as connection:
        left = await connection.execute(
            text("SELECT count(*) FROM media_assets WHERE storage_key LIKE :k"),
            {"k": f"exercises/{missing}/%"},
        )
        assert left.scalar_one() == 0
