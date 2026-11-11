from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Protocol
from uuid import UUID

from ai_trainer.domain.media_sources import DownloadedImage, WgerImage, WgerLicence


class WgerImagesPort(Protocol):
    """wger's public API (ADR-0031 *First media source*, ADR-0016). Every request names only
    wger's own listings or an image's own URL; no user data leaves the app. Each method raises
    `MediaSourceError` when wger can't be read."""

    async def licences(self) -> Sequence[WgerLicence]:
        """Every licence wger lists."""
        ...

    async def english_names(self) -> Mapping[int, str]:
        """Each wger exercise ID's first English name."""
        ...

    def images(self, limit: int | None) -> AsyncIterator[WgerImage]:
        """wger's images in its own order, at most `limit` of them (all when None)."""
        ...

    async def download(self, image: WgerImage) -> DownloadedImage:
        """The image's bytes, as served."""
        ...


class CatalogueExerciseMatchPort(Protocol):
    """Matching an outside name against the shared catalogue only: no athlete's exercise or
    alias is ever a candidate (#552)."""

    async def best_catalogue_match(
        self, *, sport_id: str, name: str, threshold: float
    ) -> UUID | None:
        """The catalogue exercise whose name or catalogue alias is most similar to `name` by
        trigram similarity, if that similarity is at least `threshold`."""
        ...
