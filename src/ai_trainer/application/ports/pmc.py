from typing import Protocol

from ai_trainer.domain.pmc import PmcArticle, PmcNotFound, PmcUnavailable


class PmcMetadataPort(Protocol):
    """An article's metadata from PMC's Cloud Service (ADR-0036, #458). A request names only the
    PMC ID (invariant 7). Never raises for the network or the bucket's answers."""

    async def fetch(self, pmcid: str) -> PmcArticle | PmcNotFound | PmcUnavailable:
        """The article's latest version."""
        ...
