"""The exercise card's pictures (F23, F24, ADR-0031 *Media links*, #558): each candidate's first
approved stored picture by `position`, with its public URL and credit, read for every candidate
in one query when the card is drawn, so a Remove hides it at once. While `MEDIA_PUBLIC_BASE_URL`
is unset there is no URL to show one by, so nothing is read and the card is text only."""

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from uuid import UUID

from ai_trainer.application.media_credit import (
    PictureOrigin,
    WgerCredit,
    ai_flagged,
    page_url,
    picture_origin,
    wger_credit,
)
from ai_trainer.application.ports.media import ExerciseCardMediaPort
from ai_trainer.domain.media import MediaAsset
from ai_trainer.domain.object_storage import media_public_url


@dataclass(frozen=True, slots=True)
class CardPicture:
    public_url: str
    origin: PictureOrigin
    credit: WgerCredit | None
    # wger's image page.
    page_url: str | None
    attribution: str | None
    # A wger picture its uploader marked as AI-made gets the "AI generated" mark.
    ai_flagged: bool


class ExerciseCardMedia:
    """Facade for the chat's exercise card."""

    def __init__(self, *, media: ExerciseCardMediaPort, public_base_url: str | None) -> None:
        self._media = media
        self._base_url = public_base_url

    def _picture(self, asset: MediaAsset, public_url: str) -> CardPicture:
        return CardPicture(
            public_url=public_url,
            origin=picture_origin(asset),
            credit=wger_credit(asset),
            page_url=page_url(asset),
            attribution=asset.attribution,
            ai_flagged=ai_flagged(asset),
        )

    async def pictures(self, exercise_ids: Collection[UUID]) -> Mapping[UUID, CardPicture]:
        """By exercise ID; an exercise without an approved picture is left out."""
        if self._base_url is None or not exercise_ids:
            return {}
        found = await self._media.first_approved_pictures(exercise_ids)
        # The adapter reads only stored pictures, which a CHECK makes carry a key.
        return {
            exercise_id: self._picture(asset, url)
            for exercise_id, asset in found.items()
            if asset.storage_key is not None
            and (url := media_public_url(self._base_url, asset.storage_key)) is not None
        }
