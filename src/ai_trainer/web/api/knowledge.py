"""The knowledge API for token clients (ADR-0036): the manifest of what is staged or stored
already, so `/research-knowledge` can skip it (#457), staging papers by PMC ID (#458), and
staging files for upload by presigned PUT (#460). Status, identifiers, licence and content hash;
nothing about an athlete (invariant 7). No route takes a file's bytes (invariant 3)."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, assert_never
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from ai_trainer.application.admin_tokens import TokenAuthenticator
from ai_trainer.application.knowledge_file_staging import (
    FileStaging,
    ObjectMissing,
    RegisterCovered,
    Registered,
    RegisterOutcome,
    RegisterRefused,
    StageCovered,
    StageOutcome,
    StageRefused,
    UploadNotFound,
    UploadReady,
)
from ai_trainer.application.knowledge_sources import KnowledgeSources, StorageUnavailable
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
    UploadProblem,
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


class ApiStageFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The name the file goes by; its extension names the type register checks the bytes against.
    file_name: str = Field(min_length=1, max_length=255)
    # #456's template under its CSL-JSON keys (`type`, `title`, `author`, `ISBN`, …) plus
    # `evidence_tier`, `source_type`, `sports`, `topics` and `licence`; null `licence` leaves it
    # for the admin to set before approving.
    source: dict[str, JsonValue]


class StageResult(StrEnum):
    READY = "ready"
    COVERED = "covered"


class ApiStagedFile(BaseModel):
    outcome: StageResult
    # For `ready`: PUT the bytes to `put_url` before `expires_at`, then register `upload_id`.
    # The link is a secret: it carries the key ID and a signature.
    upload_id: UUID | None = None
    put_url: str | None = None
    expires_at: datetime | None = None
    # For `covered`: the source with the same PMC ID, DOI, PMID or ISBN, and its status.
    source_id: UUID | None = None
    status: SourceStatus | None = None


class ApiRegisterFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    upload_id: UUID


class RegisterResult(StrEnum):
    REGISTERED = "registered"
    COVERED = "covered"
    REFUSED = "refused"
    MISSING = "missing"


class ApiRegisteredFile(BaseModel):
    outcome: RegisterResult
    # The source registered, or the one that covers it already; the new object is then deleted.
    source_id: UUID | None = None
    status: SourceStatus | None = None
    file_type: KnowledgeFileType | None = None
    size_bytes: int | None = None
    # Why a `refused` object was deleted: empty (`missing`), `too_large` or `unlisted_type`.
    reason: UploadProblem | None = None


def _unavailable(outcome: StorageUnavailable) -> HTTPException:
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=outcome.reason)


def _staged(outcome: StageOutcome) -> ApiStagedFile:
    match outcome:
        case UploadReady(upload_id=upload_id, put_url=put_url, expires_at=expires_at):
            return ApiStagedFile(
                outcome=StageResult.READY,
                upload_id=upload_id,
                put_url=put_url,
                expires_at=expires_at,
            )
        case StageCovered(source=source):
            return ApiStagedFile(
                outcome=StageResult.COVERED, source_id=source.id, status=source.status
            )
        case StageRefused(problems=problems):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=dict(problems))
        case StorageUnavailable():
            raise _unavailable(outcome)
        case _:  # pragma: no cover -- exhaustive over StageOutcome
            assert_never(outcome)


def _registered(outcome: RegisterOutcome) -> ApiRegisteredFile:
    match outcome:
        case Registered(source=source):
            return ApiRegisteredFile(
                outcome=RegisterResult.REGISTERED,
                source_id=source.id,
                status=source.status,
                file_type=source.file_type,
                size_bytes=source.size_bytes,
            )
        case RegisterCovered(source=source):
            return ApiRegisteredFile(
                outcome=RegisterResult.COVERED, source_id=source.id, status=source.status
            )
        case RegisterRefused(problem=problem):
            return ApiRegisteredFile(outcome=RegisterResult.REFUSED, reason=problem)
        case ObjectMissing():
            return ApiRegisteredFile(outcome=RegisterResult.MISSING)
        case UploadNotFound():
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No stage issued that upload.")
        case StorageUnavailable():
            raise _unavailable(outcome)
        case _:  # pragma: no cover -- exhaustive over RegisterOutcome
            assert_never(outcome)


def build_knowledge_api_router(
    *,
    authenticator: TokenAuthenticator,
    knowledge: KnowledgeSources,
    staging: KnowledgeStaging,
    files: FileStaging,
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

    @router.post("/stage/file")
    async def stage_file(body: ApiStageFile) -> ApiStagedFile:
        """Stage a file's metadata and get a presigned PUT to the `uploads` bucket, live for an
        hour. Send the bytes there (`curl -T <file> "$PUT_URL"`), never here, then register the
        upload. A source with the same PMC ID, DOI, PMID or ISBN is `covered`, with no link.
        Staging again issues a new upload and link."""
        return _staged(await files.stage(body.file_name, body.source))

    @router.post("/register")
    async def register_file(
        body: ApiRegisterFile, admin_id: Annotated[UUID, Depends(current_admin)]
    ) -> ApiRegisteredFile:
        """Check an uploaded object as the upload form checks a file: type by its bytes, size and
        content hash. A good one becomes a `proposed` source for an admin to approve. A
        duplicate is `covered` and a bad one `refused`; either way the new object is deleted.
        `missing` means nothing was PUT yet: PUT it and register again."""
        return _registered(await files.register(admin_id, body.upload_id))

    return router
