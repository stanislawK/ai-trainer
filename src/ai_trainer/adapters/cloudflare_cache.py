"""Cloudflare's cache purge by URL (ADR-0031 `removed`, #557).

One POST to `/zones/<zone>/purge_cache` with `{"files": [url]}`, under one deadline. The token
is a custom API token scoped to Zone, Cache Purge, Purge on the media zone only. Errors carry the
status or the exception class, never the request, headers or body."""

import asyncio

import httpx2

from ai_trainer.domain.cdn_cache import CdnCacheError

_API = "https://api.cloudflare.com/client/v4"


class CloudflareCdnCache:
    """Implements `CdnCachePort`."""

    def __init__(
        self,
        *,
        zone_id: str,
        token: str,
        timeout_seconds: float,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._zone_id = zone_id
        self._token = token
        self._timeout = timeout_seconds
        self._transport = transport

    def __repr__(self) -> str:
        return "CloudflareCdnCache()"

    async def purge(self, url: str) -> None:
        try:
            async with (
                asyncio.timeout(self._timeout),
                httpx2.AsyncClient(
                    base_url=_API, transport=self._transport, timeout=self._timeout
                ) as client,
            ):
                response = await client.post(
                    f"/zones/{self._zone_id}/purge_cache",
                    headers={"Authorization": f"Bearer {self._token}"},
                    json={"files": [url]},
                )
        except TimeoutError, httpx2.TimeoutException:
            raise CdnCacheError("cache purge timed out") from None
        except Exception as error:  # a purge never fails Remove; the class name only
            raise CdnCacheError(f"cache purge failed: {type(error).__name__}") from None
        if response.status_code >= httpx2.codes.BAD_REQUEST:
            raise CdnCacheError(f"cache purge refused: HTTP {response.status_code}")
        try:
            accepted = response.json().get("success") is True
        except ValueError, AttributeError:
            accepted = False
        if not accepted:
            raise CdnCacheError("cache purge refused: success was not true")
