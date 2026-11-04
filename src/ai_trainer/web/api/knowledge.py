"""The knowledge API for token clients (ADR-0036): the manifest of what is staged or stored
already, so `/research-knowledge` can skip it (#457), and staging papers by PMC ID (#458).
Status, identifiers, licence and content hash; nothing about an athlete (invariant 7)."""

from enum import StrEnum
from typing import Annotated, assert_never
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from ai_trainer.application.admin_tokens import TokenAuthenticator
from ai_trainer.application.knowledge_sources import KnowledgeSources
from ai_trainer.application.knowledge_staging import (
    Covered,
    KnowledgeStaging,
    NotFound,
    PmcOutcome,
    PmcStageRequest,
    Refused,
    Staged,
    Unavailable,
)
from ai_trainer.domain.knowledge import (
    EvidenceTier,
    KnowledgeFileType,
    LicenceFlag,
    LicenseId,
    LicenseSource,
    PmcId,
    SourceLanguage,
    SourceStatus,
)
from ai_trainer.domain.pmc import PmcFetchProblem, PmcRefusal
from ai_trainer.web.api.auth import refuse_token_in_query, token_user
from ai_trainer.web.api.paths import KNOWLEDGE_API_PREFIX

DEFAULT_PAGE = 200
MAX_PAGE = 500
# Five articles at a time, each article's two GETs to PMC's bucket bounded together by
# PMC_TIMEOUT_SECONDS (at most 10): 20 articles answer within 40 s, inside Cloudflare's 100.
MAX_PMC_ARTICLES = 20


class ApiSource(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    status: SourceStatus
    file_type: KnowledgeFileType | None
    source_url: str | None
    pmcid: str | None
    doi: str | None
    pmid: str | None
    isbn: str | None
    license_id: LicenseId | None
    license_source: LicenseSource | None
    license_url: str | None
    licence_flag: LicenceFlag | None
    content_hash: str


class ApiSourcesPage(BaseModel):
    sources: list[ApiSource]
    # Pass it back as `after` for the next page; null on the last one.
    next: UUID | None


class ApiPmcArticle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pmcid: PmcId
    evidence_tier: EvidenceTier
    sports: list[str] = Field(min_length=1)
    topics: list[str] = []
    language: SourceLanguage = SourceLanguage.EN


class ApiStagePmc(BaseModel):
    model_config = ConfigDict(extra="forbid")

    articles: list[ApiPmcArticle] = Field(min_length=1, max_length=MAX_PMC_ARTICLES)


class PmcResult(StrEnum):
    STAGED = "staged"
    COVERED = "covered"
    REFUSED = "refused"
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"


class ApiPmcResult(BaseModel):
    pmcid: str
    outcome: PmcResult
    # The source staged, or the one that covers it already, with its status.
    source_id: UUID | None = None
    status: SourceStatus | None = None
    # Why it was refused, or why PMC couldn't be read; a bucket failure may pass on a retry.
    reason: PmcRefusal | PmcFetchProblem | None = None
    # PMC's own code, e.g. `TDM`, for a refused licence.
    license_code: str | None = None


class ApiStagedPmc(BaseModel):
    # One per distinct PMC ID, in the order first asked.
    results: list[ApiPmcResult]


def _result(outcome: PmcOutcome) -> ApiPmcResult:
    match outcome:
        case Staged(pmcid=pmcid, source=source):
            return ApiPmcResult(
                pmcid=pmcid, outcome=PmcResult.STAGED, source_id=source.id, status=source.status
            )
        case Covered(pmcid=pmcid, source=source):
            return ApiPmcResult(
                pmcid=pmcid, outcome=PmcResult.COVERED, source_id=source.id, status=source.status
            )
        case Refused(pmcid=pmcid, reason=reason, license_code=code):
            return ApiPmcResult(
                pmcid=pmcid, outcome=PmcResult.REFUSED, reason=reason, license_code=code
            )
        case NotFound(pmcid=pmcid):
            return ApiPmcResult(pmcid=pmcid, outcome=PmcResult.NOT_FOUND)
        case Unavailable(pmcid=pmcid, problem=problem):
            return ApiPmcResult(pmcid=pmcid, outcome=PmcResult.UNAVAILABLE, reason=problem)
        case _:  # pragma: no cover -- exhaustive over PmcOutcome
            assert_never(outcome)


def build_knowledge_api_router(
    *, authenticator: TokenAuthenticator, knowledge: KnowledgeSources, staging: KnowledgeStaging
) -> APIRouter:
    # One callable, so FastAPI authenticates once per request where a route asks for the user.
    current_admin = token_user(authenticator)
    # In this order: a token in the query is refused before the header is read.
    router = APIRouter(
        prefix=KNOWLEDGE_API_PREFIX,
        tags=["knowledge-api"],
        dependencies=[Depends(refuse_token_in_query), Depends(current_admin)],
    )

    @router.get("/sources")
    async def list_sources(
        after: Annotated[UUID | None, Query(description="The `next` of the page before.")] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
    ) -> ApiSourcesPage:
        """Every knowledge source, by `id`, a page at a time."""
        page = await knowledge.list_page(after=after, limit=limit)
        return ApiSourcesPage(
            sources=[ApiSource.model_validate(source) for source in page.sources],
            next=page.next,
        )

    @router.post("/stage/pmc")
    async def stage_pmc(
        body: ApiStagePmc, admin_id: Annotated[UUID, Depends(current_admin)]
    ) -> ApiStagedPmc:
        """Stage papers by PMC ID as `proposed` sources for an admin to approve. Each ID's
        metadata is read from PMC's Cloud Service; a retracted article or a licence off the
        admit list is refused, and every ID gets its own result."""
        requests = [
            PmcStageRequest(
                pmcid=article.pmcid,
                evidence_tier=article.evidence_tier,
                sports=tuple(article.sports),
                topics=tuple(article.topics),
                language=article.language,
            )
            for article in body.articles
        ]
        problem = staging.problem(requests)
        if problem is not None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=problem)
        outcomes = await staging.stage_pmc(admin_id, requests)
        return ApiStagedPmc(results=[_result(outcome) for outcome in outcomes])

    return router
