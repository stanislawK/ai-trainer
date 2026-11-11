from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Protocol

from ai_trainer.domain.exercises import ExerciseCandidate
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
    """Shortlisting an outside name against the shared catalogue only: no athlete's exercise or
    alias is ever a candidate (#552, #555)."""

    async def catalogue_shortlist(
        self, *, sport_id: str, name: str, floor: float, limit: int
    ) -> Sequence[ExerciseCandidate]:
        """At most `limit` catalogue exercises whose name or catalogue alias is at least `floor`
        similar to `name` by trigram similarity, best first, each with its equipment and target
        (what Jev tells them apart by)."""
        ...
