"""Staging PMC articles by ID through the knowledge API (ADR-0036, #458): real PostgreSQL, the
app's middleware, and PMC's bucket as a mock transport, never the network.

PMC IDs are random per test: identifiers are unique, and a second insert of one another test's
open transaction holds would wait for it (knowledge_support.py)."""

from collections.abc import Callable, Mapping
from uuid import UUID, uuid4

from httpx2 import Response
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.knowledge_chunk_repository import SqlAlchemyKnowledgeIngestionRepository
from ai_trainer.adapters.knowledge_repository import SqlAlchemyKnowledgeSourcesRepository
from ai_trainer.domain.knowledge import (
    KnowledgeFileType,
    LicenseId,
    LicenseSource,
    SourceStatus,
)
from tests.integration.test_knowledge_api import NOW, Api, _add_source, _api, _user
from tests.pmc_support import FakeBucket, article, bucket_with

STAGE = "/api/admin/knowledge/stage/pmc"


def _pmcid() -> str:
    return f"PMC{uuid4().int % 10**12}"


def _ask(pmcid: str, **overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "pmcid": pmcid,
        "evidence_tier": "peer_reviewed",
        "sports": ["gym"],
        "topics": ["Strength", "older adults"],
    }
    fields.update(overrides)
    return fields


def _stage(api: Api, token: str | None, *articles: Mapping[str, object]) -> Response:
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    return api.client.post(STAGE, json={"articles": list(articles)}, headers=headers)


def _results(response: Response) -> dict[str, dict[str, object]]:
    assert response.status_code == 200, response.text
    return {result["pmcid"]: result for result in response.json()["results"]}


async def test_a_cc_by_article_is_staged_proposed_with_its_identifiers_and_licence(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    pmcid = _pmcid()
    bucket = bucket_with(article(pmcid))
    api = await _api(db_session_factory, bucket=bucket)

    results = _results(_stage(api, await api.token(), _ask(pmcid)))

    result = results[pmcid]
    assert (result["outcome"], result["status"]) == ("staged", "proposed")
    source = await SqlAlchemyKnowledgeSourcesRepository(db_session_factory).get(
        UUID(str(result["source_id"]))
    )
    assert source is not None
    number = pmcid.removeprefix("PMC")
    assert source.status is SourceStatus.PROPOSED
    assert (source.title, source.pmcid, source.doi, source.pmid) == (
        f"Article {pmcid}", pmcid, f"10.9999/test.{number}", number,
    )  # fmt: skip
    assert (source.license_id, source.license_source) == (LicenseId.CC_BY, LicenseSource.PMC)
    assert source.attribution is not None
    assert source.attribution.endswith("Licence: CC BY. Source: NLM PMC.")
    assert source.file_type is KnowledgeFileType.JATS
    assert source.storage_key is None
    assert source.topics == ("strength", "older adults")
    # The token's user, the only `user_id` on these routes.
    assert (source.uploaded_by_user_id, source.uploaded_at) == (api.admin.id, NOW)


async def test_retracted_and_tdm_articles_are_refused_and_the_rest_staged(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    retracted, tdm, fine = _pmcid(), _pmcid(), _pmcid()
    bucket = bucket_with(
        article(retracted, is_retracted=True),
        article(tdm, license_code="TDM", is_manuscript=True),
        article(fine, license_code="CC BY-NC-SA"),
    )
    api = await _api(db_session_factory, bucket=bucket)

    results = _results(_stage(api, await api.token(), _ask(retracted), _ask(tdm), _ask(fine)))

    assert results[retracted] == {
        "pmcid": retracted, "outcome": "refused", "source_id": None, "status": None,
        "reason": "retracted", "license_code": "CC BY",
    }  # fmt: skip
    assert results[tdm] == {
        "pmcid": tdm, "outcome": "refused", "source_id": None, "status": None,
        "reason": "licence_not_admitted", "license_code": "TDM",
    }  # fmt: skip
    assert results[fine]["outcome"] == "staged"
    repo = SqlAlchemyKnowledgeSourcesRepository(db_session_factory)
    assert await repo.find_by_identifiers(pmcid=retracted) is None
    assert await repo.find_by_identifiers(pmcid=tdm) is None
    staged = await repo.find_by_identifiers(pmcid=fine)
    assert staged is not None
    assert staged.license_id is LicenseId.CC_BY_NC_SA


async def test_an_id_already_staged_or_ingested_is_covered(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    staged_before, ingested = _pmcid(), _pmcid()
    bucket = bucket_with(article(staged_before), article(ingested))
    api = await _api(db_session_factory, bucket=bucket)
    token = await api.token()
    first = _results(_stage(api, token, _ask(staged_before)))[staged_before]
    number = ingested.removeprefix("PMC")
    stored = await _add_source(
        api,
        pmcid=ingested,
        doi=f"10.9999/stored.{number}",
        pmid=None,
        source_url=None,
        status=SourceStatus.INGESTED,
    )
    bucket.requests.clear()

    results = _results(_stage(api, token, _ask(staged_before), _ask(ingested)))

    assert results[staged_before] == {
        "pmcid": staged_before, "outcome": "covered", "source_id": first["source_id"],
        "status": "proposed", "reason": None, "license_code": None,
    }  # fmt: skip
    assert (results[ingested]["outcome"], results[ingested]["source_id"]) == (
        "covered", str(stored.id),
    )  # fmt: skip
    assert results[ingested]["status"] == "ingested"
    # A covered ID costs no request to PMC.
    assert bucket.requests == []


async def test_a_paper_uploaded_under_its_doi_is_covered(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    pmcid = _pmcid()
    bucket = bucket_with(article(pmcid))
    api = await _api(db_session_factory, bucket=bucket)
    number = pmcid.removeprefix("PMC")
    uploaded = await _add_source(
        api, pmcid=None, pmid=None, doi=f"10.9999/test.{number}", status=SourceStatus.INGESTED
    )

    results = _results(_stage(api, await api.token(), _ask(pmcid)))

    assert (results[pmcid]["outcome"], results[pmcid]["source_id"]) == (
        "covered", str(uploaded.id),
    )  # fmt: skip


async def test_unknown_ids_and_bucket_timeouts_are_reported_per_id(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    unknown, slow, broken, garbled, fine = (_pmcid() for _ in range(5))
    bucket = bucket_with(article(slow), article(broken), article(garbled), article(fine))
    bucket.failing.update({slow: "timeout", broken: "server_error", garbled: "malformed"})
    api = await _api(db_session_factory, bucket=bucket)

    response = _stage(
        api, await api.token(), *(_ask(p) for p in (unknown, slow, broken, garbled, fine))
    )

    results = _results(response)
    assert list(results) == [unknown, slow, broken, garbled, fine]
    assert {p: (r["outcome"], r["reason"]) for p, r in results.items()} == {
        unknown: ("not_found", None),
        slow: ("unavailable", "timeout"),
        broken: ("unavailable", "failed"),
        garbled: ("unavailable", "malformed"),
        fine: ("staged", None),
    }


async def test_the_worker_never_claims_a_staged_source(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    pmcid = _pmcid()
    api = await _api(db_session_factory, bucket=bucket_with(article(pmcid)))
    staged = _results(_stage(api, await api.token(), _ask(pmcid)))[pmcid]
    queue = SqlAlchemyKnowledgeIngestionRepository(db_session_factory)

    # Drain whatever else the shared database holds; this transaction rolls back.
    claimed: list[UUID] = []
    while (
        source := await queue.claim_next(claim_id=uuid4(), now=NOW, lease_until=NOW)
    ) is not None:
        claimed.append(source.id)

    assert UUID(str(staged["source_id"])) not in claimed
    stored = await SqlAlchemyKnowledgeSourcesRepository(db_session_factory).get(
        UUID(str(staged["source_id"]))
    )
    assert stored is not None
    assert stored.status is SourceStatus.PROPOSED


async def test_staging_needs_an_admin_token(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    pmcid = _pmcid()
    bucket = bucket_with(article(pmcid))
    api = await _api(db_session_factory, bucket=bucket)
    outsider = await _user(db_session_factory)

    assert _stage(api, None, _ask(pmcid)).status_code == 401
    assert _stage(api, "atk_not-a-token", _ask(pmcid)).status_code == 401
    assert _stage(api, await api.token(outsider), _ask(pmcid)).status_code == 403
    assert bucket.requests == []


async def test_an_invalid_request_is_refused_whole_before_any_fetch(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    bucket = FakeBucket()
    api = await _api(db_session_factory, bucket=bucket)
    token = await api.token()
    pmcid = _pmcid()

    assert _stage(api, token).status_code == 422
    assert _stage(api, token, *(_ask(_pmcid()) for _ in range(21))).status_code == 422
    assert _stage(api, token, _ask("12345")).status_code == 422
    assert _stage(api, token, _ask(pmcid, sports=[])).status_code == 422
    assert _stage(api, token, _ask(pmcid, extra="field")).status_code == 422
    unknown_sport = _stage(api, token, _ask(pmcid, sports=["chess"]))
    assert unknown_sport.status_code == 422
    assert unknown_sport.json() == {"detail": "Name only the app's sports: chess."}
    assert _stage(api, token, _ask(pmcid, evidence_tier="catalogue")).status_code == 422
    repeated = _stage(api, token, _ask(pmcid), _ask(pmcid, topics=["other"]))
    assert repeated.status_code == 422
    assert repeated.json() == {"detail": f"Name each PMC ID once: {pmcid}."}
    assert bucket.requests == []
