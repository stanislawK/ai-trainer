"""Importing wger's pictures as suggested media (ADR-0031 *First media source*, #514, #552, #555).

Per image, in wger's order: one with no author, or with a licence wger doesn't list, is skipped.
Its English name is shortlisted against the shared catalogue only, by trigram similarity at the
floor, and Jev picks from that shortlist through `ExercisePickerPort` as the run's admin. Only a
confident pick is a match: no English name, no candidate, "none", a pick below the threshold, a
timeout or an error leaves the image unmatched, and nothing is downloaded. Each name is asked
once per run, so several images of one exercise cost one call, and `limit` bounds the calls. A
matched image is downloaded, skipped when its
`sha256` is already catalogued (a `removed` asset keeps its row, so a takedown is never undone),
then put in the media bucket and written as an asset with a `suggested` link in one transaction.
A failure after the put deletes the object, so no object is left without its row. A wger,
storage or database failure stops the run; what was stored before it stays whole. Nothing here
approves: an admin does that in F24's review."""

import contextlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from uuid import UUID

from ai_trainer.application.ports.exercises import ExercisePickerPort
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


@dataclass(frozen=True, slots=True)
class ShortlistLimits:
    """The catalogue shortlist Jev picks from: candidates at `floor` or above, at most `size`."""

    floor: float
    size: int


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
        picker: ExercisePickerPort,
        media: ExerciseMediaRepositoryPort,
        storage: ObjectStoragePort,
        sport_id: str,
        limits: ShortlistLimits,
    ) -> None:
        self._wger = wger
        self._catalogue = catalogue
        self._picker = picker
        self._media = media
        self._storage = storage
        self._sport_id = sport_id
        self._limits = limits

    async def run(self, *, admin_id: UUID, limit: int | None) -> ImportReport:
        """`admin_id` is an active admin's, resolved by the caller; Jev's calls are recorded under
        it."""
        report = ImportReport()
        picks = _Picks(admin_id)
        try:
            licences = {licence.id: licence for licence in await self._wger.licences()}
            names = await self._wger.english_names()
            async for image in self._wger.images(limit):
                await self._one(image, licences=licences, names=names, picks=picks, report=report)
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
        picks: _Picks,
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
        exercise_id = None if name is None else await self._exercise_for(name, picks)
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

    async def _exercise_for(self, name: str, picks: _Picks) -> UUID | None:
        if name in picks.by_name:
            return picks.by_name[name]
        candidates = await self._catalogue.catalogue_shortlist(
            sport_id=self._sport_id,
            name=name,
            floor=self._limits.floor,
            limit=self._limits.size,
        )
        picked = await self._picker.pick(picks.admin_id, name, candidates) if candidates else None
        picks.by_name[name] = None if picked is None else picked.id
        return picks.by_name[name]


@dataclass(slots=True)
class _Picks:
    """One run's admin and its picks by wger name, so a name is shortlisted and asked once."""

    admin_id: UUID
    by_name: dict[str, UUID | None] = field(default_factory=dict)
