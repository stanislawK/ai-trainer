import re
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

# A lower-case hex SHA-256 digest, as `media_assets_sha256_format` checks it.
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
# As `media_assets_embed_url_scheme` checks it: never a `javascript:` or `data:` URL.
EMBED_URL_PATTERN = re.compile(r"^https?://")


class MediaRepositoryError(Exception):
    """The media tables could not be written; the cause stays in the adapter."""


class MediaKind(StrEnum):
    """What a media asset is (ADR-0031, F24)."""

    GIF = "gif"
    IMAGE = "image"
    DRAWING = "drawing"
    VIDEO = "video"


class MediaRights(StrEnum):
    """Under what rights it is shown (PRD G16): `unstated` is only ever embedded."""

    LICENSED = "licensed"
    UNSTATED = "unstated"


class MediaDelivery(StrEnum):
    """Whether the media bucket holds it or the source's own embed shows it."""

    STORED = "stored"
    EMBEDDED = "embedded"


class MediaLinkStatus(StrEnum):
    """A link's review state: only `approved` is shown, and `removed` is the takedown."""

    SUGGESTED = "suggested"
    APPROVED = "approved"
    REMOVED = "removed"


class MediaAsset(BaseModel):
    """One picture or video, with its source, licence and attribution (ADR-0031, G10). It
    refuses what `media_assets`' CHECKs refuse, so a bad row fails before the database."""

    model_config = ConfigDict(frozen=True)

    id: UUID | None = None
    kind: MediaKind
    rights: MediaRights
    delivery: MediaDelivery
    storage_key: str | None = None
    embed_url: str | None = None
    source: str = Field(min_length=1)
    source_ref: str = Field(min_length=1)
    license: str | None = None
    attribution: str | None = None
    sha256: str | None = None
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _matches_the_checks(self) -> Self:
        if self.sha256 is not None and not SHA256_PATTERN.fullmatch(self.sha256):
            raise ValueError("sha256 must be 64 lower-case hex digits")
        if self.delivery is MediaDelivery.STORED:
            if self.storage_key is None or self.sha256 is None or self.embed_url is not None:
                raise ValueError("stored media has a storage key and a sha256, and no embed URL")
        elif self.embed_url is None or self.storage_key is not None:
            raise ValueError("embedded media has an embed URL and no storage key")
        if self.embed_url is not None and not EMBED_URL_PATTERN.match(self.embed_url):
            raise ValueError("an embed URL is http(s)")
        if (self.width is None) != (self.height is None):
            raise ValueError("width and height are set together")
        if self.rights is MediaRights.LICENSED:
            if not (self.license or "").strip() or not (self.attribution or "").strip():
                raise ValueError("licensed media has a licence and an attribution")
        elif self.delivery is not MediaDelivery.EMBEDDED:
            raise ValueError("media with unstated rights is only embedded")
        return self


class ExerciseMediaItem(BaseModel):
    """An approved asset on an exercise's card, in its `position` order."""

    model_config = ConfigDict(frozen=True)

    asset: MediaAsset
    position: int = Field(ge=0)
