from typing import Protocol


class CdnCachePort(Protocol):
    async def purge(self, url: str) -> None:
        """Drop the CDN's cached copy of one public URL. Raises `CdnCacheError`."""
        ...
