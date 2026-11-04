"""A fake `pmc-oa-opendata` bucket for the PMC staging tests (ADR-0036, #458). It answers the two
requests the adapter makes, ListObjectsV2 on `metadata/<PMCID>.` and GET of one version's JSON,
from articles kept in memory, and records every request it was sent."""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import httpx2

BUCKET = "https://pmc-oa-opendata.s3.amazonaws.com"
DEMO = Path(__file__).parent / "fixtures" / "pmc" / "PMC13692514.1.json"

# `gone`: listed, then removed before the GET, which S3 answers 404.
type Failure = Literal["timeout", "error", "malformed", "server_error", "gone"]


def demo_article() -> dict[str, object]:
    """The real JSON of PMC13692514, version 1, read from the bucket on 2026-11-04."""
    loaded: dict[str, object] = json.loads(DEMO.read_bytes())
    return loaded


def article(pmcid: str, **overrides: object) -> dict[str, object]:
    """A CC BY article shaped like the demo's JSON, with its own identifiers."""
    number = pmcid.removeprefix("PMC")
    fields: dict[str, object] = {
        **demo_article(),
        "pmcid": pmcid,
        "version": 1,
        "pmid": int(number),
        "doi": f"10.9999/test.{number}",
        "title": f"Article {pmcid}",
        "citation": f"J Test. 2026;1:{number}.",
    }
    fields.update(overrides)
    return fields


@dataclass
class FakeBucket:
    # PMC ID → version → that version's JSON.
    articles: dict[str, dict[int, Mapping[str, object]]] = field(default_factory=dict)
    # PMC ID → how a request about it fails.
    failing: dict[str, Failure] = field(default_factory=dict)
    requests: list[httpx2.Request] = field(default_factory=list)

    def put(self, data: Mapping[str, object]) -> None:
        version = data["version"]
        assert isinstance(version, int)
        self.articles.setdefault(str(data["pmcid"]), {})[version] = data

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self._respond)

    def requested_ids(self) -> list[str]:
        return [_pmcid(str(request.url)) for request in self.requests]

    def _respond(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        pmcid = _pmcid(str(request.url))
        failure = self.failing.get(pmcid)
        if failure == "timeout":
            raise httpx2.ReadTimeout("too slow", request=request)
        if failure == "error":
            raise httpx2.ConnectError("unreachable", request=request)
        if failure == "server_error":
            return httpx2.Response(503, content=b"<Error>SlowDown</Error>")
        if request.url.path == "/":
            return self._list(pmcid)
        if failure == "gone":
            return httpx2.Response(404, content=b"<Error>NoSuchKey</Error>")
        return self._get(request.url.path, malformed=failure == "malformed")

    def _list(self, pmcid: str) -> httpx2.Response:
        keys = "".join(
            f"<Contents><Key>metadata/{pmcid}.{version}.json</Key></Contents>"
            for version in sorted(self.articles.get(pmcid, {}))
        )
        body = (
            '<?xml version="1.0" encoding="UTF-8"?><ListBucketResult '
            'xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Name>pmc-oa-opendata</Name>'
            f"<Prefix>metadata/{pmcid}.</Prefix>{keys}<IsTruncated>false</IsTruncated>"
            "</ListBucketResult>"
        )
        return httpx2.Response(200, content=body.encode())

    def _get(self, path: str, *, malformed: bool) -> httpx2.Response:
        found = re.fullmatch(r"/metadata/(PMC[0-9]+)\.([0-9]+)\.json", path)
        if found is None:
            return httpx2.Response(404, content=b"<Error>NoSuchKey</Error>")
        data = self.articles.get(found[1], {}).get(int(found[2]))
        if data is None:
            return httpx2.Response(404, content=b"<Error>NoSuchKey</Error>")
        if malformed:
            return httpx2.Response(200, content=b'{"pmcid": ')
        return httpx2.Response(200, content=json.dumps(data).encode())


def _pmcid(url: str) -> str:
    found = re.search(r"PMC[0-9]+", url)
    assert found is not None, url
    return found[0]


def bucket_with(*articles: Mapping[str, object]) -> FakeBucket:
    bucket = FakeBucket()
    for data in articles:
        bucket.put(data)
    return bucket
