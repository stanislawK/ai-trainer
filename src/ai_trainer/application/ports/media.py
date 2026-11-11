from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from ai_trainer.domain.media import ExerciseMediaItem, MediaAsset, MediaLinkStatus


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
