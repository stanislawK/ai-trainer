"""Storing a generated picture (ADR-0031 *Fallback media source*, #535): the key, the asset's
fields and the attribution, and what a storage or database failure leaves behind. Fakes only."""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from uuid import UUID, uuid4

from ai_trainer.application.exercise_picture_store import StoreExercisePicture
from ai_trainer.domain.media import (
    ExerciseMediaItem,
    MediaAsset,
    MediaDelivery,
    MediaKind,
    MediaLinkStatus,
    MediaRepositoryError,
    MediaRights,
)
from ai_trainer.domain.media_generation import (
    GeneratedImage,
    PictureBrief,
    PictureMade,
    PictureStoreFailed,
    StoredPicture,
)
from tests.picture_support import FrozenClock, InMemoryBucket

_EXERCISE_ID = UUID("00000000-0000-4000-8000-0000000c0535")
_MODEL = "google/gemini-3.1-flash-lite-image-20260630"
_BRIEF = PictureBrief(
    exercise_name="Pull-Up", start_position="a", end_position="b", image_prompt="c"
)


def _made(data: bytes = b"\xff\xd8\xff-jpeg", media_type: str = "image/jpeg") -> PictureMade:
    image = GeneratedImage(data=data, media_type=media_type)  # type: ignore[arg-type]
    return PictureMade(
        exercise_id=_EXERCISE_ID, brief=_BRIEF, image=image, spent_usd=Decimal("0.0336")
    )


@dataclass
class FakeMedia:
    assets: list[tuple[MediaAsset, UUID]] = field(default_factory=list)
    fail: bool = False

    async def add_asset(self, asset: MediaAsset) -> UUID:
        raise NotImplementedError

    async def link(
        self, exercise_id: UUID, asset_id: UUID, *, position: int, status: MediaLinkStatus
    ) -> None:
        raise NotImplementedError

    async def approved_for(self, exercise_id: UUID) -> Sequence[ExerciseMediaItem]:
        return []

    async def is_removed(self, *, sha256: str | None, embed_url: str | None) -> bool:
        return False

    async def has_sha256(self, sha256: str) -> bool:
        return any(asset.sha256 == sha256 for asset, _ in self.assets)

    async def add_suggested(self, asset: MediaAsset, exercise_id: UUID) -> UUID:
        if self.fail:
            raise MediaRepositoryError
        self.assets.append((asset, exercise_id))
        return uuid4()


def _use_case(bucket: InMemoryBucket, media: FakeMedia) -> StoreExercisePicture:
    return StoreExercisePicture(storage=bucket, media=media, clock=FrozenClock(), model=_MODEL)


async def test_asset_fields_key_and_attribution() -> None:
    bucket, media = InMemoryBucket(), FakeMedia()
    made = _made()

    result = await _use_case(bucket, media).store(made)

    digest = hashlib.sha256(made.image.data).hexdigest()
    key = f"exercises/{_EXERCISE_ID}/{digest}.jpg"
    assert isinstance(result, StoredPicture)
    assert bucket.objects == {key: made.image.data}
    assert bucket.content_types[key] == "image/jpeg"
    [(asset, exercise_id)] = media.assets
    assert exercise_id == _EXERCISE_ID
    assert (asset.kind, asset.rights, asset.delivery) == (
        MediaKind.IMAGE,
        MediaRights.LICENSED,
        MediaDelivery.STORED,
    )
    assert (asset.source, asset.source_ref) == ("ai-generated", _MODEL)
    assert asset.sha256 == digest
    assert asset.storage_key == key
    assert asset.embed_url is None
    assert asset.attribution == (
        "AI generated with Gemini 3.1 Flash Lite Image (Google) via OpenRouter · 2026-11-11"
    )
    assert asset.license is not None
    assert "Google" in asset.license


async def test_extension_follows_the_media_type() -> None:
    bucket = InMemoryBucket()
    await _use_case(bucket, FakeMedia()).store(_made(b"png", "image/png"))
    await _use_case(bucket, FakeMedia()).store(_made(b"webp", "image/webp"))
    assert sorted(key.rsplit(".", 1)[1] for key in bucket.objects) == ["png", "webp"]


async def test_storage_failure_leaves_no_asset_and_no_link() -> None:
    bucket, media = InMemoryBucket(fail_put=True), FakeMedia()

    result = await _use_case(bucket, media).store(_made())

    assert result == PictureStoreFailed(reason="storage_error", spent_usd=Decimal("0.0336"))
    assert media.assets == []
    assert bucket.objects == {}


async def test_database_failure_deletes_the_uploaded_object() -> None:
    bucket, media = InMemoryBucket(), FakeMedia(fail=True)

    result = await _use_case(bucket, media).store(_made())

    assert result == PictureStoreFailed(reason="database_error", spent_usd=Decimal("0.0336"))
    assert bucket.objects == {}


async def test_the_same_bytes_are_not_stored_twice_and_the_object_is_kept() -> None:
    bucket, media = InMemoryBucket(), FakeMedia()
    first = await _use_case(bucket, media).store(_made())
    assert isinstance(first, StoredPicture)

    again = await _use_case(bucket, media).store(_made())

    assert again == PictureStoreFailed(reason="already_stored", spent_usd=Decimal("0.0336"))
    assert len(bucket.objects) == 1
    assert len(media.assets) == 1
