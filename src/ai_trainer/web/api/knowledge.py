"""The knowledge manifest for token clients (ADR-0036, #457): what is staged or stored already, so
`/research-knowledge` can skip it. Status, identifiers, licence and content hash; nothing about
an athlete (ADR-0036 invariant 7)."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict

from ai_trainer.application.admin_tokens import TokenAuthenticator
from ai_trainer.application.knowledge_sources import KnowledgeSources
from ai_trainer.domain.knowledge import (
    KnowledgeFileType,
    LicenceFlag,
    LicenseId,
    LicenseSource,
    SourceStatus,
)
from ai_trainer.web.api.auth import refuse_token_in_query, token_user
from ai_trainer.web.api.paths import KNOWLEDGE_API_PREFIX

DEFAULT_PAGE = 200
MAX_PAGE = 500


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


def build_knowledge_api_router(
    *, authenticator: TokenAuthenticator, knowledge: KnowledgeSources
) -> APIRouter:
    # In this order: a token in the query is refused before the header is read.
    router = APIRouter(
        prefix=KNOWLEDGE_API_PREFIX,
        tags=["knowledge-api"],
        dependencies=[Depends(refuse_token_in_query), Depends(token_user(authenticator))],
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

    return router
