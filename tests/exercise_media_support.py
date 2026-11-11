"""A memory `ExerciseMediaReviewPort` for the F24 review (#554), and sample assets."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from uuid import UUID, uuid4

from ai_trainer.application.exercise_media_review import ExerciseMediaReview
from ai_trainer.domain.media import (
    ExerciseMediaItem,
    ExerciseMediaReviewRow,
    MediaAsset,
    MediaDelivery,
    MediaKind,
    MediaLinkStatus,
    MediaRights,
)


def wger_asset(*, author: str = "K. Brandt", ai: bool = False) -> MediaAsset:
    ref = f"https://wger.de/api/v2/exerciseimage/{uuid4().int % 10_000}/"
    return MediaAsset(
        kind=MediaKind.IMAGE,
        rights=MediaRights.LICENSED,
        delivery=MediaDelivery.STORED,
        storage_key=f"exercises/{uuid4()}/{uuid4().hex}.png",
        source="wger",
        source_ref=ref + ("#ai-generated" if ai else ""),
        license="CC-BY-SA 4",
        attribution=(
            f"{author} · CC-BY-SA 4 (https://creativecommons.org/licenses/by-sa/4.0/deed.en)"
            " — via wger.de"
        ),
        sha256=uuid4().hex * 2,
    )


def generated_asset() -> MediaAsset:
    return MediaAsset(
        kind=MediaKind.IMAGE,
        rights=MediaRights.LICENSED,
        delivery=MediaDelivery.STORED,
        storage_key=f"exercises/{uuid4()}/{uuid4().hex}.jpg",
        source="ai-generated",
        source_ref="google/gemini-3.1-flash-lite-image-20260630",
        license="Google won't claim ownership over generated content",
        attribution=(
            "AI generated with Gemini 3.1 Flash Lite Image (Google) via OpenRouter · 2026-11-08"
        ),
        sha256=uuid4().hex * 2,
    )


def embedded_asset() -> MediaAsset:
    return MediaAsset(
        kind=MediaKind.VIDEO,
        rights=MediaRights.UNSTATED,
        delivery=MediaDelivery.EMBEDDED,
        embed_url=f"https://www.youtube.com/embed/{uuid4().hex[:11]}",
        source="youtube",
        source_ref="lift-clinic",
    )


@dataclass
class _Link:
    link_id: UUID
    exercise_id: UUID
    position: int
    status: MediaLinkStatus
    asset: MediaAsset


class MemoryExerciseMedia:
    """Implements `ExerciseMediaReviewPort` in memory, catalogue exercises only, plus the card's
    `approved_for`, so a test can tell what an approval shows."""

    def __init__(self) -> None:
        self.names: dict[UUID, str] = {}
        self.links: list[_Link] = []

    def exercise(self, name: str) -> UUID:
        exercise_id = uuid4()
        self.names[exercise_id] = name
        return exercise_id

    def put(
        self,
        exercise_id: UUID,
        asset: MediaAsset,
        status: MediaLinkStatus = MediaLinkStatus.SUGGESTED,
    ) -> UUID:
        link_id = uuid4()
        position = sum(1 for link in self.links if link.exercise_id == exercise_id)
        self.links.append(
            _Link(link_id, exercise_id, position, status, asset.model_copy(update={"id": uuid4()}))
        )
        return link_id

    def status_of(self, link_id: UUID) -> MediaLinkStatus:
        return next(link.status for link in self.links if link.link_id == link_id)

    def _row(self, link: _Link) -> ExerciseMediaReviewRow:
        return ExerciseMediaReviewRow(
            link_id=link.link_id,
            exercise_id=link.exercise_id,
            exercise_name=self.names[link.exercise_id],
            position=link.position,
            status=link.status,
            asset=link.asset,
        )

    async def approved_for(self, exercise_id: UUID) -> Sequence[ExerciseMediaItem]:
        return [
            ExerciseMediaItem(asset=link.asset, position=link.position)
            for link in self.links
            if link.exercise_id == exercise_id and link.status is MediaLinkStatus.APPROVED
        ]

    async def review_links(
        self, status: MediaLinkStatus, *, exercise_id: UUID | None = None
    ) -> Sequence[ExerciseMediaReviewRow]:
        chosen = [
            link
            for link in self.links
            if link.status is status and exercise_id in (None, link.exercise_id)
        ]
        chosen.sort(key=lambda link: (self.names[link.exercise_id], link.position))
        return [self._row(link) for link in chosen]

    async def review_link(self, link_id: UUID) -> ExerciseMediaReviewRow | None:
        return next((self._row(link) for link in self.links if link.link_id == link_id), None)

    async def status_counts(self) -> Mapping[MediaLinkStatus, int]:
        return {
            status: sum(1 for link in self.links if link.status is status)
            for status in MediaLinkStatus
        }

    async def approve(self, link_id: UUID) -> bool:
        for link in self.links:
            if link.link_id == link_id and link.status is MediaLinkStatus.SUGGESTED:
                link.status = MediaLinkStatus.APPROVED
                return True
        return False

    async def approve_suggested_from(self, exercise_id: UUID, source: str) -> int:
        changed = 0
        for link in self.links:
            if (
                link.exercise_id == exercise_id
                and link.status is MediaLinkStatus.SUGGESTED
                and link.asset.source == source
            ):
                link.status = MediaLinkStatus.APPROVED
                changed += 1
        return changed


def memory_exercise_media_review(
    media: MemoryExerciseMedia | None = None, *, base_url: str | None = None
) -> ExerciseMediaReview:
    """`build_admin_router`'s `exercise_media`, for a test about the rest of the admin view."""
    return ExerciseMediaReview(media=media or MemoryExerciseMedia(), public_base_url=base_url)
