"""Storing a generated picture as suggested media (ADR-0031 *Fallback media source*, #535).

The bytes go to the media bucket first, then one transaction writes the asset and its
`suggested` link. A failure at either step leaves nothing behind. Nothing here approves: an
admin does that, one picture at a time (PRD 0012 F24)."""

import contextlib
import hashlib

from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.application.ports.media import ExerciseMediaRepositoryPort
from ai_trainer.application.ports.object_storage import ObjectStoragePort
from ai_trainer.domain.media import MediaRepositoryError
from ai_trainer.domain.media_generation import (
    PictureMade,
    PictureStoreFailed,
    StoredPicture,
    generated_picture_asset,
)
from ai_trainer.domain.object_storage import ObjectStorageError


class StoreExercisePicture:
    def __init__(
        self,
        *,
        storage: ObjectStoragePort,
        media: ExerciseMediaRepositoryPort,
        clock: ClockPort,
        model: str,
    ) -> None:
        self._storage = storage
        self._media = media
        self._clock = clock
        self._model = model

    async def store(self, made: PictureMade) -> StoredPicture | PictureStoreFailed:
        spent = made.spent_usd
        # The same bytes are one asset (`media_assets_sha256_key`); the object under that key
        # belongs to it, so this run neither overwrites nor deletes it.
        if await self._media.has_sha256(hashlib.sha256(made.image.data).hexdigest()):
            return PictureStoreFailed(reason="already_stored", spent_usd=spent)
        asset = generated_picture_asset(
            exercise_id=made.exercise_id,
            image=made.image,
            model=self._model,
            generated_on=self._clock.now().date(),
        )
        key = asset.storage_key
        if key is None:  # pragma: no cover -- a stored asset always has its key
            raise ValueError("stored media has a storage key")
        try:
            await self._storage.put(key, made.image.data, content_type=made.image.media_type)
        except ObjectStorageError:
            return PictureStoreFailed(reason="storage_error", spent_usd=spent)
        try:
            asset_id = await self._media.add_suggested(asset, made.exercise_id)
        except MediaRepositoryError:
            await self._discard(key)
            return PictureStoreFailed(reason="database_error", spent_usd=spent)
        except BaseException:
            # Anything else, and a cancelled run, still leaves no object without a row.
            await self._discard(key)
            raise
        return StoredPicture(asset_id=asset_id, exercise_id=made.exercise_id, spent_usd=spent)

    async def _discard(self, key: str) -> None:
        with contextlib.suppress(ObjectStorageError):
            await self._storage.delete(key)
