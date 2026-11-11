"""Importing wger pictures as suggested media (ADR-0031 *First media source*, #552): which images
are skipped, matched or stored, and what a failure leaves behind. Fakes only."""

import hashlib
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest

from ai_trainer.application.wger_picture_import import ImportWgerPictures
from ai_trainer.domain.media import (
    ExerciseMediaItem,
    MediaAsset,
    MediaDelivery,
    MediaKind,
    MediaLinkStatus,
    MediaRepositoryError,
    MediaRights,
)
from ai_trainer.domain.media_sources import (
    DownloadedImage,
    ImportedImage,
    MediaSourceError,
    SkippedImage,
    WgerImage,
    WgerLicence,
    wger_asset,
)
from tests.picture_support import InMemoryBucket

_PUSH_UP = UUID("00000000-0000-4000-8000-000000000552")
_LICENCES = (
    WgerLicence(
        id=1, short_name="CC-BY-SA 3", url="https://creativecommons.org/licenses/by-sa/3.0/deed.en"
    ),
    WgerLicence(
        id=2, short_name="CC-BY-SA 4", url="https://creativecommons.org/licenses/by-sa/4.0/deed.en"
    ),
)


def _image(
    image_id: int,
    *,
    exercise: int = 10,
    author: str | None = "Everkinetic",
    licence: int = 1,
    ai: bool = False,
) -> WgerImage:
    return WgerImage(
        id=image_id,
        exercise=exercise,
        image=f"https://wger.de/media/exercise-images/{image_id}.png",
        license=licence,
        license_author=author,
        is_ai_generated=ai,
        record_url=f"https://wger.de/api/v2/exerciseimage/{image_id}/",
    )


@dataclass
class FakeWger:
    listed: list[WgerImage]
    names: dict[int, str] = field(default_factory=lambda: {10: "Push Up"})
    fail_download_of: int | None = None
    downloads: list[int] = field(default_factory=list)
    limits: list[int | None] = field(default_factory=list)

    async def licences(self) -> Sequence[WgerLicence]:
        return _LICENCES

    async def english_names(self) -> Mapping[int, str]:
        return self.names

    async def images(self, limit: int | None) -> AsyncIterator[WgerImage]:
        self.limits.append(limit)
        for image in self.listed[:limit]:
            yield image

    async def download(self, image: WgerImage) -> DownloadedImage:
        if image.id == self.fail_download_of:
            raise MediaSourceError("wger_timeout")
        self.downloads.append(image.id)
        return DownloadedImage(data=f"png-{image.id}".encode(), media_type="image/png")


@dataclass
class FakeCatalogue:
    matches: dict[str, UUID] = field(default_factory=lambda: {"Push Up": _PUSH_UP})
    asked: list[tuple[str, str, float]] = field(default_factory=list)

    async def best_catalogue_match(
        self, *, sport_id: str, name: str, threshold: float
    ) -> UUID | None:
        self.asked.append((sport_id, name, threshold))
        return self.matches.get(name)


@dataclass
class FakeMedia:
    assets: list[tuple[MediaAsset, UUID]] = field(default_factory=list)
    known: set[str] = field(default_factory=set)
    fail: bool = False
    crash: bool = False

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
        return sha256 in self.known or any(a.sha256 == sha256 for a, _ in self.assets)

    async def add_suggested(self, asset: MediaAsset, exercise_id: UUID) -> UUID:
        if self.fail:
            raise MediaRepositoryError
        if self.crash:
            raise ValueError("not a database error")
        self.assets.append((asset, exercise_id))
        return uuid4()


def _importer(
    wger: FakeWger,
    *,
    bucket: InMemoryBucket | None = None,
    media: FakeMedia | None = None,
    catalogue: FakeCatalogue | None = None,
) -> ImportWgerPictures:
    return ImportWgerPictures(
        wger=wger,
        catalogue=catalogue or FakeCatalogue(),
        media=media or FakeMedia(),
        storage=bucket or InMemoryBucket(),
        sport_id="gym",
        threshold=0.75,
    )


def _sha(image_id: int) -> str:
    return hashlib.sha256(f"png-{image_id}".encode()).hexdigest()


async def test_image_with_no_author_is_skipped() -> None:
    wger = FakeWger([_image(1, author=""), _image(2, author="   "), _image(3, author=None)])
    bucket = InMemoryBucket()

    report = await _importer(wger, bucket=bucket).run(limit=None)

    assert report.skipped == [
        SkippedImage(image_id=1, reason="no_author"),
        SkippedImage(image_id=2, reason="no_author"),
        SkippedImage(image_id=3, reason="no_author"),
    ]
    assert wger.downloads == []
    assert bucket.objects == {}


async def test_unknown_licence_is_skipped() -> None:
    wger = FakeWger([_image(1, licence=99)])

    report = await _importer(wger).run(limit=None)

    assert report.skipped == [SkippedImage(image_id=1, reason="unknown_licence")]
    assert wger.downloads == []


async def test_below_threshold_stores_nothing() -> None:
    wger = FakeWger([_image(1, exercise=10)], names={10: "Stomach Vacuum"})
    bucket = InMemoryBucket()
    media = FakeMedia()
    catalogue = FakeCatalogue()

    report = await _importer(wger, bucket=bucket, media=media, catalogue=catalogue).run(limit=None)

    assert report.unmatched == [1]
    assert catalogue.asked == [("gym", "Stomach Vacuum", 0.75)]
    assert (wger.downloads, bucket.objects, media.assets) == ([], {}, [])


async def test_image_with_no_english_name_is_unmatched_without_a_lookup() -> None:
    wger = FakeWger([_image(1, exercise=77)])
    catalogue = FakeCatalogue()

    report = await _importer(wger, catalogue=catalogue).run(limit=None)

    assert report.unmatched == [1]
    assert catalogue.asked == []


async def test_matched_image_is_stored_with_a_suggested_link() -> None:
    wger = FakeWger([_image(1)])
    bucket = InMemoryBucket()
    media = FakeMedia()

    report = await _importer(wger, bucket=bucket, media=media).run(limit=None)

    [(asset, exercise_id)] = media.assets
    assert exercise_id == _PUSH_UP
    assert report.stored == [
        ImportedImage(image_id=1, asset_id=report.stored[0].asset_id, exercise_id=_PUSH_UP)
    ]
    assert bucket.objects == {f"exercises/{_PUSH_UP}/{_sha(1)}.png": b"png-1"}
    assert bucket.content_types[str(asset.storage_key)] == "image/png"


async def test_known_sha256_is_skipped() -> None:
    wger = FakeWger([_image(1)])
    bucket = InMemoryBucket()
    media = FakeMedia(known={_sha(1)})

    report = await _importer(wger, bucket=bucket, media=media).run(limit=None)

    assert report.skipped == [SkippedImage(image_id=1, reason="known_sha256")]
    assert (bucket.objects, media.assets) == ({}, [])


async def test_removed_sha256_is_skipped() -> None:
    """A `removed` link keeps its asset row (ADR-0031), so `has_sha256` sees it and the same
    bytes are never stored again; the repository test covers the removed row itself."""
    wger = FakeWger([_image(1)])
    media = FakeMedia(known={_sha(1)})

    report = await _importer(wger, media=media).run(limit=None)

    assert report.skipped == [SkippedImage(image_id=1, reason="known_sha256")]


async def test_the_same_bytes_twice_in_one_run_store_once() -> None:
    wger = FakeWger([_image(1), _image(1)])
    media = FakeMedia()

    report = await _importer(wger, media=media).run(limit=None)

    assert len(media.assets) == 1
    assert report.skipped == [SkippedImage(image_id=1, reason="known_sha256")]


async def test_limit_is_passed_to_the_listing() -> None:
    wger = FakeWger([_image(1), _image(2)])

    report = await _importer(wger).run(limit=1)

    assert wger.limits == [1]
    assert [stored.image_id for stored in report.stored] == [1]


async def test_wger_error_stops_the_run_and_keeps_what_was_stored() -> None:
    wger = FakeWger([_image(1), _image(2), _image(3)], fail_download_of=2)
    bucket = InMemoryBucket()
    media = FakeMedia()

    report = await _importer(wger, bucket=bucket, media=media).run(limit=None)

    assert report.failed == "wger_timeout"
    assert [stored.image_id for stored in report.stored] == [1]
    assert len(bucket.objects) == len(media.assets) == 1


async def test_wger_error_before_any_image_fails_the_run() -> None:
    class Broken(FakeWger):
        async def licences(self) -> Sequence[WgerLicence]:
            raise MediaSourceError("wger_error")

    report = await _importer(Broken([_image(1)])).run(limit=None)

    assert report.failed == "wger_error"
    assert report.stored == []


async def test_storage_error_stops_the_run_with_no_row() -> None:
    media = FakeMedia()

    report = await _importer(
        FakeWger([_image(1)]), bucket=InMemoryBucket(fail_put=True), media=media
    ).run(limit=None)

    assert report.failed == "storage_error"
    assert media.assets == []


async def test_database_error_after_put_deletes_object() -> None:
    bucket = InMemoryBucket()

    report = await _importer(FakeWger([_image(1)]), bucket=bucket, media=FakeMedia(fail=True)).run(
        limit=None
    )

    assert report.failed == "database_error"
    assert bucket.objects == {}


async def test_any_other_error_after_put_deletes_object_and_raises() -> None:
    bucket = InMemoryBucket()

    with pytest.raises(ValueError, match="not a database error"):
        await _importer(FakeWger([_image(1)]), bucket=bucket, media=FakeMedia(crash=True)).run(
            limit=None
        )

    assert bucket.objects == {}


def test_wger_asset_fields() -> None:
    image = _image(724, author="gemini", licence=2)
    data = b"\xff\xd8\xff-bytes"

    asset = wger_asset(
        image=image,
        licence=_LICENCES[1],
        downloaded=DownloadedImage(data=data, media_type="image/jpeg"),
        exercise_id=_PUSH_UP,
    )

    digest = hashlib.sha256(data).hexdigest()
    assert (asset.kind, asset.rights, asset.delivery) == (
        MediaKind.IMAGE,
        MediaRights.LICENSED,
        MediaDelivery.STORED,
    )
    assert asset.source == "wger"
    assert asset.license == "CC-BY-SA 4"
    assert asset.attribution == (
        "gemini · CC-BY-SA 4 (https://creativecommons.org/licenses/by-sa/4.0/deed.en) — via wger.de"
    )
    assert asset.source_ref == "https://wger.de/api/v2/exerciseimage/724/"
    assert asset.sha256 == digest
    assert asset.storage_key == f"exercises/{_PUSH_UP}/{digest}.jpg"
    assert asset.embed_url is None


def test_ai_flag_is_kept_in_source_ref() -> None:
    asset = wger_asset(
        image=_image(9, ai=True),
        licence=_LICENCES[0],
        downloaded=DownloadedImage(data=b"x", media_type="image/webp"),
        exercise_id=_PUSH_UP,
    )

    assert asset.source_ref == "https://wger.de/api/v2/exerciseimage/9/#ai-generated"
    assert str(asset.storage_key).endswith(".webp")


def test_wger_asset_refuses_an_image_with_no_author() -> None:
    with pytest.raises(ValueError, match="no author"):
        wger_asset(
            image=_image(1, author=" "),
            licence=_LICENCES[0],
            downloaded=DownloadedImage(data=b"x", media_type="image/png"),
            exercise_id=_PUSH_UP,
        )
