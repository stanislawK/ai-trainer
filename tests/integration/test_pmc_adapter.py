"""Reading article metadata from PMC's Cloud Service bucket (ADR-0036, #458). No network: every
response comes from the fake bucket's mock transport."""

import asyncio
import hashlib

import httpx2
import pytest

from ai_trainer.adapters.pmc import PmcCloudFiles, PmcCloudMetadata
from ai_trainer.domain.pmc import PmcArticle, PmcFetchProblem, PmcNotFound, PmcUnavailable
from tests.pmc_support import (
    BUCKET,
    DEMO,
    Failure,
    FakeBucket,
    article,
    bucket_with,
    demo_article,
)


def _metadata(bucket: FakeBucket) -> PmcCloudMetadata:
    return PmcCloudMetadata(bucket_url=BUCKET, timeout_seconds=1.0, transport=bucket.transport())


async def test_a_fetch_requests_only_the_metadata_by_pmc_id() -> None:
    bucket = bucket_with(demo_article())

    fetched = await _metadata(bucket).fetch("PMC13692514")

    assert isinstance(fetched, PmcArticle)
    assert fetched.title.startswith("Resistance Training Improves")
    urls = [str(request.url) for request in bucket.requests]
    assert urls == [
        f"{BUCKET}/?list-type=2&prefix=metadata%2FPMC13692514.",
        f"{BUCKET}/metadata/PMC13692514.1.json",
    ]
    # Never the article's XML, PDF or media, which the JSON links (that's #463).
    assert all(request.method == "GET" for request in bucket.requests)
    for request in bucket.requests:
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers


async def test_the_highest_version_is_read() -> None:
    bucket = bucket_with(
        article("PMC77", version=1, title="First"),
        article("PMC77", version=3, title="Third"),
        article("PMC77", version=2, title="Second"),
    )

    fetched = await _metadata(bucket).fetch("PMC77")

    assert isinstance(fetched, PmcArticle)
    assert (fetched.version, fetched.title) == (3, "Third")


async def test_a_prefix_of_a_longer_id_is_not_that_article() -> None:
    # `metadata/PMC77.` lists PMC77's versions, never PMC771's.
    bucket = bucket_with(article("PMC771"))

    assert await _metadata(bucket).fetch("PMC77") == PmcNotFound()


async def test_an_unknown_id_is_not_found() -> None:
    assert await _metadata(FakeBucket()).fetch("PMC1") == PmcNotFound()


@pytest.mark.parametrize(
    ("failure", "problem"),
    [
        ("timeout", PmcFetchProblem.TIMEOUT),
        ("error", PmcFetchProblem.FAILED),
        ("server_error", PmcFetchProblem.FAILED),
        ("malformed", PmcFetchProblem.MALFORMED),
    ],
)
async def test_a_bucket_failure_is_unavailable(failure: Failure, problem: PmcFetchProblem) -> None:
    bucket = bucket_with(article("PMC5"))
    bucket.failing["PMC5"] = failure

    assert await _metadata(bucket).fetch("PMC5") == PmcUnavailable(problem)


async def test_json_for_another_article_is_malformed() -> None:
    bucket = bucket_with(article("PMC6"))
    bucket.articles["PMC6"][1] = article("PMC9")

    assert await _metadata(bucket).fetch("PMC6") == PmcUnavailable(PmcFetchProblem.MALFORMED)


async def test_json_of_the_wrong_shape_is_malformed() -> None:
    bucket = bucket_with(article("PMC8"))
    bucket.articles["PMC8"][1] = {"pmcid": "PMC8"}

    assert await _metadata(bucket).fetch("PMC8") == PmcUnavailable(PmcFetchProblem.MALFORMED)


async def test_a_listed_version_that_is_gone_is_unavailable() -> None:
    bucket = bucket_with(article("PMC4"))
    bucket.failing["PMC4"] = "gone"

    assert await _metadata(bucket).fetch("PMC4") == PmcUnavailable(PmcFetchProblem.FAILED)


async def test_json_of_another_version_is_malformed() -> None:
    # The listing says version 2; the object under that key says it is version 1.
    bucket = bucket_with(article("PMC3", version=2))
    bucket.articles["PMC3"][2] = article("PMC3", version=1)

    assert await _metadata(bucket).fetch("PMC3") == PmcUnavailable(PmcFetchProblem.MALFORMED)


async def test_one_deadline_bounds_both_requests_together() -> None:
    """Each GET answers just inside the timeout, but the two together don't: the article's
    deadline stops it (an httpx timeout alone bounds each phase separately)."""
    bucket = bucket_with(article("PMC2"))
    inner = bucket.transport()

    async def slow(request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(0.06)
        return await inner.handle_async_request(request)

    metadata = PmcCloudMetadata(
        bucket_url=BUCKET, timeout_seconds=0.1, transport=httpx2.MockTransport(slow)
    )

    assert await metadata.fetch("PMC2") == PmcUnavailable(PmcFetchProblem.TIMEOUT)


# --- The JATS file (#463) -------------------------------------------------------------------

_XML = b"<article><body><p>Hello.</p></body></article>"


def _files(bucket: FakeBucket, *, timeout: float = 1.0, max_bytes: int = 1024) -> PmcCloudFiles:
    return PmcCloudFiles(
        metadata=_metadata(bucket),
        bucket_url=BUCKET,
        timeout_seconds=timeout,
        max_bytes=max_bytes,
        transport=bucket.transport(),
    )


async def test_the_jats_is_fetched_from_the_articles_own_key() -> None:
    bucket = FakeBucket()
    bucket.put_with_xml(article("PMC77"), _XML)

    fetched = await _files(bucket).fetch_jats("PMC77")

    assert fetched == _XML
    urls = [str(request.url) for request in bucket.requests]
    assert urls[-1] == f"{BUCKET}/PMC77.1/PMC77.1.xml"
    # The same two metadata GETs as staging, then the one XML: nothing else in the bucket.
    assert len(urls) == 3
    for request in bucket.requests:
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers


async def test_the_real_articles_xml_matches_its_recorded_md5() -> None:
    real = (DEMO.parent / "PMC13692514.1.xml").read_bytes()
    bucket = FakeBucket()
    bucket.put(demo_article())
    bucket.xml["PMC13692514"] = {1: real}

    fetched = await _files(bucket, max_bytes=1_000_000).fetch_jats("PMC13692514")

    assert fetched == real


async def test_the_latest_versions_xml_is_fetched() -> None:
    bucket = FakeBucket()
    bucket.put_with_xml(article("PMC77", version=1), b"<article>one</article>")
    bucket.put_with_xml(article("PMC77", version=2), _XML)

    assert await _files(bucket).fetch_jats("PMC77") == _XML


async def test_an_unknown_id_has_no_xml() -> None:
    assert isinstance(await _files(FakeBucket()).fetch_jats("PMC404"), PmcNotFound)


async def test_an_xml_key_that_is_gone_is_not_found() -> None:
    bucket = FakeBucket()
    bucket.put_with_xml(article("PMC77"), _XML)
    bucket.xml_failing["PMC77"] = "gone"

    assert isinstance(await _files(bucket).fetch_jats("PMC77"), PmcNotFound)


async def test_an_article_without_an_xml_url_is_not_found() -> None:
    bucket = bucket_with(article("PMC77", xml_url=None))

    assert isinstance(await _files(bucket).fetch_jats("PMC77"), PmcNotFound)


@pytest.mark.parametrize(
    ("failure", "problem"),
    [
        pytest.param("timeout", PmcFetchProblem.TIMEOUT, id="timeout"),
        pytest.param("error", PmcFetchProblem.FAILED, id="unreachable"),
        pytest.param("server_error", PmcFetchProblem.FAILED, id="5xx"),
    ],
)
async def test_an_xml_failure_is_unavailable(failure: Failure, problem: PmcFetchProblem) -> None:
    bucket = FakeBucket()
    bucket.put_with_xml(article("PMC77"), _XML)
    bucket.xml_failing["PMC77"] = failure

    assert await _files(bucket).fetch_jats("PMC77") == PmcUnavailable(problem)


async def test_metadata_that_cannot_be_read_is_passed_on() -> None:
    bucket = FakeBucket()
    bucket.put_with_xml(article("PMC77"), _XML)
    bucket.failing["PMC77"] = "error"

    assert await _files(bucket).fetch_jats("PMC77") == PmcUnavailable(PmcFetchProblem.FAILED)


async def test_bytes_that_do_not_match_the_recorded_md5_are_malformed() -> None:
    bucket = FakeBucket()
    bucket.put_with_xml(article("PMC77"), _XML, md5="0" * 32)

    assert await _files(bucket).fetch_jats("PMC77") == PmcUnavailable(PmcFetchProblem.MALFORMED)


async def test_a_file_over_the_cap_is_malformed_not_read_whole() -> None:
    bucket = FakeBucket()
    bucket.put_with_xml(article("PMC77"), _XML * 100)

    fetched = await _files(bucket, max_bytes=len(_XML) * 10).fetch_jats("PMC77")

    assert fetched == PmcUnavailable(PmcFetchProblem.MALFORMED)


@pytest.mark.parametrize(
    "xml_url",
    [
        pytest.param("s3://other-bucket/PMC77.1/PMC77.1.xml", id="other-bucket"),
        pytest.param("s3://pmc-oa-opendata/PMC78.1/PMC78.1.xml", id="other-article"),
        pytest.param("s3://pmc-oa-opendata/PMC77.2/PMC77.2.xml", id="other-version"),
        pytest.param("s3://pmc-oa-opendata/PMC77.1/PMC77.1.pdf", id="not-xml"),
        pytest.param("https://evil.example/PMC77.1/PMC77.1.xml", id="not-s3"),
        pytest.param("s3://pmc-oa-opendata/../PMC77.1/PMC77.1.xml", id="traversal"),
        pytest.param("s3://pmc-oa-opendata/PMC77.1/PMC77.1.xml?md5=not-hex", id="bad-md5"),
        pytest.param(
            f"s3://pmc-oa-opendata/PMC77.1/PMC77.1.xml?md5={'a' * 32}&md5={'b' * 32}", id="two-md5"
        ),
    ],
)
async def test_an_xml_url_that_is_not_this_articles_is_never_followed(xml_url: str) -> None:
    bucket = bucket_with(article("PMC77", xml_url=xml_url))

    fetched = await _files(bucket).fetch_jats("PMC77")

    assert fetched == PmcUnavailable(PmcFetchProblem.MALFORMED)
    assert len(bucket.requests) == 2  # the metadata's two GETs only


async def test_a_link_without_an_md5_is_never_followed() -> None:
    # PMC's metadata always records one; without it a truncated body could not be told apart.
    bucket = FakeBucket()
    bucket.put_with_xml(article("PMC77"), _XML)
    bucket.put(article("PMC77", xml_url="s3://pmc-oa-opendata/PMC77.1/PMC77.1.xml"))

    fetched = await _files(bucket).fetch_jats("PMC77")

    assert fetched == PmcUnavailable(PmcFetchProblem.MALFORMED)
    assert len(bucket.requests) == 2  # the metadata's two GETs only


async def test_an_uppercase_md5_is_checked_like_a_lowercase_one() -> None:
    bucket = FakeBucket()
    bucket.put_with_xml(
        article("PMC77"), _XML, md5=hashlib.md5(_XML, usedforsecurity=False).hexdigest().upper()
    )

    assert await _files(bucket).fetch_jats("PMC77") == _XML


async def test_one_deadline_bounds_the_xml_request() -> None:
    bucket = bucket_with(article("PMC77"))
    bucket.put_with_xml(article("PMC77"), _XML)
    inner = bucket.transport()

    async def slow_xml(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith(".xml"):
            await asyncio.sleep(1)
        return await inner.handle_async_request(request)

    files = PmcCloudFiles(
        metadata=_metadata(bucket),
        bucket_url=BUCKET,
        timeout_seconds=0.1,
        max_bytes=1024,
        transport=httpx2.MockTransport(slow_xml),
    )

    assert await files.fetch_jats("PMC77") == PmcUnavailable(PmcFetchProblem.TIMEOUT)
