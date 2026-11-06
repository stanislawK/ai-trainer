"""PMC's Cloud Service bucket, `pmc-oa-opendata`: the retrieval route PMC permits (ADR-0036).

Two anonymous GETs per article, both naming only its PMC ID: ListObjectsV2 on
`metadata/<PMCID>.` for its versions, then the highest version's JSON. Nothing else in the
bucket is read by `PmcCloudMetadata`; the article's XML is fetched after approval, by
`PmcCloudFiles` (#463).
"""

import asyncio
import hashlib
import re
from urllib.parse import parse_qs, urlsplit

import httpx2
from pydantic import ValidationError

from ai_trainer.application.ports.pmc import PmcMetadataPort
from ai_trainer.domain.pmc import (
    PMC_BUCKET_NAME,
    PmcArticle,
    PmcFetchProblem,
    PmcNotFound,
    PmcUnavailable,
)

_MD5 = re.compile(r"[0-9a-f]{32}")


class PmcCloudMetadata:
    """Implements `PmcMetadataPort`."""

    def __init__(
        self,
        *,
        bucket_url: str,
        timeout_seconds: float,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._bucket_url = bucket_url
        self._timeout = timeout_seconds
        self._transport = transport

    async def fetch(self, pmcid: str) -> PmcArticle | PmcNotFound | PmcUnavailable:
        # One deadline for the article's two GETs, connect and read included: an httpx timeout
        # bounds each phase separately, which would let a slow bucket outlast Cloudflare's 100 s.
        try:
            async with (
                asyncio.timeout(self._timeout),
                httpx2.AsyncClient(
                    base_url=self._bucket_url, transport=self._transport, timeout=self._timeout
                ) as client,
            ):
                listing = await client.get(
                    "/", params={"list-type": "2", "prefix": f"metadata/{pmcid}."}
                )
                version = _latest_version(listing.raise_for_status().text, pmcid)
                if version is None:
                    return PmcNotFound()
                response = await client.get(f"/metadata/{pmcid}.{version}.json")
                article = PmcArticle.model_validate_json(response.raise_for_status().content)
        except TimeoutError, httpx2.TimeoutException:
            return PmcUnavailable(PmcFetchProblem.TIMEOUT)
        except httpx2.HTTPError:
            return PmcUnavailable(PmcFetchProblem.FAILED)
        except ValidationError:
            return PmcUnavailable(PmcFetchProblem.MALFORMED)
        if (article.pmcid, article.version) != (pmcid, version):
            # Another article's or another version's JSON under this key: its licence and
            # retraction flag can't be trusted for this one.
            return PmcUnavailable(PmcFetchProblem.MALFORMED)
        return article


class PmcCloudFiles:
    """Implements `PmcArticleFilesPort` (#463).

    The article's JSON gives the key of its JATS: `s3://pmc-oa-opendata/<PMCID>.<n>/<PMCID>.<n>.xml
    ?md5=<hex>`. That link is followed only when it is exactly this article's latest version's
    own XML, in the bucket, over the bucket's HTTPS URL; anything else in it is refused, and so is
    a link without the md5 PMC records. The body is read under a size cap and checked against
    that md5.
    """

    def __init__(
        self,
        *,
        metadata: PmcMetadataPort,
        bucket_url: str,
        timeout_seconds: float,
        max_bytes: int,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._metadata = metadata
        self._bucket_url = bucket_url
        self._timeout = timeout_seconds
        self._max_bytes = max_bytes
        self._transport = transport

    async def fetch_jats(self, pmcid: str) -> bytes | PmcNotFound | PmcUnavailable:
        article = await self._metadata.fetch(pmcid)
        if not isinstance(article, PmcArticle):
            return article
        if article.xml_url is None:
            return PmcNotFound()
        located = _locate_xml(article.xml_url, article)
        if located is None:
            return PmcUnavailable(PmcFetchProblem.MALFORMED)
        path, md5 = located
        try:
            # One deadline for the whole download, as for the metadata's two GETs.
            async with (
                asyncio.timeout(self._timeout),
                httpx2.AsyncClient(
                    base_url=self._bucket_url, transport=self._transport, timeout=self._timeout
                ) as client,
                client.stream("GET", path) as response,
            ):
                if response.status_code == httpx2.codes.NOT_FOUND:
                    return PmcNotFound()
                response.raise_for_status()
                data = await self._read(response)
        except TimeoutError, httpx2.TimeoutException:
            return PmcUnavailable(PmcFetchProblem.TIMEOUT)
        except httpx2.HTTPError:
            return PmcUnavailable(PmcFetchProblem.FAILED)
        if data is None or _md5(data) != md5:
            return PmcUnavailable(PmcFetchProblem.MALFORMED)
        return data

    async def _read(self, response: httpx2.Response) -> bytes | None:
        """The body, or None as soon as it passes the cap: an oversized file is never held whole."""
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > self._max_bytes:
                return None
            chunks.append(chunk)
        return b"".join(chunks)


def _locate_xml(xml_url: str, article: PmcArticle) -> tuple[str, str] | None:
    """The bucket path and recorded md5 of this article's own XML, or None for any other link."""
    parts = urlsplit(xml_url)
    key = f"{article.pmcid}.{article.version}"
    path = f"/{key}/{key}.xml"
    if (parts.scheme, parts.netloc, parts.path) != ("s3", PMC_BUCKET_NAME, path):
        return None
    # PMC's metadata always records the md5; a link without one is not followed, since a
    # truncated body could not be told from the whole file.
    wanted = parse_qs(parts.query).get("md5", [])
    if len(wanted) != 1 or not _MD5.fullmatch(wanted[0].lower()):
        return None
    return path, wanted[0].lower()


def _md5(data: bytes) -> str:
    # PMC records md5 as an integrity check of the download, not for security.
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


def _latest_version(listing: str, pmcid: str) -> int | None:
    # The listing's keys only; the prefix ends in a dot, so `PMC77.` never lists PMC771.
    pattern = rf"<Key>metadata/{re.escape(pmcid)}\.([0-9]+)\.json</Key>"
    versions = [int(found) for found in re.findall(pattern, listing)]
    return max(versions, default=None)
