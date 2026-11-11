"""wger.de's public API (ADR-0031 *First media source*, ADR-0016, #552).

Anonymous GETs only: the licence list, the English exercise names, the image listing and each
image's own file. Nothing about a user is sent. `next` links and image files are followed only on
wger's own origin. Each request has its own deadline, connect and read included, as `pmc.py`
does; a non-2xx answer, a timeout, a transport error or a body that doesn't parse is a
`MediaSourceError`."""

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import get_args
from urllib.parse import urlsplit

import httpx2
from pydantic import BaseModel, ValidationError

from ai_trainer.domain.media_sources import (
    DownloadedImage,
    ImageMediaType,
    MediaSourceError,
    WgerImage,
    WgerLicence,
)

# wger's own page size limit is higher; 100 keeps each page small.
_PAGE_SIZE = 100
_ENGLISH = 2
_MEDIA_TYPES: frozenset[str] = frozenset(get_args(ImageMediaType.__value__))


class _Page[T](BaseModel):
    next: str | None = None
    results: list[T]


class _Image(BaseModel):
    id: int
    exercise: int
    image: str
    license: int
    license_author: str | None = None
    is_ai_generated: bool = False


class _Translation(BaseModel):
    exercise: int
    name: str


class HttpxWgerImages:
    """Implements `WgerImagesPort`."""

    def __init__(
        self, *, http_client: httpx2.AsyncClient, base_url: str, timeout_seconds: float
    ) -> None:
        self._client = http_client
        self._base = base_url.rstrip("/")
        self._origin = _origin(self._base)
        self._timeout = timeout_seconds

    async def licences(self) -> Sequence[WgerLicence]:
        return [
            licence async for licence in self._items(f"{self._base}/api/v2/license/", WgerLicence)
        ]

    async def english_names(self) -> Mapping[int, str]:
        names: dict[int, str] = {}
        url = f"{self._base}/api/v2/exercise-translation/?language={_ENGLISH}&limit={_PAGE_SIZE}"
        async for translation in self._items(url, _Translation):
            names.setdefault(translation.exercise, translation.name)
        return names

    async def images(self, limit: int | None) -> AsyncIterator[WgerImage]:
        size = _PAGE_SIZE if limit is None else max(1, min(limit, _PAGE_SIZE))
        seen = 0
        async for raw in self._items(f"{self._base}/api/v2/exerciseimage/?limit={size}", _Image):
            if limit is not None and seen >= limit:
                return
            seen += 1
            yield WgerImage(
                **raw.model_dump(), record_url=f"{self._base}/api/v2/exerciseimage/{raw.id}/"
            )

    async def download(self, image: WgerImage) -> DownloadedImage:
        response = await self._get(image.image)
        media_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if media_type not in _MEDIA_TYPES:
            raise MediaSourceError("wger_error")
        return DownloadedImage(data=response.content, media_type=media_type)  # type: ignore[arg-type]  # checked against ImageMediaType just above

    async def _items[T: BaseModel](self, url: str, model: type[T]) -> AsyncIterator[T]:
        """Every result over the listing's pages, following `next` on wger's origin only."""
        visited: set[str] = set()
        next_url: str | None = url
        while next_url is not None:
            if next_url in visited:
                raise MediaSourceError("wger_error")
            visited.add(next_url)
            response = await self._get(next_url)
            try:
                page = _Page[model].model_validate_json(response.content)  # type: ignore[valid-type]  # a runtime type parameter
            except ValidationError:
                raise MediaSourceError("wger_error") from None
            for item in page.results:
                yield item
            next_url = page.next

    async def _get(self, url: str) -> httpx2.Response:
        if _origin(url) != self._origin:
            raise MediaSourceError("wger_error")
        try:
            async with asyncio.timeout(self._timeout):
                response = await self._client.get(url, timeout=self._timeout)
                await response.aread()
            return response.raise_for_status()
        except TimeoutError, httpx2.TimeoutException:
            raise MediaSourceError("wger_timeout") from None
        except httpx2.HTTPError:
            raise MediaSourceError("wger_error") from None


def _origin(url: str) -> tuple[str, str]:
    parts = urlsplit(url)
    return parts.scheme, parts.netloc
