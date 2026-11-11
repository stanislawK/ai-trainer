from collections.abc import Collection, Mapping, Sequence
from typing import Protocol
from uuid import UUID

from ai_trainer.domain.media import (
    ExerciseMediaItem,
    ExerciseMediaReviewRow,
    MediaAsset,
    MediaLinkStatus,
)


class ExerciseMediaRepositoryPort(Protocol):
    """Catalogue media and its links to exercises (ADR-0031, F24). It holds no user data: a
    caller reaches an `exercise_id` through the athlete's own, user-scoped exercise lookup."""

    async def add_asset(self, asset: MediaAsset) -> UUID:
        """Store `asset` and return its ID."""
        ...

    async def link(
        self, exercise_id: UUID, asset_id: UUID, *, position: int, status: MediaLinkStatus
    ) -> None:
        """Put the asset on the exercise at `position`, with `status`."""
        ...

    async def has_sha256(self, sha256: str) -> bool:
        """Whether an asset with these bytes is already catalogued."""
        ...

    async def add_suggested(self, asset: MediaAsset, exercise_id: UUID) -> UUID:
        """Store `asset` and its `suggested` link on the exercise, at the next free position, in
        one transaction, so an asset never exists without its link (#535). Raises
        `MediaRepositoryError` when the write fails, and then writes nothing."""
        ...

    async def approved_for(self, exercise_id: UUID) -> Sequence[ExerciseMediaItem]:
        """The exercise card's media: only `approved` links, by `position` (invariant 5)."""
        ...

    async def is_removed(self, *, sha256: str | None, embed_url: str | None) -> bool:
        """Whether an importer must skip this picture: an asset with that `sha256` or
        `embed_url` has a `removed` link on any exercise (ADR-0031, the takedown path)."""
        ...


class ExerciseMediaReviewPort(Protocol):
    """The admin's F24 review of catalogue media links (ADR-0031, #554). Admin-only, checked by
    the caller from `ADMIN_EMAILS` (ADR-0005); catalogue media holds no user data."""

    async def review_links(
        self, status: MediaLinkStatus, *, exercise_id: UUID | None = None
    ) -> Sequence[ExerciseMediaReviewRow]:
        """Links with `status` on catalogue exercises (never an athlete's own), by exercise name
        and then `position`; only those on `exercise_id` when it is given (F24 review, #554)."""
        ...

    async def review_link(self, link_id: UUID) -> ExerciseMediaReviewRow | None:
        """One link on a catalogue exercise, whatever its status; None when there is none."""
        ...

    async def status_counts(self) -> Mapping[MediaLinkStatus, int]:
        """How many catalogue links have each status, every status present."""
        ...

    async def approve(self, link_id: UUID) -> bool:
        """Set the link `approved` only if it is still `suggested`, in one conditional update, so
        a concurrent change is never overwritten. False when nothing changed."""
        ...

    async def approve_suggested_from(
        self, exercise_id: UUID, source: str, link_ids: Collection[UUID]
    ) -> int:
        """Approve those of `link_ids` that are still `suggested`, on `exercise_id`, with an asset
        from `source`, in one conditional update; any other ID is ignored. Returns how many
        changed (ADR-0031: a generated picture is never bulk-approved, so the caller names the
        source; the IDs are the ones the admin saw, so a later suggestion is never swept in)."""
        ...
