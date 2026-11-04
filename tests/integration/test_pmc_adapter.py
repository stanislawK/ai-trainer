"""Reading article metadata from PMC's Cloud Service bucket (ADR-0036, #458). No network: every
response comes from the fake bucket's mock transport."""

import asyncio

import httpx2
import pytest

from ai_trainer.adapters.pmc import PmcCloudMetadata
from ai_trainer.domain.pmc import PmcArticle, PmcFetchProblem, PmcNotFound, PmcUnavailable
from tests.pmc_support import BUCKET, Failure, FakeBucket, article, bucket_with, demo_article


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
