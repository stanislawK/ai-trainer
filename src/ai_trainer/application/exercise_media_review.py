"""The admin's review of catalogue exercise media (F24, ADR-0031 *Media links*, #554).

Suggested pictures are listed per exercise with their credit and public URL, and approved one
at a time or, for wger's, all of an exercise's at once. A generated picture is never approved in
bulk (ADR-0031 *Fallback media source*): bulk approval names `WGER_SOURCE`, so no other source
can be swept in. Every approval is conditional on `suggested`, so one approved or removed since
the page was drawn is left as it is and the admin is told."""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import groupby
from typing import Literal
from uuid import UUID

from ai_trainer.application.ports.media import ExerciseMediaReviewPort
from ai_trainer.domain.media import (
    ExerciseMediaReviewRow,
    MediaDelivery,
    MediaLinkStatus,
)
from ai_trainer.domain.media_generation import GENERATED_PICTURE_SOURCE
from ai_trainer.domain.media_sources import AI_GENERATED_FRAGMENT, WGER_SOURCE
from ai_trainer.domain.object_storage import media_public_url

type PictureOrigin = Literal["wger", "generated", "embedded", "other"]

# ADR-0031 *First media source*: `<author> · <licence name> (<licence URL>) — via wger.de`.
_WGER_ATTRIBUTION = re.compile(
    r"^(?P<author>.+) · (?P<licence>.+) \((?P<url>https?://[^\s()]+)\) — via wger\.de$"
)


@dataclass(frozen=True, slots=True)
class WgerCredit:
    """A wger picture's credit, split so its licence can be a link."""

    author: str
    licence: str
    licence_url: str


@dataclass(frozen=True, slots=True)
class ReviewPicture:
    link_id: UUID
    status: MediaLinkStatus
    origin: PictureOrigin
    # None for embedded media, and while `MEDIA_PUBLIC_BASE_URL` is unset: no broken image.
    public_url: str | None
    # wger's image page, or an embedded video's own URL.
    page_url: str | None
    credit: WgerCredit | None
    attribution: str | None
    source: str
    ai_flagged: bool


@dataclass(frozen=True, slots=True)
class ReviewGroup:
    exercise_id: UUID
    name: str
    pictures: tuple[ReviewPicture, ...]
    # What "Approve all from wger · n" counts: suggested wger pictures only.
    wger_suggested: int


@dataclass(frozen=True, slots=True)
class ReviewPage:
    view: MediaLinkStatus
    groups: tuple[ReviewGroup, ...]
    counts: Mapping[MediaLinkStatus, int]


@dataclass(frozen=True, slots=True)
class Approved:
    exercise_name: str


@dataclass(frozen=True, slots=True)
class NotSuggested:
    """Approved or removed since the page was drawn; nothing changed."""

    exercise_name: str
    status: MediaLinkStatus


@dataclass(frozen=True, slots=True)
class BulkApproved:
    count: int
    # Generated pictures on the exercise still waiting for their own Approve.
    generated_left: int


type ApproveOutcome = Approved | NotSuggested


def _origin(row: ExerciseMediaReviewRow) -> PictureOrigin:
    if row.asset.delivery is MediaDelivery.EMBEDDED:
        return "embedded"
    if row.asset.source == WGER_SOURCE:
        return "wger"
    if row.asset.source == GENERATED_PICTURE_SOURCE:
        return "generated"
    return "other"


def _credit(attribution: str | None) -> WgerCredit | None:
    match = _WGER_ATTRIBUTION.fullmatch(attribution or "")
    if match is None:
        return None
    return WgerCredit(author=match["author"], licence=match["licence"], licence_url=match["url"])


class ExerciseMediaReview:
    """Facade for `/admin/exercises`: the caller has already checked the admin (ADR-0005)."""

    def __init__(self, *, media: ExerciseMediaReviewPort, public_base_url: str | None) -> None:
        self._media = media
        self._base_url = public_base_url

    def _picture(self, row: ExerciseMediaReviewRow) -> ReviewPicture:
        asset = row.asset
        origin = _origin(row)
        public_url = (
            media_public_url(self._base_url, asset.storage_key)
            if asset.storage_key is not None
            else None
        )
        page_url: str | None = asset.embed_url
        if origin == "wger":
            page_url = asset.source_ref.removesuffix(AI_GENERATED_FRAGMENT)
        return ReviewPicture(
            link_id=row.link_id,
            status=row.status,
            origin=origin,
            public_url=public_url,
            page_url=page_url,
            credit=_credit(asset.attribution) if origin == "wger" else None,
            attribution=asset.attribution,
            source=asset.source,
            ai_flagged=origin == "wger" and asset.source_ref.endswith(AI_GENERATED_FRAGMENT),
        )

    def _groups(self, rows: Sequence[ExerciseMediaReviewRow]) -> tuple[ReviewGroup, ...]:
        groups: list[ReviewGroup] = []
        for (exercise_id, name), members in groupby(
            rows, key=lambda row: (row.exercise_id, row.exercise_name)
        ):
            pictures = tuple(self._picture(row) for row in members)
            groups.append(
                ReviewGroup(
                    exercise_id=exercise_id,
                    name=name,
                    pictures=pictures,
                    wger_suggested=sum(
                        1
                        for picture in pictures
                        if picture.origin == "wger" and picture.status is MediaLinkStatus.SUGGESTED
                    ),
                )
            )
        return tuple(groups)

    async def page(self, view: MediaLinkStatus) -> ReviewPage:
        rows = await self._media.review_links(view)
        return ReviewPage(
            view=view, groups=self._groups(rows), counts=await self._media.status_counts()
        )

    async def approve(self, link_id: UUID) -> ApproveOutcome | None:
        """None when there is no such link on a catalogue exercise."""
        row = await self._media.review_link(link_id)
        if row is None:
            return None
        if await self._media.approve(link_id):
            return Approved(exercise_name=row.exercise_name)
        # Read again: it may have changed between the read above and the update.
        now = await self._media.review_link(link_id)
        return NotSuggested(exercise_name=row.exercise_name, status=(now or row).status)

    async def approve_all_from_wger(self, exercise_id: UUID) -> BulkApproved:
        count = await self._media.approve_suggested_from(exercise_id, WGER_SOURCE)
        left = await self._media.review_links(MediaLinkStatus.SUGGESTED, exercise_id=exercise_id)
        return BulkApproved(
            count=count,
            generated_left=sum(1 for row in left if _origin(row) == "generated"),
        )
