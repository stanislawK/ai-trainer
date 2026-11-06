from typing import Protocol

from ai_trainer.domain.pmc import PmcArticle, PmcNotFound, PmcUnavailable


class PmcMetadataPort(Protocol):
    """An article's metadata from PMC's Cloud Service (ADR-0036, #458). A request names only the
    PMC ID (invariant 7). Never raises for the network or the bucket's answers."""

    async def fetch(self, pmcid: str) -> PmcArticle | PmcNotFound | PmcUnavailable:
        """The article's latest version."""
        ...


class PmcArticleFilesPort(Protocol):
    """An approved article's JATS XML from PMC's Cloud Service (ADR-0036, #463). A request names
    only the PMC ID (invariant 7). Never raises for the network or the bucket's answers."""

    async def fetch_jats(self, pmcid: str) -> bytes | PmcNotFound | PmcUnavailable:
        """The latest version's XML. `PmcNotFound` when the bucket holds no such article or no
        XML for it; `PmcUnavailable` for a failure, a file over the size cap, a link that is not
        that article's own, or bytes that do not match the md5 PMC recorded."""
        ...
