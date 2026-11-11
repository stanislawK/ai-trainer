"""Importing wger's pictures as suggested media (ADR-0031 *First media source*, #514, #552).

Per image, in wger's order: one with no author, or with a licence wger doesn't list, is skipped;
its English name is matched against the shared catalogue only, by trigram similarity at the
threshold, and below it nothing is downloaded. A matched image is downloaded, skipped when its
`sha256` is already catalogued (a `removed` asset keeps its row, so a takedown is never undone),
then put in the media bucket and written as an asset with a `suggested` link in one transaction.
A failure after the put deletes the object, so no object is left without its row. A wger,
storage or database failure stops the run; what was stored before it stays whole. Nothing here
approves: an admin does that in F24's review."""

import contextlib
from collections.abc import Mapping

from ai_trainer.application.ports.media import ExerciseMediaRepositoryPort
from ai_trainer.application.ports.media_sources import (
    CatalogueExerciseMatchPort,
    WgerImagesPort,
)
from ai_trainer.application.ports.object_storage import ObjectStoragePort
from ai_trainer.domain.media import MediaRepositoryError
from ai_trainer.domain.media_sources import (
    ImportedImage,
    ImportFailure,
    ImportReport,
    MediaSourceError,
    SkippedImage,
    WgerImage,
    WgerLicence,
    wger_asset,
)
from ai_trainer.domain.object_storage import ObjectStorageError


class _StopError(Exception):
    def __init__(self, failure: ImportFailure) -> None:
        super().__init__(failure)
        self.failure: ImportFailure = failure


class ImportWgerPictures:
    def __init__(
        self,
        *,
        wger: WgerImagesPort,
        catalogue: CatalogueExerciseMatchPort,
        media: ExerciseMediaRepositoryPort,
        storage: ObjectStoragePort,
        sport_id: str,
        threshold: float,
    ) -> None:
        self._wger = wger
        self._catalogue = catalogue
        self._media = media
        self._storage = storage
        self._sport_id = sport_id
        self._threshold = threshold

    async def run(self, *, limit: int | None) -> ImportReport:
        report = ImportReport()
        try:
            licences = {licence.id: licence for licence in await self._wger.licences()}
            names = await self._wger.english_names()
            async for image in self._wger.images(limit):
                await self._one(image, licences=licences, names=names, report=report)
        except MediaSourceError as error:
            report.failed = error.problem
        except _StopError as stop:
            report.failed = stop.failure
        return report

    async def _one(
        self,
        image: WgerImage,
        *,
        licences: Mapping[int, WgerLicence],
        names: Mapping[int, str],
        report: ImportReport,
    ) -> None:
        if not (image.license_author or "").strip():
            report.skipped.append(SkippedImage(image_id=image.id, reason="no_author"))
            return
        licence = licences.get(image.license)
        if licence is None:
            report.skipped.append(SkippedImage(image_id=image.id, reason="unknown_licence"))
            return
        name = names.get(image.exercise)
        exercise_id = (
            None
            if name is None
            else await self._catalogue.best_catalogue_match(
                sport_id=self._sport_id, name=name, threshold=self._threshold
            )
        )
        if exercise_id is None:
            report.unmatched.append(image.id)
            return
        downloaded = await self._wger.download(image)
        asset = wger_asset(
            image=image, licence=licence, downloaded=downloaded, exercise_id=exercise_id
        )
        if asset.sha256 is None or asset.storage_key is None:  # pragma: no cover -- stored media
            raise ValueError("stored media has a sha256 and a storage key")
        if await self._media.has_sha256(asset.sha256):
            report.skipped.append(SkippedImage(image_id=image.id, reason="known_sha256"))
            return
        key = asset.storage_key
        try:
            await self._storage.put(key, downloaded.data, content_type=downloaded.media_type)
        except ObjectStorageError:
            raise _StopError("storage_error") from None
        try:
            asset_id = await self._media.add_suggested(asset, exercise_id)
        except MediaRepositoryError:
            await self._discard(key)
            raise _StopError("database_error") from None
        except BaseException:
            # Anything else, and a cancelled run, still leaves no object without a row.
            await self._discard(key)
            raise
        report.stored.append(
            ImportedImage(image_id=image.id, asset_id=asset_id, exercise_id=exercise_id)
        )

    async def _discard(self, key: str) -> None:
        with contextlib.suppress(ObjectStorageError):
            await self._storage.delete(key)
