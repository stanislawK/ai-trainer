"""PMC's Cloud Service bucket, `pmc-oa-opendata`: the retrieval route PMC permits (ADR-0036).

Two anonymous GETs per article, both naming only its PMC ID: ListObjectsV2 on
`metadata/<PMCID>.` for its versions, then the highest version's JSON. Nothing else in the
bucket is read here; the article's XML is fetched after approval (#463).
"""

import re

import httpx2
from pydantic import ValidationError

from ai_trainer.domain.pmc import PmcArticle, PmcFetchProblem, PmcNotFound, PmcUnavailable


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
        try:
            async with httpx2.AsyncClient(
                base_url=self._bucket_url, transport=self._transport, timeout=self._timeout
            ) as client:
                listing = await client.get(
                    "/", params={"list-type": "2", "prefix": f"metadata/{pmcid}."}
                )
                version = _latest_version(listing.raise_for_status().text, pmcid)
                if version is None:
                    return PmcNotFound()
                response = await client.get(f"/metadata/{pmcid}.{version}.json")
                article = PmcArticle.model_validate_json(response.raise_for_status().content)
        except httpx2.TimeoutException:
            return PmcUnavailable(PmcFetchProblem.TIMEOUT)
        except httpx2.HTTPError:
            return PmcUnavailable(PmcFetchProblem.FAILED)
        except ValidationError:
            return PmcUnavailable(PmcFetchProblem.MALFORMED)
        if article.pmcid != pmcid:
            return PmcUnavailable(PmcFetchProblem.MALFORMED)
        return article


def _latest_version(listing: str, pmcid: str) -> int | None:
    # The listing's keys only; the prefix ends in a dot, so `PMC77.` never lists PMC771.
    pattern = rf"<Key>metadata/{re.escape(pmcid)}\.([0-9]+)\.json</Key>"
    versions = [int(found) for found in re.findall(pattern, listing)]
    return max(versions, default=None)
