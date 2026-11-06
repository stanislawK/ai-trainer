"""A fake `pmc-oa-opendata` bucket for the PMC staging tests (ADR-0036, #458). It answers the two
requests the adapter makes, ListObjectsV2 on `metadata/<PMCID>.` and GET of one version's JSON,
from articles kept in memory, and records every request it was sent."""

import hashlib
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
        # The demo's link is to its own file; `FakeBucket.put_with_xml` sets one for this article.
        "xml_url": None,
    }
    fields.update(overrides)
    return fields


def xml_url(pmcid: str, version: int, md5: str) -> str:
    """The metadata JSON's `xml_url` as the bucket writes it."""
    key = f"{pmcid}.{version}"
    return f"s3://pmc-oa-opendata/{key}/{key}.xml?md5={md5}"


@dataclass
class FakeBucket:
    # PMC ID → version → that version's JSON.
    articles: dict[str, dict[int, Mapping[str, object]]] = field(default_factory=dict)
    # PMC ID → version → that version's JATS XML (#463).
    xml: dict[str, dict[int, bytes]] = field(default_factory=dict)
    # PMC ID → how a request about it fails.
    failing: dict[str, Failure] = field(default_factory=dict)
    # PMC ID → how the request for its XML (and only that) fails (#463).
    xml_failing: dict[str, Failure] = field(default_factory=dict)
    requests: list[httpx2.Request] = field(default_factory=list)

    def put(self, data: Mapping[str, object]) -> None:
        version = data["version"]
        assert isinstance(version, int)
        self.articles.setdefault(str(data["pmcid"]), {})[version] = data

    def put_with_xml(
        self, data: Mapping[str, object], xml: bytes, *, md5: str | None = None
    ) -> None:
        """Stores the article's JSON, pointing `xml_url` at `xml`, whose md5 is `md5` unless
        another is given (to model a corrupted download)."""
        pmcid, version = str(data["pmcid"]), data["version"]
        assert isinstance(version, int)
        digest = md5 or hashlib.md5(xml, usedforsecurity=False).hexdigest()
        self.put({**data, "xml_url": xml_url(pmcid, version, digest)})
        self.xml.setdefault(pmcid, {})[version] = xml

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
        if request.url.path.endswith(".xml"):
            return self._get_xml(request, pmcid)
        if failure == "gone":
            return httpx2.Response(404, content=b"<Error>NoSuchKey</Error>")
        return self._get(request.url.path, malformed=failure == "malformed")

    def _get_xml(self, request: httpx2.Request, pmcid: str) -> httpx2.Response:
        failure = self.xml_failing.get(pmcid)
        if failure == "timeout":
            raise httpx2.ReadTimeout("too slow", request=request)
        if failure == "error":
            raise httpx2.ConnectError("unreachable", request=request)
        if failure == "server_error":
            return httpx2.Response(503, content=b"<Error>SlowDown</Error>")
        found = re.fullmatch(r"/(PMC[0-9]+)\.([0-9]+)/\1\.\2\.xml", request.url.path)
        data = None if found is None else self.xml.get(found[1], {}).get(int(found[2]))
        if failure == "gone" or data is None:
            return httpx2.Response(404, content=b"<Error>NoSuchKey</Error>")
        return httpx2.Response(200, content=data)

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
