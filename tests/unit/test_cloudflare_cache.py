"""The Cloudflare cache purge adapter on a fake HTTP transport (ADR-0031 `removed`, #557)."""

import json
import logging

import httpx2
import pytest

from ai_trainer.adapters.cloudflare_cache import CloudflareCdnCache
from ai_trainer.domain.cdn_cache import CdnCacheError

ZONE = "zone-id-0123456789"
TOKEN = "cf-token-very-secret-value"
URL = "https://media.example.org/exercises/ab/cd.png"


def _cache(handler: httpx2.MockTransport, timeout: float = 5.0) -> CloudflareCdnCache:
    return CloudflareCdnCache(zone_id=ZONE, token=TOKEN, timeout_seconds=timeout, transport=handler)


async def test_cdn_purge_posts_the_one_url_with_the_bearer_token() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"success": True, "errors": [], "result": {"id": "x"}})

    await _cache(httpx2.MockTransport(handler)).purge(URL)

    [request] = seen
    assert request.method == "POST"
    assert str(request.url) == f"https://api.cloudflare.com/client/v4/zones/{ZONE}/purge_cache"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert json.loads(request.content) == {"files": [URL]}


def _failures() -> dict[str, httpx2.MockTransport]:
    def forbidden(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(403, json={"success": False, "errors": [{"message": TOKEN}]})

    def refused(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"success": False, "errors": [{"message": TOKEN}]})

    def not_json(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, text=f"oops {TOKEN}")

    def read_timeout(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout(f"timed out {TOKEN}", request=request)

    def connect_error(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError(f"no route {TOKEN}", request=request)

    return {
        "forbidden": httpx2.MockTransport(forbidden),
        "success_false": httpx2.MockTransport(refused),
        "not_json": httpx2.MockTransport(not_json),
        "read_timeout": httpx2.MockTransport(read_timeout),
        "connect_error": httpx2.MockTransport(connect_error),
    }


@pytest.mark.parametrize("case", list(_failures()))
async def test_cdn_purge_failures_raise_cdn_cache_error(case: str) -> None:
    with pytest.raises(CdnCacheError):
        await _cache(_failures()[case]).purge(URL)


async def test_cdn_purge_deadline_raises_cdn_cache_error() -> None:
    import asyncio

    async def slow(request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(1)
        return httpx2.Response(200, json={"success": True})

    with pytest.raises(CdnCacheError):
        await _cache(httpx2.MockTransport(slow), timeout=0.01).purge(URL)


@pytest.mark.parametrize("case", list(_failures()))
async def test_cdn_purge_token_never_appears_in_errors_or_logs(
    case: str, caplog: pytest.LogCaptureFixture
) -> None:
    cache = _cache(_failures()[case])
    with caplog.at_level(logging.DEBUG), pytest.raises(CdnCacheError) as raised:
        await cache.purge(URL)

    assert TOKEN not in str(raised.value)
    assert TOKEN not in repr(raised.value)
    assert TOKEN not in caplog.text
    assert TOKEN not in repr(cache)
    assert TOKEN not in str(vars(cache).get("_headers", ""))
