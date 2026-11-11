"""Reading wger's image, licence and exercise-name listings (ADR-0031, ADR-0016, #552). No
network: every response is replayed from `tests/fixtures/wger/` through a mock transport."""

from collections.abc import AsyncIterator

import httpx2
import pytest

from ai_trainer.adapters.wger import HttpxWgerImages
from ai_trainer.domain.media_sources import MediaSourceError, WgerImage
from tests.wger_support import BENCH_PRESS_FILE, WGER, FakeWgerSite, WgerFailure


@pytest.fixture
async def site() -> FakeWgerSite:
    return FakeWgerSite()


@pytest.fixture
async def client(site: FakeWgerSite) -> AsyncIterator[httpx2.AsyncClient]:
    async with httpx2.AsyncClient(transport=site.transport()) as client:
        yield client


def _adapter(client: httpx2.AsyncClient, *, timeout: float = 1.0) -> HttpxWgerImages:
    return HttpxWgerImages(http_client=client, base_url=WGER, timeout_seconds=timeout)


async def _all(adapter: HttpxWgerImages, limit: int | None = None) -> list[WgerImage]:
    return [image async for image in adapter.images(limit)]


async def test_licences_are_parsed() -> None:
    async with httpx2.AsyncClient(transport=FakeWgerSite().transport()) as client:
        licences = await _adapter(client).licences()

    by_id = {licence.id: licence for licence in licences}
    assert set(by_id) == {1, 2, 3, 4, 5}
    assert by_id[2].short_name == "CC-BY-SA 4"
    assert by_id[2].url == "https://creativecommons.org/licenses/by-sa/4.0/deed.en"


async def test_english_names_follow_next(site: FakeWgerSite, client: httpx2.AsyncClient) -> None:
    names = await _adapter(client).english_names()

    assert names == {
        76: "Bench Press Narrow Grip",
        95: "Biceps Curl With Cable",
        167: "Crunches",
        2673: "Stomach Vacuum",
    }
    [first, second] = site.requests
    assert first.url.params.get("language") == "2"
    assert second.url.params.get("offset") == "2"


async def test_images_follow_next_and_carry_their_record(
    site: FakeWgerSite, client: httpx2.AsyncClient
) -> None:
    images = await _all(_adapter(client))

    assert [image.id for image in images] == [3, 15, 19, 724]
    no_author, bench, _, vacuum = images
    assert no_author.license_author == ""
    assert (bench.exercise, bench.license, bench.license_author) == (76, 1, "Everkinetic")
    assert bench.record_url == "https://wger.de/api/v2/exerciseimage/15/"
    assert (vacuum.is_ai_generated, vacuum.license, vacuum.license_author) == (True, 2, "gemini")
    # Only wger's own listings: no cookie, no credentials, a generic agent.
    for request in site.requests:
        assert request.method == "GET"
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers


async def test_limit_stops_the_listing_early(
    site: FakeWgerSite, client: httpx2.AsyncClient
) -> None:
    images = await _all(_adapter(client), limit=1)

    assert [image.id for image in images] == [3]
    [request] = site.requests
    assert request.url.params.get("limit") == "1"


async def test_download_returns_the_bytes_as_served(client: httpx2.AsyncClient) -> None:
    adapter = _adapter(client)
    bench = (await _all(adapter))[1]

    downloaded = await adapter.download(bench)

    assert downloaded.data == BENCH_PRESS_FILE.read_bytes()
    assert downloaded.media_type == "image/png"


async def test_download_from_another_host_is_refused(client: httpx2.AsyncClient) -> None:
    adapter = _adapter(client)
    bench = (await _all(adapter))[1]
    elsewhere = bench.model_copy(update={"image": "https://evil.example/x.png"})

    with pytest.raises(MediaSourceError) as raised:
        await adapter.download(elsewhere)

    assert raised.value.problem == "wger_error"


async def test_download_of_an_unsupported_type_is_an_error(client: httpx2.AsyncClient) -> None:
    adapter = _adapter(client)
    bench = (await _all(adapter))[1]
    # The fake answers JSON for the licence path.
    odd = bench.model_copy(update={"image": "https://wger.de/api/v2/license/"})

    with pytest.raises(MediaSourceError) as raised:
        await adapter.download(odd)

    assert raised.value.problem == "wger_error"


async def test_next_on_another_host_is_not_followed() -> None:
    site = FakeWgerSite()
    inner = site.transport()

    async def rewrite(request: httpx2.Request) -> httpx2.Response:
        response = await inner.handle_async_request(request)
        if request.url.path == "/api/v2/exerciseimage/" and "offset" not in request.url.params:
            body = (await response.aread()).replace(b"https://wger.de/", b"https://evil.example/")
            return httpx2.Response(200, content=body, request=request)
        return response

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(rewrite)) as client:
        with pytest.raises(MediaSourceError) as raised:
            await _all(_adapter(client))

    assert raised.value.problem == "wger_error"
    assert all(request.url.host == "wger.de" for request in site.requests)


@pytest.mark.parametrize(
    ("failure", "problem"),
    [
        ("server_error", "wger_error"),
        ("malformed", "wger_error"),
        ("transport", "wger_error"),
        ("timeout", "wger_timeout"),
    ],
)
async def test_a_failed_page_is_a_media_source_error(failure: WgerFailure, problem: str) -> None:
    site = FakeWgerSite(failing={"/api/v2/exerciseimage/?offset=2": failure})

    async with httpx2.AsyncClient(transport=site.transport()) as client:
        with pytest.raises(MediaSourceError) as raised:
            await _all(_adapter(client, timeout=0.05))

    assert raised.value.problem == problem


async def test_a_failed_download_is_a_media_source_error() -> None:
    path = "/media/exercise-images/88/Narrow-grip-bench-press-1.png"
    site = FakeWgerSite(failing={path: "timeout"})

    async with httpx2.AsyncClient(transport=site.transport()) as client:
        adapter = _adapter(client, timeout=0.05)
        bench = (await _all(adapter))[1]
        with pytest.raises(MediaSourceError) as raised:
            await adapter.download(bench)

    assert raised.value.problem == "wger_timeout"


async def test_a_failed_licence_list_is_a_media_source_error() -> None:
    site = FakeWgerSite(failing={"/api/v2/license/": "server_error"})

    async with httpx2.AsyncClient(transport=site.transport()) as client:
        with pytest.raises(MediaSourceError):
            await _adapter(client).licences()


async def test_a_next_link_back_to_a_read_page_is_an_error() -> None:
    inner = FakeWgerSite().transport()

    async def loop(request: httpx2.Request) -> httpx2.Response:
        response = await inner.handle_async_request(request)
        if request.url.params.get("offset") == "2":
            body = (await response.aread()).replace(
                b'"next": null', b'"next": "https://wger.de/api/v2/exerciseimage/?limit=100"'
            )
            return httpx2.Response(200, content=body, request=request)
        return response

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(loop)) as client:
        with pytest.raises(MediaSourceError) as raised:
            await _all(_adapter(client))

    assert raised.value.problem == "wger_error"
