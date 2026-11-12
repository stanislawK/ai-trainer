"""The admin's review of catalogue exercise media (F24, ADR-0031 *Media links*, #554).

Suggested pictures are listed per exercise with their credit and public URL, and approved one
at a time or, for wger's, all of an exercise's the admin saw at once. A generated picture is
never approved in bulk (ADR-0031 *Fallback media source*): bulk approval names `WGER_SOURCE`, so
no other source can be swept in, and only the link IDs the page drew, so no later import either.
Every approval is conditional on `suggested`, so one approved or removed since the page was drawn
is left as it is and the admin is told.

Any suggested or approved picture can be removed at once (#556): the link becomes `removed` first,
so the card stops showing it whatever happens next, and then a stored picture's object moves to
quarantine, unless another link still shows the same asset. Undo and Restore move it back first,
and put the earlier status back only once the object is public again."""

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from itertools import groupby
from typing import Literal
from uuid import UUID

from ai_trainer.application.media_quarantine import MediaQuarantine
from ai_trainer.application.ports.clock import ClockPort
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
    # Under Removed: when, and the status Restore puts back.
    removed_at: datetime | None = None
    removed_from: MediaLinkStatus | None = None


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


@dataclass(frozen=True, slots=True)
class Removed:
    exercise_name: str
    # The object could not leave the public bucket; Remove again retries the move.
    still_public: bool
    # Another link still shows the same asset, so its file was left public on purpose.
    shared: bool


@dataclass(frozen=True, slots=True)
class Restored:
    exercise_name: str
    status: MediaLinkStatus


@dataclass(frozen=True, slots=True)
class RestoreFailed:
    """The object could not be moved back; the link is still `removed`."""

    exercise_name: str


type ApproveOutcome = Approved | NotSuggested
type RestoreOutcome = Restored | RestoreFailed


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

    def __init__(
        self,
        *,
        media: ExerciseMediaReviewPort,
        public_base_url: str | None,
        quarantine: MediaQuarantine,
        clock: ClockPort,
    ) -> None:
        self._media = media
        self._base_url = public_base_url
        self._quarantine = quarantine
        self._clock = clock

    def _picture(self, row: ExerciseMediaReviewRow) -> ReviewPicture:
        asset = row.asset
        origin = _origin(row)
        # A removed picture's public URL stops serving it, so none is drawn (no broken image).
        public_url = (
            media_public_url(self._base_url, asset.storage_key)
            if asset.storage_key is not None and row.status is not MediaLinkStatus.REMOVED
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
            removed_at=row.removed_at,
            removed_from=row.removed_from,
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

    async def approve_all_from_wger(
        self, exercise_id: UUID, link_ids: Collection[UUID]
    ) -> BulkApproved:
        """Approve the wger pictures the admin saw: `link_ids` are the ones the page drew, so one
        imported since stays `suggested`; a posted ID that isn't a suggested wger link on this
        exercise is ignored."""
        count = await self._media.approve_suggested_from(exercise_id, WGER_SOURCE, link_ids)
        left = await self._media.review_links(MediaLinkStatus.SUGGESTED, exercise_id=exercise_id)
        return BulkApproved(
            count=count,
            generated_left=sum(1 for row in left if _origin(row) == "generated"),
        )

    async def remove(self, link_id: UUID) -> Removed | Restored | None:
        """Hide the link at once, then move a stored picture's object to quarantine. A link
        already removed keeps its first removal and has the move retried. A link restored in
        another tab since is left live with its file public, and says its status. None when
        there is no such link on a catalogue exercise."""
        row = await self._media.review_link(link_id)
        if row is None:
            return None
        name = row.exercise_name
        if await self._media.remove(link_id, at=self._clock.now()) is None:
            # Nothing changed: read again, so a link that is live by now keeps its file.
            now = await self._media.review_link(link_id)
            if now is not None and now.status is not MediaLinkStatus.REMOVED:
                return Restored(exercise_name=name, status=now.status)
        key = row.asset.storage_key
        if key is None:
            return Removed(exercise_name=name, still_public=False, shared=False)
        # Read from the table, so the asset always has its ID.
        asset_id = row.asset.id
        if asset_id is not None and await self._media.has_live_link_besides(asset_id, link_id):
            return Removed(exercise_name=name, still_public=False, shared=True)
        hidden = await self._quarantine.hide(key)
        return Removed(exercise_name=name, still_public=not hidden, shared=False)

    async def restore(self, link_id: UUID) -> RestoreOutcome | None:
        """Move a stored picture's object back, then put the link's earlier status back; if
        the move fails the link stays `removed`. None when there is no such link."""
        row = await self._media.review_link(link_id)
        if row is None:
            return None
        name = row.exercise_name
        if row.status is not MediaLinkStatus.REMOVED:
            return Restored(exercise_name=name, status=row.status)
        key = row.asset.storage_key
        if key is not None and not await self._quarantine.release(key):
            return RestoreFailed(exercise_name=name)
        try:
            status = await self._media.restore(link_id)
        except BaseException:
            # The link is still `removed`, so its file must not stay public.
            if key is not None:
                await self._quarantine.hide(key)
            raise
        if status is None:
            # Restored in another tab between the read above and the update.
            now = await self._media.review_link(link_id)
            status = (now or row).status
        return Restored(exercise_name=name, status=status)
