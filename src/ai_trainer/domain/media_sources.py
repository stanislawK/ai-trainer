"""Importing pictures from an outside media source (ADR-0031 *First media source*, #514, #552).

wger is the first: each image carries its own licence, author and AI flag. An image becomes a
`licensed`, `stored` asset whose bytes are wger's file as served, and whose `sha256` is of those
bytes. Nothing here reaches the network; the port and its adapter do."""

import hashlib
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ai_trainer.domain.media import MediaAsset, MediaDelivery, MediaKind, MediaRights

WGER_SOURCE = "wger"
# Appended to `source_ref` when wger flags the image as AI-generated, so the card reads the
# flag without a lookup (ADR-0031's #552 amendment).
AI_GENERATED_FRAGMENT = "#ai-generated"

type MediaSourceProblem = Literal["wger_error", "wger_timeout"]
type ImageMediaType = Literal["image/jpeg", "image/png", "image/webp", "image/gif"]


class MediaSourceError(Exception):
    """The media source could not be read: an HTTP error, a timeout, or a body that doesn't
    parse. `problem` is bounded; the cause stays in the adapter."""

    def __init__(self, problem: MediaSourceProblem) -> None:
        super().__init__(problem)
        self.problem: MediaSourceProblem = problem


class WgerLicence(BaseModel):
    """One entry of `/api/v2/license/`."""

    model_config = ConfigDict(frozen=True)

    id: int
    short_name: str = Field(min_length=1)
    url: str = Field(min_length=1)


class WgerImage(BaseModel):
    """One entry of `/api/v2/exerciseimage/`, with the URL of its own API record."""

    model_config = ConfigDict(frozen=True)

    id: int
    exercise: int
    image: str = Field(min_length=1)
    license: int
    license_author: str | None = None
    is_ai_generated: bool = False
    record_url: str = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class DownloadedImage:
    """An image's bytes as wger served them, never re-encoded."""

    data: bytes
    media_type: ImageMediaType


_EXTENSIONS: dict[ImageMediaType, str] = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
}


def wger_source_ref(image: WgerImage) -> str:
    """The image's own API record, plus `#ai-generated` when wger flags it."""
    return image.record_url + (AI_GENERATED_FRAGMENT if image.is_ai_generated else "")


def wger_asset(
    *, image: WgerImage, licence: WgerLicence, downloaded: DownloadedImage, exercise_id: UUID
) -> MediaAsset:
    """The asset row for one wger image: `licensed`, `stored`, `image`, with ADR-0031's
    attribution and the key `exercises/<exercise_id>/<sha256>.<ext>`."""
    author = (image.license_author or "").strip()
    if not author:
        raise ValueError("a wger image with no author is skipped, never stored")
    digest = hashlib.sha256(downloaded.data).hexdigest()
    return MediaAsset(
        kind=MediaKind.IMAGE,
        rights=MediaRights.LICENSED,
        delivery=MediaDelivery.STORED,
        storage_key=f"exercises/{exercise_id}/{digest}.{_EXTENSIONS[downloaded.media_type]}",
        source=WGER_SOURCE,
        source_ref=wger_source_ref(image),
        license=licence.short_name,
        attribution=f"{author} · {licence.short_name} ({licence.url}) — via wger.de",
        sha256=digest,
    )


type SkipReason = Literal["no_author", "unknown_licence", "known_sha256"]
type ImportFailure = Literal["wger_error", "wger_timeout", "storage_error", "database_error"]


@dataclass(frozen=True, slots=True)
class ImportedImage:
    image_id: int
    asset_id: UUID
    exercise_id: UUID


@dataclass(frozen=True, slots=True)
class SkippedImage:
    image_id: int
    reason: SkipReason


@dataclass(slots=True)
class ImportReport:
    """What one run did, image by image. `failed` is set when the run stopped early; what was
    stored before it stays whole, an object and its row together."""

    stored: list[ImportedImage] = field(default_factory=list)
    skipped: list[SkippedImage] = field(default_factory=list)
    unmatched: list[int] = field(default_factory=list)
    failed: ImportFailure | None = None
